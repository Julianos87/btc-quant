from __future__ import annotations

import pandas as pd
import pytest

from btcquant.backtest import BacktestEngine
from btcquant.research.walkforward import walk_forward
from btcquant.research.strategies import TrendSwing
from btcquant.research.governance import GovernanceError


def test_legacy_walkforward_entrypoint_requires_governed_spec() -> None:
    frame = pd.DataFrame(
        {
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [1.0, 1.0],
        },
        index=pd.date_range("2026-01-01T00:00Z", periods=2, freq="h"),
    )
    with pytest.raises(GovernanceError, match="legacy walk-forward"):
        walk_forward(
            TrendSwing,
            frame,
            {"donchian": [55]},
            BacktestEngine(),
            train_bars=1,
            test_bars=1,
        )
