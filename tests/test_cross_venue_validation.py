from __future__ import annotations

import pandas as pd
import pytest

from scripts.run_cross_venue_validation import _validate_ohlcv


def _bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-09-06T00:00:00Z", "2026-09-06T01:00:00Z"], utc=True),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [10.0, 11.0],
        }
    )


def test_cross_venue_quality_fails_closed_on_duplicate_rows() -> None:
    frame = pd.concat([_bars(), _bars().iloc[[1]]], ignore_index=True)
    with pytest.raises(RuntimeError, match="duplicate"):
        _validate_ohlcv(frame, venue="fixture", cutoff=pd.Timestamp("2026-09-07T00:00:00Z"))


def test_cross_venue_quality_rejects_misaligned_timestamp() -> None:
    frame = _bars()
    frame.loc[1, "timestamp"] = pd.Timestamp("2026-09-06T01:30:00Z")
    with pytest.raises(RuntimeError, match="aligned"):
        _validate_ohlcv(frame, venue="fixture", cutoff=pd.Timestamp("2026-09-07T00:00:00Z"))
