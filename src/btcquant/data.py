"""Téléchargement et cache des données OHLCV via ccxt.

- Pagination automatique (limite exchange ~1000 bougies par appel).
- Cache CSV incrémental : on ne retélécharge que les bougies manquantes.
- La dernière bougie (potentiellement incomplète) est toujours écartée.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import ccxt
import pandas as pd

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]


def _make_exchange(exchange_id: str) -> ccxt.Exchange:
    klass = getattr(ccxt, exchange_id)
    return klass({"enableRateLimit": True, "timeout": 30_000})


def _cache_path(data_dir: str | Path, exchange_id: str, symbol: str, timeframe: str) -> Path:
    safe_symbol = symbol.replace("/", "-")
    return Path(data_dir) / f"{exchange_id}_{safe_symbol}_{timeframe}.csv"


def _fetch_paginated(
    ex: ccxt.Exchange, symbol: str, timeframe: str, since_ms: int
) -> pd.DataFrame:
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
            break  # l'exchange ne progresse plus, on évite la boucle infinie
        cursor = last_ts + tf_ms
        log.info("  … %s bougies, jusqu'à %s", len(all_rows), pd.Timestamp(last_ts, unit="ms", tz="UTC"))
    df = pd.DataFrame(all_rows, columns=["timestamp", *COLUMNS])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    return df.astype(float)


def load_ohlcv(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    since: str,
    data_dir: str | Path = "data",
    refresh: bool = True,
) -> pd.DataFrame:
    """Charge les OHLCV depuis le cache, complète depuis l'exchange si besoin.

    Retourne un DataFrame indexé en UTC, colonnes open/high/low/close/volume,
    dernière bougie incomplète exclue.
    """
    path = _cache_path(data_dir, exchange_id, symbol, timeframe)
    cached: pd.DataFrame | None = None
    if path.exists():
        cached = pd.read_csv(path, index_col="timestamp", parse_dates=True)
        cached.index = pd.DatetimeIndex(cached.index, tz="UTC") if cached.index.tz is None else cached.index

    if refresh:
        ex = _make_exchange(exchange_id)
        if cached is not None and not cached.empty:
            start_ms = int(cached.index[-1].timestamp() * 1000)
        else:
            start_ms = int(pd.Timestamp(since, tz="UTC").timestamp() * 1000)
        log.info("Téléchargement %s %s %s depuis %s", exchange_id, symbol, timeframe,
                 pd.Timestamp(start_ms, unit="ms", tz="UTC"))
        fresh = _fetch_paginated(ex, symbol, timeframe, start_ms)
        df = pd.concat([cached, fresh]) if cached is not None else fresh
        df = df[~df.index.duplicated(keep="last")].sort_index()
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index_label="timestamp")
    elif cached is not None:
        df = cached
    else:
        raise FileNotFoundError(f"Aucun cache pour {symbol} {timeframe} et refresh=False")

    return df.iloc[:-1]  # écarte la bougie en cours, potentiellement incomplète


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Rééchantillonne des bougies 1h vers 4h/1d, etc. (`rule` façon pandas : '4h', '1D')."""
    out = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna(subset=["open", "close"])


TIMEFRAME_TO_PANDAS = {"1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "12h": "12h", "1d": "1D"}
