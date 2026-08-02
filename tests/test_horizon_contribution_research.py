from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scripts.research_btc_horizon_contribution import (
    _all_combinations,
    _entry_overlap,
    _normalized_weights,
)


def test_all_non_empty_horizon_combinations_are_compared_once():
    assert _all_combinations() == (
        (20,),
        (55,),
        (100,),
        (20, 55),
        (20, 100),
        (55, 100),
        (20, 55, 100),
    )


def test_subset_weights_keep_total_trend_capital_constant():
    cfg = {
        "strategies": {
            "a": {
                "enabled": True,
                "type": "trend_ls",
                "capital_fraction": 0.3333,
                "params": {"donchian": 20},
            },
            "b": {
                "enabled": True,
                "type": "trend_ls",
                "capital_fraction": 0.3334,
                "params": {"donchian": 100},
            },
        }
    }

    weights = _normalized_weights(cfg, (20, 100))

    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights[100] > weights[20]


def test_entry_overlap_counts_only_same_bar_and_same_direction():
    ts1 = datetime(2026, 1, 1, tzinfo=UTC)
    ts2 = datetime(2026, 1, 2, tzinfo=UTC)
    left = [
        SimpleNamespace(entry_time=ts1, direction=1),
        SimpleNamespace(entry_time=ts2, direction=-1),
    ]
    right = [
        SimpleNamespace(entry_time=ts1, direction=1),
        SimpleNamespace(entry_time=ts2, direction=1),
    ]

    overlap = _entry_overlap(left, right)

    assert overlap["shared_entries"] == 1
    assert overlap["share_of_smaller"] == pytest.approx(0.5)
    assert overlap["jaccard"] == pytest.approx(1 / 3)
