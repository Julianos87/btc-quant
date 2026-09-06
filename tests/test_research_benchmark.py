from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import numpy as np

from scripts.run_research_benchmark import (
    _bootstrap,
    _lookahead_control,
    _quality_ohlcv,
)
from btcquant.research.carry_v2_replay import ReplayPolicy, replay_policy


def test_dataset_quality_manifest_hash_and_gap_are_explicit(tmp_path: Path) -> None:
    path = tmp_path / "ohlcv.csv"
    timestamps = pd.to_datetime(["2030-01-01T00:00:00Z", "2030-01-01T02:00:00Z"], utc=True)
    pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1.0, 1.0],
        }
    ).to_csv(path, index=False)
    manifest = _quality_ohlcv(path)
    assert len(manifest["sha256"]) == 64
    assert manifest["gaps"] == 1
    duplicate = pd.read_csv(path)
    duplicate.loc[1, "timestamp"] = duplicate.loc[0, "timestamp"]
    duplicate.to_csv(path, index=False)
    with pytest.raises(ValueError, match="duplicate"):
        _quality_ohlcv(path)


def test_lookahead_control_passes_and_detects_contamination() -> None:
    result = _lookahead_control()
    assert result == {
        "causal_prefix_pass": True,
        "future_contamination_detected": True,
        "status": "PASS",
    }


def test_block_bootstrap_is_deterministic() -> None:
    index = pd.date_range("2030-01-01", periods=40, freq="D", tz="UTC")
    equity = pd.Series((1.0 + 0.001) ** np.arange(40), index=index)
    assert _bootstrap(equity, 7, blocks=20, block_len=3) == _bootstrap(
        equity, 7, blocks=20, block_len=3
    )


def test_carry_replay_filter_is_causal_and_can_block_open() -> None:
    timestamps = pd.date_range("2030-01-01", periods=48, freq="h", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "spot_price": 100.0,
            "perp_price": 100.0,
            "native_rate": 0.01,
            "reference_price": 100.0,
            "reference_price_timestamp": timestamps - pd.Timedelta("1min"),
            "vol_ann": 9.0,
            "basis_pct": 0.0,
        }
    )
    result = replay_policy(
        frame,
        ReplayPolicy(smooth_days=1, enter_ann=0.01),
        entry_filter=lambda row: float(row["vol_ann"]) < 1.0,
    )
    assert result["entries"] == 0
