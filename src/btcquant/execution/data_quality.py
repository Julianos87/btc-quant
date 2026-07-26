"""Invariants bloquants des bougies utilisées pour une décision."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .errors import ExecutionError


class MarketDataInvalid(ExecutionError):
    """Les données ne permettent pas de prendre une décision sûre."""


def validate_closed_ohlcv(
    frame: pd.DataFrame,
    *,
    timeframe_seconds: int,
    now: pd.Timestamp,
) -> pd.DataFrame:
    """Retient les bougies closes et refuse trous, doublons ou prix impossibles."""

    required = ["open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in frame]
    if missing:
        raise MarketDataInvalid(f"Colonnes OHLCV absentes : {missing}")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise MarketDataInvalid("Index OHLCV non temporel")
    if frame.index.tz is None:
        raise MarketDataInvalid("Index OHLCV sans fuseau UTC")
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise MarketDataInvalid("Horodatages OHLCV dupliqués ou désordonnés")

    current_start = now.floor(f"{timeframe_seconds}s")
    closed = frame.loc[frame.index < current_start].copy()
    if len(closed) < 2:
        raise MarketDataInvalid("Historique OHLCV clos insuffisant")

    values = closed[required].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise MarketDataInvalid("OHLCV contient NaN ou valeur infinie")
    if (closed[["open", "high", "low", "close"]] <= 0).any().any():
        raise MarketDataInvalid("Prix OHLCV nul ou négatif")
    if (closed["volume"] < 0).any():
        raise MarketDataInvalid("Volume OHLCV négatif")
    if (closed["high"] < closed[["open", "close", "low"]].max(axis=1)).any() or (
        closed["low"] > closed[["open", "close", "high"]].min(axis=1)
    ).any():
        raise MarketDataInvalid("Invariants high/low OHLCV invalides")

    expected_delta = pd.Timedelta(seconds=timeframe_seconds)
    deltas = closed.index.to_series().diff().dropna()
    if not deltas.eq(expected_delta).all():
        raise MarketDataInvalid("Trou ou intervalle irrégulier dans les bougies")
    expected_latest = current_start - expected_delta
    if closed.index[-1] != expected_latest:
        age = current_start - closed.index[-1]
        raise MarketDataInvalid(f"Dernière bougie close périmée ({age})")
    return closed
