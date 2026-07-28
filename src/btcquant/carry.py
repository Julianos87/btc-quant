"""Module cash-and-carry : encaisser le funding des perpétuels, delta-neutre.

Structure documentée (basis trade classique) : long spot + short perpétuel de
même taille. Le risque de prix s'annule ; on encaisse le funding versé par
les longs aux shorts toutes les 8 h (positif ~85 % du temps sur BTC).

Règles (pas d'encaissement aveugle) :
- ENTRÉE  : funding lissé (moyenne mobile `smooth_days` jours) annualisé
            > `enter_ann` — le loyer est assez élevé pour payer les coûts.
- SORTIE  : funding lissé annualisé < `exit_ann` — le régime est devenu
            défavorable (les shorts paieraient les longs).
- Décision au paiement t, position effective au paiement t+1 (pas de look-ahead).

Coûts : 4 exécutions par cycle (2 jambes × entrée + sortie), proportionnels
au levier. Le levier multiplie le notionnel des deux jambes (portfolio margin).

Le modèle accepte les prix spot/perp, un taux d'emprunt variable et les
paramètres de marge. Sans ces données, les hypothèses synthétiques restent
visibles dans le résultat et ne peuvent pas servir à qualifier le carry réel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

from .domain.carry_decision import decide_carry_payment
from .performance import daily_returns, sharpe_ratio

log = logging.getLogger(__name__)

PAYMENTS_PER_DAY = 3  # funding toutes les 8 h
PAYMENTS_PER_YEAR = PAYMENTS_PER_DAY * 365


def load_funding(
    symbol_perp: str = "BTC/USDT:USDT",
    data_dir: str | Path = "data",
    refresh: bool = True,
) -> pd.Series:
    """Historique complet des taux de funding Binance (cache CSV incrémental)."""
    safe = symbol_perp.replace("/", "").replace(":", "_")
    path = Path(data_dir) / f"binanceusdm_{safe}_funding.csv"
    cached: pd.Series | None = None
    if path.exists():
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True, format="ISO8601")
        cached = df["rate"]

    if refresh:
        ex = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30_000})
        since = (
            int(cached.index[-1].timestamp() * 1000) + 1
            if cached is not None and len(cached)
            else ex.parse8601("2019-09-10T00:00:00Z")
        )
        rows = []
        while True:
            batch = ex.fetch_funding_rate_history(symbol_perp, since=since, limit=1000)
            if not batch:
                break
            rows += batch
            last = batch[-1]["timestamp"]
            if last <= since or len(batch) < 1000:
                break
            since = last + 1
        if rows:
            fresh = pd.Series(
                [float(r["fundingRate"]) for r in rows],
                index=pd.DatetimeIndex(
                    [pd.Timestamp(r["timestamp"], unit="ms", tz="UTC") for r in rows]
                ),
                name="rate",
            )
            cached = pd.concat([cached, fresh]) if cached is not None else fresh
            cached = cached[~cached.index.duplicated(keep="last")].sort_index()
            path.parent.mkdir(parents=True, exist_ok=True)
            cached.to_frame().to_csv(path, index_label="ts")
    if cached is None:
        raise FileNotFoundError(f"Aucun cache funding pour {symbol_perp} et refresh=False")
    return cached


def add_funding_columns(df: pd.DataFrame, funding_8h: pd.Series, pandas_freq: str) -> pd.DataFrame:
    """Ajoute les DEUX colonnes de funding attendues par le moteur, qui n'ont
    ni la même unité ni le même usage — les confondre était la cause de l'écart
    backtest/paper documenté jusqu'en juillet 2026 :

    - ``funding_rate`` : **somme des paiements tombant dans la barre**, servant
      au P&L. Sur des barres 4 h et un funding 8 h, une barre sur deux vaut
      exactement 0 (les paiements tombent à 00/08/16 UTC).
    - ``funding_at_open`` : paiement exactement à l'ouverture de la barre ;
      il concerne seulement la position détenue avant les ordres d'ouverture.
    - ``funding_after_open`` : paiements postérieurs à l'ouverture et antérieurs
      à la clôture ; ils concernent la position détenue pendant la barre.
    - ``funding`` : **dernier taux 8 h connu** à la clôture de la barre, servant
      au filtre d'entrée de `TrendLS`. C'est l'équivalent backtest de
      `Venue.funding_rate_8h()` côté live.

    Alimenter le filtre avec ``funding_rate`` le rendrait actif une barre sur
    deux seulement, et sous-estimerait le taux d'un facteur deux sur les autres.

    Pas de look-ahead : le filtre d'une barre utilise le dernier paiement connu
    avant sa clôture, jamais un paiement futur.
    """
    out = df.copy()
    offset = pd.tseries.frequencies.to_offset(pandas_freq)
    funding_index = pd.DatetimeIndex(funding_8h.index)
    bucket = funding_index.floor(pandas_freq)
    at_open_mask = funding_index == bucket
    at_open = funding_8h[at_open_mask].groupby(bucket[at_open_mask]).sum()
    after_open = funding_8h[~at_open_mask].groupby(bucket[~at_open_mask]).sum()
    per_bar = funding_8h.groupby(bucket).sum()
    out["funding_rate"] = per_bar.reindex(out.index).fillna(0.0)
    out["funding_at_open"] = at_open.reindex(out.index).fillna(0.0)
    out["funding_after_open"] = after_open.reindex(out.index).fillna(0.0)
    close_times = pd.DatetimeIndex(out.index + offset)
    out["funding"] = funding_8h.reindex(close_times, method="ffill").to_numpy()
    return out


#: Coût annuel par défaut des fonds empruntés pour financer la jambe spot.
#: Ordre de grandeur du taux USDT en margin isolé/croisé sur les grandes
#: plateformes : très variable (il grimpe justement quand le funding est élevé,
#: puisque les deux traduisent la même demande de levier). À surcharger avec le
#: taux réellement consenti par la plateforme utilisée.
DEFAULT_BORROW_RATE_ANN = 0.10


@dataclass(frozen=True)
class CarryPolicy:
    """Règles du carry — SOURCE UNIQUE du backtest, du runner et des références.

    Ces valeurs étaient auparavant recopiées à trois endroits (défauts de
    ``backtest_carry``, défauts de ``CarryRunner``, constantes de
    ``make_yearly_reference``) avec des chiffres différents : le backtest
    publié n'était donc pas celui du moteur paper. Toute modification doit se
    faire ici, et invalide les références (leur provenance est hashée).
    """

    capital: float = 4000.0
    leverage: float = 3.0
    enter_ann: float = 0.03
    exit_ann: float = 0.0
    smooth_days: int = 14
    fee_rate: float = 0.0005
    slippage_bps: float = 5.0
    borrow_rate_ann: float = DEFAULT_BORROW_RATE_ANN

    def __post_init__(self) -> None:
        if self.leverage < 1.0:
            raise ValueError("leverage < 1 non modélisé (sous-emploi du capital)")
        if self.capital <= 0:
            raise ValueError("capital doit être strictement positif")
        if self.smooth_days < 1:
            raise ValueError("smooth_days doit valoir au moins 1 jour")
        if self.enter_ann < self.exit_ann:
            raise ValueError("enter_ann doit être supérieur ou égal à exit_ann")
        for name in ("fee_rate", "slippage_bps", "borrow_rate_ann"):
            value = getattr(self, name)
            if not np.isfinite(value) or value < 0:
                raise ValueError(f"{name} doit être fini et positif ou nul")

    @property
    def switch_cost(self) -> float:
        """Coût d'une bascule ON/OFF : 2 jambes, proportionnel au levier."""

        return 2 * (self.fee_rate + self.slippage_bps / 10_000.0) * self.leverage


#: Profil exécuté en paper sur le VPS (cf. deploy/btcquant-carry.service).
PAPER_CARRY_POLICY = CarryPolicy()


def _validate_carry_backtest_inputs(
    leverage: float,
    *,
    borrow_rate_ann: float,
    collateral_haircut: float,
    maintenance_margin_rate: float,
    liquidation_fee_rate: float,
) -> None:
    if leverage < 1.0:
        raise ValueError("leverage < 1 non modélisé (sous-emploi du capital)")
    values = {
        "borrow_rate_ann": borrow_rate_ann,
        "collateral_haircut": collateral_haircut,
        "maintenance_margin_rate": maintenance_margin_rate,
        "liquidation_fee_rate": liquidation_fee_rate,
    }
    for name, value in values.items():
        if not np.isfinite(value) or value < 0:
            raise ValueError(f"{name} doit être fini et positif ou nul")
    if collateral_haircut >= 1:
        raise ValueError("collateral_haircut doit être inférieur à 1")
    if maintenance_margin_rate >= 0.5:
        raise ValueError("maintenance_margin_rate doit être inférieur à 0.5")


def _aligned_series(name: str, values: pd.Series, index: pd.Index) -> pd.Series:
    numeric = values.astype(float).reindex(index)
    if numeric.isna().any() or not np.isfinite(numeric.to_numpy()).all():
        raise ValueError(f"{name} doit couvrir exactement tous les paiements de funding")
    return numeric


def _carry_market_components(
    funding: pd.Series,
    *,
    leverage: float,
    borrow_rate_ann: float,
    borrow_rate_ann_series: pd.Series | None,
    spot_price: pd.Series | None,
    perp_price: pd.Series | None,
) -> tuple[pd.Series, pd.Series, str]:
    if borrow_rate_ann_series is None:
        borrow_rates = pd.Series(float(borrow_rate_ann), index=funding.index)
    else:
        borrow_rates = _aligned_series(
            "borrow_rate_ann_series", borrow_rate_ann_series, funding.index
        )
        if (borrow_rates < 0).any():
            raise ValueError("borrow_rate_ann_series contient un taux négatif")

    if (spot_price is None) != (perp_price is None):
        raise ValueError("spot_price et perp_price doivent être fournis ensemble")
    if spot_price is None:
        return borrow_rates, pd.Series(0.0, index=funding.index), "synthetic_zero"
    spot = _aligned_series("spot_price", spot_price, funding.index)
    perp = _aligned_series("perp_price", perp_price, funding.index)  # type: ignore[arg-type]
    if (spot <= 0).any() or (perp <= 0).any():
        raise ValueError("les prix spot/perp doivent être strictement positifs")
    basis_return = (spot.pct_change() - perp.pct_change()).fillna(0.0)
    return borrow_rates, basis_return, "observed"


def _carry_applied_positions(
    funding: pd.Series,
    *,
    smooth_days: int,
    enter_ann: float,
    exit_ann: float,
) -> pd.Series:
    smooth = funding.rolling(
        smooth_days * PAYMENTS_PER_DAY,
        min_periods=PAYMENTS_PER_DAY,
    ).mean()
    smooth_ann = smooth * PAYMENTS_PER_YEAR
    in_position = pd.Series(False, index=funding.index)
    state = False
    for i, value in enumerate(smooth_ann):
        decision = decide_carry_payment(
            in_position=state,
            smooth_ann=float(value),
            enter_ann=enter_ann,
            exit_ann=exit_ann,
        )
        state = decision.in_position
        in_position.iloc[i] = state
    return in_position.shift(1, fill_value=False)


def _apply_carry_liquidation(
    pnl: pd.Series,
    applied: pd.Series,
    *,
    initial_capital: float,
    leverage: float,
    collateral_haircut: float,
    maintenance_margin_rate: float,
    liquidation_fee_rate: float,
) -> tuple[pd.Series, pd.Series, pd.Series, bool, object | None]:
    equity = initial_capital * (1.0 + pnl).cumprod()
    entry_equity = initial_capital
    for i, timestamp in enumerate(applied.index):
        if bool(applied.iloc[i]) and (i == 0 or not bool(applied.iloc[i - 1])):
            entry_equity = initial_capital if i == 0 else float(equity.iloc[i - 1])
        if not bool(applied.iloc[i]):
            continue
        spot_notional = entry_equity * leverage
        effective_collateral = float(equity.iloc[i]) - spot_notional * collateral_haircut
        maintenance = 2.0 * spot_notional * maintenance_margin_rate
        if effective_collateral > maintenance:
            continue
        before = initial_capital if i == 0 else float(equity.iloc[i - 1])
        after = max(
            0.0,
            float(equity.iloc[i]) - 2.0 * spot_notional * liquidation_fee_rate,
        )
        pnl.iloc[i] = after / before - 1.0
        if i + 1 < len(pnl):
            pnl.iloc[i + 1 :] = 0.0
            applied.iloc[i + 1 :] = False
        equity = initial_capital * (1.0 + pnl).cumprod()
        return pnl, equity, applied, True, timestamp
    return pnl, equity, applied, False, None


def backtest_carry(
    funding: pd.Series,
    leverage: float = PAPER_CARRY_POLICY.leverage,
    fee_rate: float = PAPER_CARRY_POLICY.fee_rate,
    slippage_bps: float = PAPER_CARRY_POLICY.slippage_bps,
    enter_ann: float = PAPER_CARRY_POLICY.enter_ann,
    exit_ann: float = PAPER_CARRY_POLICY.exit_ann,
    smooth_days: int = PAPER_CARRY_POLICY.smooth_days,
    initial_capital: float = 10_000.0,
    borrow_rate_ann: float = PAPER_CARRY_POLICY.borrow_rate_ann,
    borrow_rate_ann_series: pd.Series | None = None,
    spot_price: pd.Series | None = None,
    perp_price: pd.Series | None = None,
    collateral_haircut: float = 0.0,
    maintenance_margin_rate: float = 0.0,
    liquidation_fee_rate: float = 0.0,
) -> dict:
    """Backtest du cash-and-carry avec règles d'entrée/sortie sur funding lissé.

    Le levier a un coût. Un carry à levier L immobilise L×capital de spot alors
    que l'on ne dispose que du capital : les (L−1)×capital manquants sont
    empruntés et se paient, en continu, tant que la position est ouverte.
    Ignorer ce poste — ce que faisait le modèle jusqu'au 18/07/2026 — surestime
    massivement le rendement et produit un Sharpe irréaliste, le funding
    apparaissant alors comme un revenu sans contrepartie.

    Rendement par période, position ouverte :
        L × funding − (L−1) × borrow_rate_ann / PAYMENTS_PER_YEAR

    À ``leverage=1.0`` le terme d'emprunt s'annule : la position est intégralement
    financée par le capital, ce qui est le seul cas réalisable sans marge.
    """
    _validate_carry_backtest_inputs(
        leverage,
        borrow_rate_ann=borrow_rate_ann,
        collateral_haircut=collateral_haircut,
        maintenance_margin_rate=maintenance_margin_rate,
        liquidation_fee_rate=liquidation_fee_rate,
    )
    borrow_rates, basis_return, basis_mode = _carry_market_components(
        funding,
        leverage=leverage,
        borrow_rate_ann=borrow_rate_ann,
        borrow_rate_ann_series=borrow_rate_ann_series,
        spot_price=spot_price,
        perp_price=perp_price,
    )
    borrow_per_period = (leverage - 1.0) * borrow_rates / PAYMENTS_PER_YEAR
    applied = _carry_applied_positions(
        funding,
        smooth_days=smooth_days,
        enter_ann=enter_ann,
        exit_ann=exit_ann,
    )

    cost_per_switch = 2 * (fee_rate + slippage_bps / 10_000.0) * leverage  # 2 jambes
    switches = applied != applied.shift(1, fill_value=False)
    # le coût d'emprunt court uniquement quand la position est ouverte : hors
    # position, rien n'est emprunté puisque rien n'est immobilisé en spot.
    funding_component = applied * funding * leverage
    borrow_component = applied * borrow_per_period
    basis_component = applied * basis_return * leverage
    pnl = funding_component - borrow_component + basis_component - switches * cost_per_switch
    pnl, equity, applied, liquidated, liquidation_ts = _apply_carry_liquidation(
        pnl,
        applied,
        initial_capital=initial_capital,
        leverage=leverage,
        collateral_haircut=collateral_haircut,
        maintenance_margin_rate=maintenance_margin_rate,
        liquidation_fee_rate=liquidation_fee_rate,
    )

    years = len(funding) / PAYMENTS_PER_YEAR
    dd = (equity / equity.cummax() - 1.0).min()
    ann_all = pnl.mean() * PAYMENTS_PER_YEAR
    n_cycles = int(switches.sum()) // 2
    return {
        "equity": equity,
        "cagr": (equity.iloc[-1] / initial_capital) ** (1 / years) - 1,
        "ann_return_simple": ann_all,
        "sharpe": sharpe_ratio(daily_returns(equity)),
        "max_drawdown": dd,
        "exposure": applied.mean(),
        "cycles": n_cycles,
        "years": years,
        "leverage": leverage,
        "borrow_rate_ann": borrow_rate_ann,
        "borrow_rate_ann_mean": float(borrow_rates.mean()),
        "basis_mode": basis_mode,
        "basis_return_ann": float(basis_component.mean() * PAYMENTS_PER_YEAR),
        "collateral_haircut": collateral_haircut,
        "maintenance_margin_rate": maintenance_margin_rate,
        "liquidation_fee_rate": liquidation_fee_rate,
        "liquidated": liquidated,
        "liquidation_ts": liquidation_ts,
        "real_market_inputs_complete": bool(
            basis_mode == "observed" and borrow_rate_ann_series is not None
        ),
        #: coût de financement annualisé effectivement supporté (0 si levier 1)
        "borrow_cost_ann": float(borrow_component.mean() * PAYMENTS_PER_YEAR),
    }
