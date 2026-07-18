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

Limites modélisées honnêtement : le PnL de base (écart spot-perp) est supposé
nul en moyenne (vrai en tenant jusqu'à convergence), le risque de marge
intra-position n'est pas simulé — d'où le plafond de levier recommandé (3x).
"""

from __future__ import annotations

import logging
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

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


def add_funding_columns(
    df: pd.DataFrame, funding_8h: pd.Series, pandas_freq: str
) -> pd.DataFrame:
    """Ajoute les DEUX colonnes de funding attendues par le moteur, qui n'ont
    ni la même unité ni le même usage — les confondre était la cause de l'écart
    backtest/paper documenté jusqu'en juillet 2026 :

    - ``funding_rate`` : **somme des paiements tombant dans la barre**, servant
      au P&L. Sur des barres 4 h et un funding 8 h, une barre sur deux vaut
      exactement 0 (les paiements tombent à 00/08/16 UTC).
    - ``funding`` : **dernier taux 8 h connu** à la clôture de la barre, servant
      au filtre d'entrée de `TrendLS`. C'est l'équivalent backtest de
      `Venue.funding_rate_8h()` côté live.

    Alimenter le filtre avec ``funding_rate`` le rendrait actif une barre sur
    deux seulement, et sous-estimerait le taux d'un facteur deux sur les autres.

    Pas de look-ahead : le taux payé à t est connu à t, et la barre étiquetée t
    ne clôture qu'en t+1 barre.
    """
    out = df.copy()
    per_bar = funding_8h.resample(pandas_freq, label="left", closed="left").sum()
    out["funding_rate"] = per_bar.reindex(out.index).fillna(0.0)
    out["funding"] = funding_8h.reindex(out.index, method="ffill")
    return out


#: Coût annuel par défaut des fonds empruntés pour financer la jambe spot.
#: Ordre de grandeur du taux USDT en margin isolé/croisé sur les grandes
#: plateformes : très variable (il grimpe justement quand le funding est élevé,
#: puisque les deux traduisent la même demande de levier). À surcharger avec le
#: taux réellement consenti par la plateforme utilisée.
DEFAULT_BORROW_RATE_ANN = 0.10


def backtest_carry(
    funding: pd.Series,
    leverage: float = 3.0,
    fee_rate: float = 0.0005,
    slippage_bps: float = 5.0,
    enter_ann: float = 0.05,
    exit_ann: float = 0.0,
    smooth_days: int = 7,
    initial_capital: float = 10_000.0,
    borrow_rate_ann: float = DEFAULT_BORROW_RATE_ANN,
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
    if leverage < 1.0:
        raise ValueError("leverage < 1 non modélisé (sous-emploi du capital)")
    borrow_per_period = (leverage - 1.0) * borrow_rate_ann / PAYMENTS_PER_YEAR
    smooth = funding.rolling(smooth_days * PAYMENTS_PER_DAY, min_periods=PAYMENTS_PER_DAY).mean()
    smooth_ann = smooth * PAYMENTS_PER_YEAR

    # signal décidé en t, appliqué en t+1 : aucun look-ahead
    in_pos = pd.Series(False, index=funding.index)
    state = False
    for i, v in enumerate(smooth_ann):
        if not np.isnan(v):
            if not state and v > enter_ann:
                state = True
            elif state and v < exit_ann:
                state = False
        in_pos.iloc[i] = state
    applied = in_pos.shift(1, fill_value=False)

    cost_per_switch = 2 * (fee_rate + slippage_bps / 10_000.0) * leverage  # 2 jambes
    switches = applied != applied.shift(1, fill_value=False)
    # le coût d'emprunt court uniquement quand la position est ouverte : hors
    # position, rien n'est emprunté puisque rien n'est immobilisé en spot.
    pnl = (
        applied * funding * leverage
        - applied * borrow_per_period
        - switches * cost_per_switch
    )
    equity = initial_capital * (1.0 + pnl).cumprod()

    years = len(funding) / PAYMENTS_PER_YEAR
    dd = (equity / equity.cummax() - 1.0).min()
    ann_all = pnl.mean() * PAYMENTS_PER_YEAR
    vol = pnl.std() * np.sqrt(PAYMENTS_PER_YEAR)
    n_cycles = int(switches.sum()) // 2
    return {
        "equity": equity,
        "cagr": (equity.iloc[-1] / initial_capital) ** (1 / years) - 1,
        "ann_return_simple": ann_all,
        "sharpe": ann_all / vol if vol > 0 else np.nan,
        "max_drawdown": dd,
        "exposure": applied.mean(),
        "cycles": n_cycles,
        "years": years,
        "leverage": leverage,
        "borrow_rate_ann": borrow_rate_ann,
        #: coût de financement annualisé effectivement supporté (0 si levier 1)
        "borrow_cost_ann": (leverage - 1.0) * borrow_rate_ann * float(applied.mean()),
    }
