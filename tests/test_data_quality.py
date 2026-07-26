from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from btcquant.execution.data_quality import MarketDataInvalid, validate_closed_ohlcv

NOW = pd.Timestamp("2026-07-25T12:00:20Z")
TF = 3600


def _frame() -> pd.DataFrame:
    index = pd.date_range("2026-07-25T08:00:00Z", periods=5, freq="1h")
    return pd.DataFrame(
        {
            "open": [100, 101, 102, 103, 104],
            "high": [102, 103, 104, 105, 106],
            "low": [99, 100, 101, 102, 103],
            "close": [101, 102, 103, 104, 105],
            "volume": [10, 11, 12, 13, 14],
        },
        index=index,
    )


def test_current_candle_is_removed_and_latest_closed_is_kept():
    result = validate_closed_ohlcv(_frame(), timeframe_seconds=TF, now=NOW)

    assert result.index[-1] == pd.Timestamp("2026-07-25T11:00:00Z")
    assert len(result) == 4


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda df: df.drop(df.index[1]), "Trou"),
        (lambda df: df.iloc[::-1], "désordonnés"),
        (lambda df: df.assign(close=np.nan), "NaN"),
        (lambda df: df.assign(low=200), "high/low"),
        (lambda df: df.assign(volume=-1), "Volume"),
    ],
)
def test_invalid_market_data_fails_closed(mutate, message):
    with pytest.raises(MarketDataInvalid, match=message):
        validate_closed_ohlcv(mutate(_frame()), timeframe_seconds=TF, now=NOW)


def test_stale_latest_bar_fails_closed():
    stale = _frame().iloc[:-2]

    with pytest.raises(MarketDataInvalid, match="périmée"):
        validate_closed_ohlcv(stale, timeframe_seconds=TF, now=NOW)
