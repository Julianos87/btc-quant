"""Module cash-and-carry : encaisser le funding des perpétuels, delta-neutre.

Structure documentée (basis trade classique) : long spot + short perpétuel de
même taille. Le risque de prix s'annule ; on encaisse les événements de
funding réellement publiés par la venue (horaire sur Hyperliquid, 8 h sur le
contrat Binance legacy).

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
from typing import cast

import ccxt
import numpy as np
import pandas as pd

from .data_integrity import cadence_report
from .domain.carry_decision import decide_carry_payment
from .performance import daily_returns, sharpe_ratio

log = logging.getLogger(__name__)

# Legacy Binance research compatibility. Native Carry accounting below uses
# event timestamps and explicit venue cadence instead.
PAYMENTS_PER_DAY = 3
PAYMENTS_PER_YEAR = PAYMENTS_PER_DAY * 365
SECONDS_PER_YEAR = 365.25 * 24.0 * 60.0 * 60.0
BINANCE_USDM_FUNDING_FREQUENCY = pd.Timedelta("8h")
BINANCE_USDM_FUNDING_JITTER = pd.Timedelta(seconds=1)
FUNDING_PRICE_ASOF_INTERVAL = pd.Timedelta("1h")
FUNDING_PRICE_ASOF_TOLERANCE = pd.Timedelta(seconds=1)
FUNDING_PRICE_ASOF_SOURCE = "HYPERLIQUID_PREVIOUS_1H_CLOSE_APPROXIMATION"


@dataclass(frozen=True)
class FundingResolution:
    """Funding input with an explicit origin for a quantitative result."""

    series: pd.Series | None
    source: str
    rate: float | None = None


@dataclass(frozen=True)
class FundingSmoothingResult:
    """Fenêtre de funding calculée sur une durée calendrier explicite."""

    annualized: pd.Series
    coverage: pd.DataFrame
    expected_interval_seconds: float


def normalize_funding_events(
    series: pd.Series,
    *,
    context: str = "funding",
) -> pd.Series:
    """Normalise les événements en UTC sans supprimer une anomalie."""

    raw_index = pd.DatetimeIndex(series.index)
    if raw_index.tz is None:
        raise ValueError(f"{context}: index sans fuseau UTC")
    index = raw_index.tz_convert("UTC")
    if index.has_duplicates:
        raise ValueError(f"DUPLICATE dans {context}")
    if not index.is_monotonic_increasing:
        raise ValueError(f"OUT_OF_ORDER dans {context}")
    values = pd.to_numeric(series, errors="raise").astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError(f"NaN ou infini dans {context}")
    values.index = index
    return values


def _interval_seconds(
    index: pd.DatetimeIndex,
    funding_interval: str | pd.Timedelta | None,
) -> float:
    if funding_interval is not None:
        seconds = pd.Timedelta(funding_interval).total_seconds()
    elif len(index) >= 2:
        deltas = index.to_series().diff().dropna().dt.total_seconds().to_numpy()
        seconds = float(np.median(deltas))
    else:
        seconds = float("nan")
    if not np.isfinite(seconds) or seconds <= 0:
        raise ValueError("funding_interval doit être strictement positif et exploitable")
    return seconds


def funding_event_id(
    venue: str,
    instrument: str,
    timestamp: pd.Timestamp,
) -> str:
    """Identité stable d'un paiement, indépendante des retries et restarts."""

    ts = pd.Timestamp(timestamp)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return f"{venue.lower()}|{instrument}|{ts.isoformat()}"


def funding_slot(
    timestamp: pd.Timestamp,
    *,
    interval: str | pd.Timedelta = FUNDING_PRICE_ASOF_INTERVAL,
    tolerance: pd.Timedelta = FUNDING_PRICE_ASOF_TOLERANCE,
) -> pd.Timestamp:
    """Return the nominal UTC funding slot without changing the raw timestamp."""

    value = pd.Timestamp(timestamp)
    if value.tzinfo is None:
        raise ValueError("funding timestamp must be timezone-aware")
    value = value.tz_convert("UTC")
    interval_ns = pd.Timedelta(interval).value
    if interval_ns <= 0:
        raise ValueError("funding interval must be strictly positive")
    epoch_ns = pd.Timestamp("1970-01-01T00:00:00Z").value
    timestamp_ns = value.value
    slot_ns = epoch_ns + round((timestamp_ns - epoch_ns) / interval_ns) * interval_ns
    jitter = abs(timestamp_ns - slot_ns)
    if jitter > pd.Timedelta(tolerance).value:
        raise ValueError(
            f"funding timestamp jitter exceeds {pd.Timedelta(tolerance).total_seconds():g}s"
        )
    return pd.Timestamp(slot_ns, unit="ns", tz="UTC")


def funding_notional_prices_asof(
    funding: pd.Series,
    candle_closes: pd.Series,
    *,
    funding_interval: str | pd.Timedelta | None = None,
    tolerance: pd.Timedelta = FUNDING_PRICE_ASOF_TOLERANCE,
) -> tuple[pd.Series, pd.Series]:
    """Resolve funding prices from the previous completed candle only.

    Candle indexes are their opening timestamps. For a funding event in slot t,
    the selected value is the close of the candle opened at t-1h. This keeps
    the funding timestamp raw while making the price provenance explicitly
    as-of and prevents the close of the current, still-forming candle from
    leaking into the calculation.
    """

    events = normalize_funding_events(funding, context="funding price events")
    raw_index = pd.DatetimeIndex(candle_closes.index)
    if raw_index.tz is None:
        raise ValueError("funding price candles: index sans fuseau UTC")
    price_index = raw_index.tz_convert("UTC")
    if price_index.has_duplicates or not price_index.is_monotonic_increasing:
        raise ValueError("funding price candles must be unique and ordered")
    prices = pd.to_numeric(candle_closes, errors="raise").astype(float)
    prices.index = price_index
    if prices.isna().any() or not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any():
        raise ValueError("funding price candles must be finite and strictly positive")
    interval = pd.Timedelta(funding_interval or FUNDING_PRICE_ASOF_INTERVAL)
    if interval <= pd.Timedelta(0):
        raise ValueError("funding price interval must be strictly positive")

    resolved = pd.Series(np.nan, index=events.index, dtype=float)
    provenance = pd.Series(pd.NaT, index=events.index, dtype="datetime64[ns, UTC]")
    for event_timestamp in events.index:
        slot = funding_slot(
            event_timestamp, interval=funding_interval or pd.Timedelta("1h"), tolerance=tolerance
        )
        completed_open = slot - FUNDING_PRICE_ASOF_INTERVAL
        if completed_open in prices.index:
            resolved.loc[event_timestamp] = float(prices.loc[completed_open])
            provenance.loc[event_timestamp] = completed_open
    resolved.attrs["source"] = FUNDING_PRICE_ASOF_SOURCE
    return resolved, provenance


def elapsed_years_between(start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Durée économique réelle selon la convention calendrier du projet."""

    first = pd.Timestamp(start)
    last = pd.Timestamp(end)
    if first.tzinfo is None or last.tzinfo is None:
        raise ValueError("les timestamps économiques doivent être timezone-aware")
    seconds = (last.tz_convert("UTC") - first.tz_convert("UTC")).total_seconds()
    if seconds < 0:
        raise ValueError("la durée économique ne peut pas être négative")
    return seconds / SECONDS_PER_YEAR


def borrow_cost_for_intervals(
    timestamps: pd.DatetimeIndex | list[pd.Timestamp],
    *,
    borrow_notional: float | pd.Series,
    annual_borrow_rate: float | pd.Series,
    active: pd.Series | None = None,
) -> pd.Series:
    """Accrue le borrow sur les intervalles UTC réellement observés."""

    index = pd.DatetimeIndex(timestamps)
    if index.tz is None:
        raise ValueError("les timestamps de borrow doivent être timezone-aware")
    index = index.tz_convert("UTC")
    if index.has_duplicates or not index.is_monotonic_increasing:
        raise ValueError("les timestamps de borrow doivent être uniques et ordonnés")
    seconds = pd.Series(0.0, index=index)
    if len(index) > 1:
        seconds.iloc[1:] = index.to_series().diff().dt.total_seconds().iloc[1:]
    if (seconds < 0).any() or not np.isfinite(seconds.to_numpy()).all():
        raise ValueError("intervalles de borrow invalides")

    def aligned(value: float | pd.Series, name: str) -> pd.Series:
        if isinstance(value, pd.Series):
            result = value.astype(float).reindex(index)
        else:
            result = pd.Series(float(value), index=index)
        if result.isna().any() or not np.isfinite(result.to_numpy()).all():
            raise ValueError(f"{name} doit couvrir tous les timestamps")
        return result

    notional = aligned(borrow_notional, "borrow_notional")
    rate = aligned(annual_borrow_rate, "annual_borrow_rate")
    if (notional < 0).any() or (rate < 0).any():
        raise ValueError("notionnel et taux de borrow doivent être positifs ou nuls")
    exposure = (
        pd.Series(1.0, index=index) if active is None else active.astype(float).reindex(index)
    )
    if exposure.isna().any() or not np.isfinite(exposure.to_numpy()).all():
        raise ValueError("active doit couvrir tous les timestamps")
    return notional * rate * (seconds / SECONDS_PER_YEAR) * exposure


def funding_event_gaps(
    series: pd.Series,
    *,
    funding_interval: str | pd.Timedelta | None = None,
    tolerance_seconds: float = 1.0,
) -> dict:
    """Inventorie les événements manquants sans les reconstruire."""

    values = normalize_funding_events(series)
    interval_seconds = _interval_seconds(pd.DatetimeIndex(values.index), funding_interval)
    gaps: list[dict[str, object]] = []
    cadence_anomalies: list[dict[str, object]] = []
    interval_ns = int(round(interval_seconds * 1_000_000_000))
    epoch_ns = pd.Timestamp("1970-01-01T00:00:00Z").value
    timestamp_ns = np.asarray([timestamp.value for timestamp in values.index], dtype=np.int64)
    slot_ns = (
        epoch_ns + np.rint((timestamp_ns - epoch_ns) / interval_ns).astype(np.int64) * interval_ns
    )
    for timestamp, slot in zip(values.index, slot_ns, strict=True):
        jitter_seconds = abs(timestamp.value - int(slot)) / 1_000_000_000
        if jitter_seconds > tolerance_seconds:
            cadence_anomalies.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "jitter_seconds": jitter_seconds,
                }
            )
    for left, right in zip(values.index[:-1], values.index[1:], strict=True):
        delta_seconds = (right - left).total_seconds()
        slots = max(1, int(round(delta_seconds / interval_seconds)))
        expected_seconds = slots * interval_seconds
        jitter_seconds = abs(delta_seconds - expected_seconds)
        if jitter_seconds > tolerance_seconds:
            cadence_anomalies.append(
                {
                    "start": left.isoformat(),
                    "end": right.isoformat(),
                    "actual_seconds": delta_seconds,
                    "expected_seconds": expected_seconds,
                    "jitter_seconds": jitter_seconds,
                }
            )
        missing = max(0, slots - 1)
        if missing and delta_seconds > interval_seconds + tolerance_seconds:
            gaps.append(
                {
                    "start": left.isoformat(),
                    "end": right.isoformat(),
                    "missing_events": missing,
                    "elapsed_seconds": delta_seconds,
                }
            )
    return {
        "expected_interval_seconds": interval_seconds,
        "gap_groups": gaps,
        "cadence_anomalies": cadence_anomalies,
        "missing_events": sum(cast(int, gap["missing_events"]) for gap in gaps),
    }


def smooth_funding_events(
    funding: pd.Series,
    *,
    smooth_days: int,
    funding_interval: str | pd.Timedelta | None = None,
    min_coverage_ratio: float = 1.0,
) -> FundingSmoothingResult:
    """Moyenne de funding sur une durée calendrier, sans forward-fill.

    Un signal n'est disponible qu'après une histoire complète de smooth_days
    jours et lorsque tous les slots attendus de la fenêtre sont observés. Les
    anomalies restent visibles dans coverage.
    """

    if smooth_days < 1:
        raise ValueError("smooth_days doit être au moins égal à un jour")
    if not 0 < min_coverage_ratio <= 1:
        raise ValueError("min_coverage_ratio doit être dans ]0, 1]")
    values = normalize_funding_events(funding, context="funding smoothing")
    interval_seconds = _interval_seconds(pd.DatetimeIndex(values.index), funding_interval)
    duration = pd.Timedelta(days=smooth_days)
    expected_events = max(1, int(round(duration.total_seconds() / interval_seconds)))
    annualized = pd.Series(np.nan, index=values.index, dtype=float)
    rows: list[dict[str, object]] = []
    first_timestamp = values.index[0] if len(values) else None

    for timestamp in values.index:
        window_start = timestamp - duration
        mask = (values.index > window_start) & (values.index <= timestamp)
        window = values[mask]
        observed = int(len(window))
        missing = max(0, expected_events - observed)
        history_complete = (
            first_timestamp is not None
            and (timestamp - first_timestamp).total_seconds() >= duration.total_seconds()
        )
        coverage_ratio = min(1.0, observed / expected_events)
        status = (
            "OK"
            if history_complete and missing == 0 and coverage_ratio >= min_coverage_ratio
            else "INSUFFICIENT_FUNDING_HISTORY"
        )
        if status == "OK":
            annualized.loc[timestamp] = float(window.sum()) / (
                duration.total_seconds() / SECONDS_PER_YEAR
            )
        rows.append(
            {
                "window_start": window_start,
                "window_end": timestamp,
                "expected_duration_seconds": duration.total_seconds(),
                "expected_events": expected_events,
                "observed_events": observed,
                "missing_events": missing,
                "coverage_ratio": coverage_ratio,
                "status": status,
            }
        )
    coverage = pd.DataFrame(rows, index=values.index)
    return FundingSmoothingResult(annualized, coverage, interval_seconds)


def _validate_funding_series(
    series: pd.Series,
    *,
    expected_frequency: str | None = None,
    context: str = "funding",
) -> pd.Series:
    values = normalize_funding_events(series, context=context)
    if expected_frequency is not None and len(values) < 2:
        raise ValueError(f"Durée {context} insuffisante")
    if expected_frequency is not None:
        report = cadence_report(values.index, expected_frequency)
        if not report.is_valid:
            raise ValueError(f"Cadence {context} invalide : {', '.join(report.anomalies)}")
    return values


def funding_cache_path(symbol_perp: str, data_dir: str | Path = "data") -> Path:
    """Return the canonical Binance USDM funding-cache path for a perp symbol."""
    safe_symbol = symbol_perp.replace("/", "").replace(":", "_")
    return Path(data_dir) / f"binanceusdm_{safe_symbol}_funding.csv"


def _validate_binance_usdm_funding(series: pd.Series, *, context: str) -> pd.Series:
    """Validate Binance USDM's 8-hour slots while preserving raw timestamps.

    Binance's historical API may publish timestamps a few milliseconds away
    from the nominal UTC 00:00/08:00/16:00 slots. This venue-specific
    contract assigns each observation to its nearest slot, but never rewrites
    the timestamp stored in the returned series.
    """
    raw_index = pd.DatetimeIndex(series.index)
    if raw_index.tz is None:
        raise ValueError(f"{context}: index sans fuseau UTC")
    index = raw_index.tz_convert("UTC")
    if index.has_duplicates:
        raise ValueError(f"DUPLICATE dans {context}")
    if not index.is_monotonic_increasing:
        raise ValueError(f"OUT_OF_ORDER dans {context}")
    values = pd.to_numeric(series, errors="raise").astype(float)
    if not np.isfinite(values.to_numpy()).all():
        raise ValueError(f"NaN ou infini dans {context}")
    if len(index) < 2:
        raise ValueError(f"Durée {context} insuffisante")

    epoch_ns = pd.Timestamp("1970-01-01T00:00:00Z").value
    interval_ns = BINANCE_USDM_FUNDING_FREQUENCY.value
    timestamp_ns = index.tz_localize(None).to_numpy(dtype="datetime64[ns]").astype(np.int64)
    offsets = timestamp_ns - epoch_ns
    slot_ns = epoch_ns + ((offsets + interval_ns // 2) // interval_ns) * interval_ns
    jitter_ns = np.abs(offsets - (slot_ns - epoch_ns))
    if np.any(jitter_ns > BINANCE_USDM_FUNDING_JITTER.value):
        raise ValueError(f"Jitter de timestamp > 1 seconde dans {context}")

    if len(np.unique(slot_ns)) != len(slot_ns):
        raise ValueError(f"DUPLICATE de slot 8h dans {context}")
    slot_deltas = np.diff(slot_ns)
    if np.any(slot_deltas <= 0):
        raise ValueError(f"OUT_OF_ORDER de slots 8h dans {context}")
    if np.any(slot_deltas != interval_ns):
        raise ValueError(f"GAP de slot 8h dans {context}")

    # Keep the observed UTC instants, including their millisecond jitter.
    values.index = index
    return values


def load_funding(
    symbol_perp: str = "BTC/USDT:USDT",
    data_dir: str | Path = "data",
    refresh: bool = True,
) -> pd.Series:
    """Historique complet des taux de funding Binance (cache CSV incrémental)."""
    path = funding_cache_path(symbol_perp, data_dir)
    cached: pd.Series | None = None
    if path.exists():
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True, format="ISO8601")
        cached = _validate_binance_usdm_funding(df["rate"], context=str(path))

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
            cached = _validate_binance_usdm_funding(cached, context=str(path))
            path.parent.mkdir(parents=True, exist_ok=True)
            cached.to_frame().to_csv(path, index_label="ts")
    if cached is None:
        raise FileNotFoundError(f"Aucun cache funding pour {symbol_perp} et refresh=False")
    return _validate_binance_usdm_funding(cached, context=str(path))


def funding_mode_from_cli(mode: str, synthetic_rate: float | None) -> tuple[str, float | None]:
    """Resolve the explicit CLI funding contract shared by research scripts."""
    if mode == "synthetic":
        if synthetic_rate is None:
            raise ValueError("--synthetic-funding-rate est requis avec --funding-mode synthetic")
        return "SYNTHETIC_EXPLICIT", synthetic_rate
    if synthetic_rate is not None:
        raise ValueError("--synthetic-funding-rate est interdit avec --funding-mode real")
    return "REAL", None


def resolve_funding(
    symbol_perp: str,
    *,
    data_dir: str | Path = "data",
    refresh: bool,
    mode: str = "REAL",
    synthetic_rate: float | None = None,
) -> FundingResolution:
    """Resolve REAL funding or an explicitly requested synthetic constant."""
    normalized = mode.upper()
    if normalized == "REAL":
        return FundingResolution(
            series=load_funding(symbol_perp, data_dir=data_dir, refresh=refresh),
            source="real",
        )
    if normalized != "SYNTHETIC_EXPLICIT":
        raise ValueError("mode funding doit être REAL ou SYNTHETIC_EXPLICIT")
    if synthetic_rate is None or not np.isfinite(float(synthetic_rate)):
        raise ValueError("synthetic_rate doit être fini en mode SYNTHETIC_EXPLICIT")
    return FundingResolution(
        series=None,
        source="synthetic_constant",
        rate=float(synthetic_rate),
    )


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
    funding_8h = _validate_funding_series(funding_8h, context="funding fourni au backtest")
    funding_index = pd.DatetimeIndex(funding_8h.index)
    if len(out) and len(funding_8h) and funding_index[-1] > out.index[-1] + offset:
        raise ValueError("Funding après la dernière bougie OHLCV")
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
    funding_interval: str | pd.Timedelta | None = None,
) -> tuple[pd.Series, pd.Series, FundingSmoothingResult]:
    smoothing = smooth_funding_events(
        funding,
        smooth_days=smooth_days,
        funding_interval=funding_interval,
    )
    decision_state = pd.Series(False, index=funding.index)
    state = False
    for i, value in enumerate(smoothing.annualized):
        decision = decide_carry_payment(
            in_position=state,
            smooth_ann=float(value),
            enter_ann=enter_ann,
            exit_ann=exit_ann,
        )
        state = decision.in_position
        decision_state.iloc[i] = state
    exposure_state = decision_state.shift(1, fill_value=False)
    return decision_state, exposure_state, smoothing


def _carry_fixed_position_amounts(
    funding: pd.Series,
    decision_state: pd.Series,
    exposure_state: pd.Series,
    *,
    initial_capital: float,
    leverage: float,
    fee_rate: float,
    slippage_bps: float,
    borrow_rates: pd.Series,
    basis_return: pd.Series,
    funding_notional_price: pd.Series | None,
) -> tuple[
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    str,
    list[pd.Timestamp],
    list[pd.Timestamp],
]:
    """Calcule le P&L avec décision et exposition séparées.

    Une décision à t est exécutée après le paiement de t. La position est donc
    exposée sur (t, t+1] : elle ne reçoit pas le funding de son entrée, mais
    reçoit celui de l'événement suivant. Le borrow est facturé sur le même
    intervalle réel.
    """
    index = funding.index
    dt_seconds = pd.Series(0.0, index=index)
    if len(index) > 1:
        dt_seconds.iloc[1:] = index.to_series().diff().dt.total_seconds().iloc[1:].to_numpy()
    dt_years = dt_seconds / SECONDS_PER_YEAR
    prices = (
        funding_notional_price.astype(float).reindex(index)
        if funding_notional_price is not None
        else None
    )
    if prices is not None and (prices.dropna() < 0).any():
        raise ValueError("funding_notional_price doit être positif ou absent")
    if prices is not None and not np.isfinite(prices.dropna().to_numpy()).all():
        raise ValueError("funding_notional_price doit être fini ou absent")

    def price_at(timestamp: pd.Timestamp) -> float | None:
        if prices is None:
            return None
        value = float(prices.loc[timestamp])
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f"funding_notional_price indisponible as-of {timestamp.isoformat()}")
        return value

    notional_mode = "perp_qty_times_price" if prices is not None else "fixed_entry_notional"
    funding_amounts = pd.Series(0.0, index=index)
    borrow_amounts = pd.Series(0.0, index=index)
    basis_amounts = pd.Series(0.0, index=index)
    fee_amounts = pd.Series(0.0, index=index)
    slippage_amounts = pd.Series(0.0, index=index)
    pnl = pd.Series(0.0, index=index)
    equity = initial_capital
    entry_notional = 0.0
    borrow_principal = 0.0
    perp_qty = 0.0
    previous_decision = False
    entry_timestamps: list[pd.Timestamp] = []
    exit_timestamps: list[pd.Timestamp] = []

    for timestamp in index:
        decision_active = bool(decision_state.loc[timestamp])
        exposed = bool(exposure_state.loc[timestamp])
        opened = decision_active and not previous_decision
        closed = not decision_active and previous_decision
        event_price = (
            price_at(timestamp) if prices is not None and (exposed or opened or closed) else None
        )
        current_notional = (
            abs(perp_qty * event_price)
            if exposed and event_price is not None
            else entry_notional
            if exposed
            else 0.0
        )
        if exposed:
            funding_amounts.loc[timestamp] = current_notional * float(funding.loc[timestamp])
            borrow_amounts.loc[timestamp] = (
                borrow_principal
                * float(borrow_rates.loc[timestamp])
                * float(dt_years.loc[timestamp])
            )
            basis_amounts.loc[timestamp] = current_notional * float(basis_return.loc[timestamp])

        recurring_amount = (
            funding_amounts.loc[timestamp]
            - borrow_amounts.loc[timestamp]
            + basis_amounts.loc[timestamp]
        )
        equity_after_event = equity + recurring_amount

        if opened:
            entry_timestamps.append(timestamp)
            entry_notional = max(0.0, equity_after_event) * leverage
            borrow_principal = max(0.0, equity_after_event) * max(0.0, leverage - 1.0)
            if event_price is not None:
                perp_qty = entry_notional / event_price
            fee_amounts.loc[timestamp] = 2.0 * fee_rate * entry_notional
            slippage_amounts.loc[timestamp] = 2.0 * slippage_bps / 10_000.0 * entry_notional
        elif closed:
            exit_timestamps.append(timestamp)
            close_notional = (
                abs(perp_qty * event_price) if event_price is not None else entry_notional
            )
            fee_amounts.loc[timestamp] = 2.0 * fee_rate * close_notional
            slippage_amounts.loc[timestamp] = 2.0 * slippage_bps / 10_000.0 * close_notional

        net_amount = recurring_amount - fee_amounts.loc[timestamp] - slippage_amounts.loc[timestamp]
        equity_before = equity
        if equity_before <= 0:
            pnl.loc[timestamp] = -1.0
            equity = 0.0
        else:
            pnl.loc[timestamp] = net_amount / equity_before
            equity += net_amount

        if closed:
            entry_notional = 0.0
            borrow_principal = 0.0
            perp_qty = 0.0
        previous_decision = decision_active

    return (
        pnl,
        funding_amounts,
        borrow_amounts,
        basis_amounts,
        fee_amounts,
        slippage_amounts,
        dt_seconds,
        notional_mode,
        entry_timestamps,
        exit_timestamps,
    )


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
    funding_notional_price: pd.Series | None = None,
    funding_notional_price_source: str = "OHLC_APPROXIMATION",
    spot_price: pd.Series | None = None,
    perp_price: pd.Series | None = None,
    collateral_haircut: float = 0.0,
    maintenance_margin_rate: float = 0.0,
    liquidation_fee_rate: float = 0.0,
    funding_interval: str | pd.Timedelta | None = None,
) -> dict:
    """Backtest du cash-and-carry avec règles d'entrée/sortie sur funding lissé.

    Le levier a un coût. Un carry à levier L immobilise L×capital de spot alors
    que l'on ne dispose que du capital : les (L−1)×capital manquants sont
    empruntés et se paient, en continu, tant que la position est ouverte.
    Ignorer ce poste — ce que faisait le modèle jusqu'au 18/07/2026 — surestime
    massivement le rendement et produit un Sharpe irréaliste, le funding
    apparaissant alors comme un revenu sans contrepartie.

    Pour une position ouverte, le funding est le taux natif de l'événement et
    le borrow est ``(L−1) × borrow_rate_ann × dt_years`` sur l'intervalle UTC
    réellement écoulé depuis l'événement précédent.

    À ``leverage=1.0`` le terme d'emprunt s'annule : la position est intégralement
    financée par le capital, ce qui est le seul cas réalisable sans marge.
    """
    funding = normalize_funding_events(funding, context="funding carry")
    if funding.empty:
        raise ValueError("funding carry ne peut pas être vide")
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
    decision_state, exposure_state, smoothing = _carry_applied_positions(
        funding,
        smooth_days=smooth_days,
        enter_ann=enter_ann,
        exit_ann=exit_ann,
        funding_interval=funding_interval,
    )

    raw_price_candles = funding_notional_price
    resolved_funding_price = None
    funding_price_timestamps = None
    resolved_funding_price_source = "fixed_entry_notional"
    if raw_price_candles is not None:
        resolved_funding_price, funding_price_timestamps = funding_notional_prices_asof(
            funding,
            raw_price_candles,
            funding_interval=funding_interval or FUNDING_PRICE_ASOF_INTERVAL,
        )
        resolved_funding_price_source = str(
            resolved_funding_price.attrs.get("source", FUNDING_PRICE_ASOF_SOURCE)
        )

    (
        pnl,
        funding_amounts,
        borrow_amounts,
        basis_amounts,
        fee_amounts,
        slippage_amounts,
        dt_seconds,
        funding_notional_mode,
        entry_timestamps,
        exit_timestamps,
    ) = _carry_fixed_position_amounts(
        funding,
        decision_state,
        exposure_state,
        initial_capital=initial_capital,
        leverage=leverage,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        borrow_rates=borrow_rates,
        basis_return=basis_return,
        funding_notional_price=resolved_funding_price,
    )
    pnl, equity, exposure_state, liquidated, liquidation_ts = _apply_carry_liquidation(
        pnl.copy(),
        exposure_state.copy(),
        initial_capital=initial_capital,
        leverage=leverage,
        collateral_haircut=collateral_haircut,
        maintenance_margin_rate=maintenance_margin_rate,
        liquidation_fee_rate=liquidation_fee_rate,
    )
    if liquidated and liquidation_ts is not None:
        post_liquidation = funding.index > cast(pd.Timestamp, liquidation_ts)
        decision_state.loc[post_liquidation] = False
        for component in (
            funding_amounts,
            borrow_amounts,
            basis_amounts,
            fee_amounts,
            slippage_amounts,
        ):
            component.loc[post_liquidation] = 0.0
    switches = decision_state != decision_state.shift(1, fill_value=False)

    years = elapsed_years_between(funding.index[0], funding.index[-1])
    component_total = (
        float(funding_amounts.sum())
        - float(borrow_amounts.sum())
        + float(basis_amounts.sum())
        - float(fee_amounts.sum())
        - float(slippage_amounts.sum())
    )
    liquidation_adjustment = float(equity.iloc[-1] - initial_capital - component_total)
    fees_total = float(fee_amounts.sum())
    total_pnl = float(equity.iloc[-1] - initial_capital)
    active_seconds = float((dt_seconds * exposure_state.astype(float)).sum())
    elapsed_seconds = float(dt_seconds.sum())
    dd = (equity / equity.cummax() - 1.0).min()
    ann_all = float(pnl.sum() / years) if years > 0 else float("nan")
    n_cycles = int(switches.sum()) // 2
    return {
        "equity": equity,
        "cagr": (
            (equity.iloc[-1] / initial_capital) ** (1 / years) - 1 if years > 0 else float("nan")
        ),
        "ann_return_simple": ann_all,
        "sharpe": sharpe_ratio(daily_returns(equity)),
        "max_drawdown": dd,
        "exposure": active_seconds / elapsed_seconds if elapsed_seconds > 0 else 0.0,
        "cycles": n_cycles,
        "years": years,
        "elapsed_seconds": elapsed_seconds,
        "time_in_market_seconds": active_seconds,
        "time_in_market_years": active_seconds / SECONDS_PER_YEAR,
        "leverage": leverage,
        "borrow_rate_ann": borrow_rate_ann,
        "borrow_rate_ann_mean": float(borrow_rates.mean()),
        "basis_mode": basis_mode,
        "funding_notional_mode": funding_notional_mode,
        "funding_notional_price_source": resolved_funding_price_source,
        "funding_notional_price_timestamps": funding_price_timestamps,
        "decision_state": decision_state,
        "exposure_state": exposure_state,
        "entry_timestamps": [timestamp.isoformat() for timestamp in entry_timestamps],
        "exit_timestamps": [timestamp.isoformat() for timestamp in exit_timestamps],
        "basis_return_ann": (
            float(basis_amounts.sum() / initial_capital / years) if years > 0 else float("nan")
        ),
        "collateral_haircut": collateral_haircut,
        "maintenance_margin_rate": maintenance_margin_rate,
        "liquidation_fee_rate": liquidation_fee_rate,
        "liquidated": liquidated,
        "liquidation_ts": liquidation_ts,
        "real_market_inputs_complete": bool(
            basis_mode == "observed" and borrow_rate_ann_series is not None
        ),
        "funding_pnl": float(funding_amounts.sum()),
        "borrow_cost": float(borrow_amounts.sum()),
        "basis_pnl": float(basis_amounts.sum()),
        "fees": fees_total,
        "slippage": float(slippage_amounts.sum()),
        "total_pnl": total_pnl,
        "liquidation_adjustment": liquidation_adjustment,
        "borrow_cost_ann": (
            float(borrow_amounts.sum() / initial_capital / years) if years > 0 else 0.0
        ),
        "funding_interval_seconds": smoothing.expected_interval_seconds,
        "smoothing_coverage": smoothing.coverage,
        "smoothing_status": str(smoothing.coverage["status"].iloc[-1]),
        "funding_missing_events": int(smoothing.coverage["missing_events"].iloc[-1]),
    }
