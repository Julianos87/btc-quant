from __future__ import annotations

import pandas as pd
import pytest

from btcquant.reporting.analytics import (
    best_and_worst_day,
    carry_funding_curve,
    combined_equity,
    deposits_total,
    live_metrics,
    net_of_flows,
    trade_analytics,
)


def _series(values: list[float], *, start: str = "2030-01-01") -> pd.Series:
    index = pd.date_range(start, periods=len(values), freq="1D", tz="UTC")
    return pd.Series(values, index=index, dtype=float)


def test_flows_are_aggregated_and_removed_before_combining_equity():
    trend = _series([6000.0, 6060.0, 6060.0])
    carry = _series([4000.0, 4040.0, 4040.0])
    flow_time = trend.index[1]
    flows = pd.DataFrame(
        [
            {"ts": flow_time, "trend_flow": 50.0, "carry_flow": 30.0},
            {"ts": flow_time, "trend_flow": 10.0, "carry_flow": 10.0},
        ]
    )

    assert deposits_total(flows) == pytest.approx(100.0)
    assert net_of_flows(trend, flows, "trend_flow").tolist() == [6000.0, 6000.0, 6000.0]
    combined = combined_equity(trend, carry, flows, exclude_flows=True)
    assert combined.iloc[0] == pytest.approx(10_000.0)
    assert combined.iloc[-1] == pytest.approx(10_000.0)


def test_metrics_refuse_short_history_and_handle_losing_capital():
    short = _series([10_000.0, 9_900.0])
    assert live_metrics(short, 10_000.0)["max_dd"] is None

    losing = _series([10_000.0, 10_100.0, 9_000.0], start="2029-01-01")
    metrics = live_metrics(losing, 10_000.0)
    assert metrics["max_dd"] == pytest.approx(9_000 / 10_100 - 1)
    assert metrics["cur_dd"] == pytest.approx(9_000 / 10_100 - 1)
    assert metrics["cagr"] is not None


def test_trade_records_follow_exit_chronology_not_input_order():
    trades = pd.DataFrame(
        [
            {
                "strategy": "trend_ls_2",
                "direction": -1,
                "pnl": -5.0,
                "exit_ts": "2030-01-04",
            },
            {
                "strategy": "trend_ls_1",
                "direction": 1,
                "pnl": 10.0,
                "exit_ts": "2030-01-01",
            },
            {
                "strategy": "trend_ls_1",
                "direction": 1,
                "pnl": 20.0,
                "exit_ts": "2030-01-02",
            },
            {
                "strategy": "trend_ls_2",
                "direction": -1,
                "pnl": -30.0,
                "exit_ts": "2030-01-03",
            },
        ]
    )

    breakdown, records = trade_analytics(trades)

    assert len(breakdown["by_strategy"]) == 2
    assert records["biggest_win"] == 20.0
    assert records["biggest_loss"] == -30.0
    assert records["longest_win_streak"] == 2
    assert records["longest_loss_streak"] == 2


def test_carry_funding_excludes_deposits_and_daily_records_are_stable():
    carry = _series([4000.0, 4050.0, 4150.0])
    flows = pd.DataFrame([{"ts": carry.index[2], "trend_flow": 150.0, "carry_flow": 100.0}])

    total, curve = carry_funding_curve(carry, flows, 4000.0)
    assert total == pytest.approx(50.0)
    assert curve[-1][1] == pytest.approx(50.0)

    best, worst = best_and_worst_day(_series([100.0, 110.0, 99.0, 108.9]))
    assert best == pytest.approx(0.10)
    assert worst == pytest.approx(-0.10)


def test_missing_columns_fail_safely_for_empty_or_legacy_inputs():
    malformed = pd.DataFrame([{"unexpected": 1}])
    empty_equity = pd.Series(dtype=float)

    assert deposits_total(malformed) == 0.0
    assert net_of_flows(empty_equity, malformed, "carry_flow").empty
    assert trade_analytics(malformed) == (
        {"by_strategy": [], "by_direction": []},
        {},
    )
