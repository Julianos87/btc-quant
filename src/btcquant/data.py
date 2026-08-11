"""Téléchargement, cache et intégrité temporelle des données OHLCV."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import ccxt
import pandas as pd

from .data_integrity import GapPolicy, cadence_report, validate_cadence

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]


def _make_exchange(exchange_id: str) -> ccxt.Exchange:
    klass = getattr(ccxt, exchange_id)
    return klass({"enableRateLimit": True, "timeout": 30_000})


def _cache_path(data_dir: str | Path, exchange_id: str, symbol: str, timeframe: str) -> Path:
    safe_symbol = symbol.replace("/", "-")
    return Path(data_dir) / f"{exchange_id}_{safe_symbol}_{timeframe}.csv"


def _fetch_paginated(ex: ccxt.Exchange, symbol: str, timeframe: str, since_ms: int) -> pd.DataFrame:
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    all_rows: list[list] = []
    cursor = since_ms
    now_ms = ex.milliseconds()
    while cursor < now_ms:
        for attempt in range(5):
            try:
                batch = ex.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
                break
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                wait = 2**attempt
                log.warning("Erreur réseau (%s), retry dans %ss", e.__class__.__name__, wait)
                time.sleep(wait)
        else:
            raise RuntimeError("Échec du téléchargement après 5 tentatives")
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= cursor and len(batch) > 1:
            break
        cursor = last_ts + tf_ms
        log.info(
            "  … %s bougies, jusqu'à %s", len(all_rows), pd.Timestamp(last_ts, unit="ms", tz="UTC")
        )
    df = pd.DataFrame(all_rows, columns=["timestamp", *COLUMNS])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    raw_index = pd.DatetimeIndex(df["timestamp"])
    if raw_index.has_duplicates:
        raise ValueError("DUPLICATE dans les bougies OHLCV téléchargées")
    if not raw_index.is_monotonic_increasing:
        raise ValueError("OUT_OF_ORDER dans les bougies OHLCV téléchargées")
    df = df.set_index("timestamp")
    result = df.astype(float)
    if not result[COLUMNS].notna().all().all():
        raise ValueError("NaN dans les bougies OHLCV téléchargées")
    return result


def load_ohlcv(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    since: str,
    data_dir: str | Path = "data",
    refresh: bool = True,
    gap_policy: GapPolicy | str = GapPolicy.ALLOW_REPORTED,
) -> pd.DataFrame:
    """Charge le cache; les appels historiques peuvent exiger une cadence complète."""
    path = _cache_path(data_dir, exchange_id, symbol, timeframe)
    cached: pd.DataFrame | None = None
    if path.exists():
        cached = pd.read_csv(path, index_col="timestamp", parse_dates=True)
        cached_index = pd.DatetimeIndex(cached.index)
        if cached_index.has_duplicates:
            raise ValueError("DUPLICATE dans le cache OHLCV")
        if not cached_index.is_monotonic_increasing:
            raise ValueError("OUT_OF_ORDER dans le cache OHLCV")
        if not cached[COLUMNS].notna().all().all():
            raise ValueError("NaN dans le cache OHLCV")
        if cached_index.tz is None:
            raise ValueError("Index OHLCV sans fuseau UTC")
        cached.index = cached_index.tz_convert("UTC")

    if refresh:
        ex = _make_exchange(exchange_id)
        if cached is not None and not cached.empty:
            start_ms = int(cached.index[-1].timestamp() * 1000)
        else:
            start_ms = int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)
        log.info(
            "Téléchargement %s %s %s depuis %s",
            exchange_id,
            symbol,
            timeframe,
            pd.Timestamp(start_ms, unit="ms", tz="UTC"),
        )
        fresh = _fetch_paginated(ex, symbol, timeframe, start_ms)
        if cached is not None and not cached.empty and not fresh.empty:
            if fresh.index[0] == cached.index[-1]:
                cached = cached.iloc[:-1]
            elif fresh.index[0] <= cached.index[-1]:
                raise ValueError("OVERLAP/OUT_OF_ORDER entre le cache et le téléchargement")
        df = pd.concat([cached, fresh]) if cached is not None else fresh
        if not df[COLUMNS].notna().all().all():
            raise ValueError("NaN dans les bougies OHLCV")
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index_label="timestamp")
    elif cached is not None:
        df = cached
    else:
        raise FileNotFoundError(f"Aucun cache pour {symbol} {timeframe} et refresh=False")

    result = df.iloc[:-1]
    report = validate_cadence(result.index, timeframe, gap_policy=gap_policy)
    result.attrs["cadence_report"] = report.to_dict()
    return result


def resample(
    df: pd.DataFrame,
    rule: str,
    *,
    source_frequency: str | None = None,
) -> pd.DataFrame:
    """Agrège seulement les fenêtres dont toutes les observations sources existent."""
    if df.empty:
        result = df.copy()
        result.attrs["cadence_report"] = cadence_report(
            df.index, source_frequency or rule
        ).to_dict()
        return result
    index = pd.DatetimeIndex(df.index)
    if index.tz is None:
        raise ValueError("Le resampling historique exige un index timezone-aware")
    if str(index.tz) != "UTC":
        df = df.copy()
        df.index = index.tz_convert("UTC")
        index = pd.DatetimeIndex(df.index)
    if source_frequency is None:
        deltas = index.to_series().diff().dropna()
        positive = deltas[deltas > pd.Timedelta(0)]
        if positive.empty:
            raise ValueError("Impossible d'inférer la fréquence source")
        source_delta = positive.mode().iloc[0]
        source_frequency = str(source_delta)
    report = validate_cadence(
        index,
        source_frequency,
        gap_policy=GapPolicy.ALLOW_REPORTED,
    )
    source_delta = report.expected_delta
    window_delta = pd.Timedelta(rule)
    if window_delta.total_seconds() % source_delta.total_seconds() != 0:
        raise ValueError("La fenêtre de resampling n'est pas multiple de la cadence source")
    source_bars_per_window = int(window_delta / source_delta)
    aggregates: list[pd.Series] = []
    dropped_windows: list[pd.Timestamp] = []
    first_window = index.min().floor(rule)
    for window_start in pd.date_range(first_window, index.max(), freq=rule):
        expected = pd.date_range(
            window_start,
            periods=source_bars_per_window,
            freq=source_delta,
            tz="UTC",
        )
        mask = (index >= window_start) & (index < window_start + window_delta)
        group = df.loc[mask]
        if len(group) != source_bars_per_window or not group.index.equals(expected):
            dropped_windows.append(window_start)
            continue
        aggregates.append(
            pd.Series(
                {
                    "open": group["open"].iloc[0],
                    "high": group["high"].max(),
                    "low": group["low"].min(),
                    "close": group["close"].iloc[-1],
                    "volume": group["volume"].sum(),
                },
                name=window_start,
            )
        )
    out = pd.DataFrame(aggregates)
    anomalies = list(report.anomalies)
    if dropped_windows:
        anomalies.append("PARTIAL_WINDOW")
    report_dict = report.to_dict()
    report_dict["anomalies"] = sorted(set(anomalies))
    report_dict["dropped_windows"] = [value.isoformat() for value in dropped_windows]
    report_dict["source_frequency"] = source_frequency
    report_dict["window_frequency"] = rule
    out.attrs["cadence_report"] = report_dict
    return out


TIMEFRAME_TO_PANDAS = {"1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "12h": "12h", "1d": "1D"}
