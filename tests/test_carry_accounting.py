"""Contrats temporels du carry Hyperliquid et reprise des événements."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.carry import (  # noqa: E402
    backtest_carry,
    _carry_fixed_position_amounts,
    PAPER_CARRY_POLICY,
    SECONDS_PER_YEAR,
    borrow_cost_for_intervals,
    elapsed_years_between,
    funding_event_gaps,
    funding_event_id,
    normalize_funding_events,
    smooth_funding_events,
)
from btcquant.execution.carry_runner import CarryRunner  # noqa: E402


class _Venue:
    exchange_id = "hyperliquid"
    payments_per_day = 24
    native_funding_interval = pd.Timedelta("1h")

    def __init__(self, funding: pd.Series):
        self.funding = funding

    @property
    def payments_per_year(self) -> int:
        return self.payments_per_day * 365

    def funding_history_since(self, since: pd.Timestamp) -> pd.Series:
        return self.funding[self.funding.index >= pd.Timestamp(since)]

    def funding_notional_prices_since(
        self,
        since: pd.Timestamp,
        until: pd.Timestamp | None = None,
    ) -> pd.Series:
        index = self.funding.index[self.funding.index >= pd.Timestamp(since)]
        if until is not None:
            index = index[index <= pd.Timestamp(until)]
        prices = pd.Series(100_000.0, index=index, dtype=float)
        prices.attrs["source"] = "PAPER_RECENT_PRICE_APPROXIMATION"
        return prices


def _hourly(periods: int, rate: float = 0.001) -> pd.Series:
    index = pd.date_range("2030-01-01T00:00:00Z", periods=periods, freq="h")
    return pd.Series(rate, index=index, dtype=float)


def _mark_open(runner: CarryRunner, checkpoint: pd.Timestamp) -> None:
    runner.in_position = True
    runner.execution_state = "OPEN"
    runner.entry_equity = runner.equity
    runner.entry_timestamp = checkpoint
    runner.entry_price = 100_000.0
    runner.spot_notional = runner.equity * runner.leverage
    runner.perp_notional = runner.spot_notional
    runner.perp_qty = runner.perp_notional / runner.entry_price
    runner.qty = runner.perp_qty
    runner.borrow_principal = runner.equity * (runner.leverage - 1.0)
    runner.position_generation = (
        funding_event_id(runner.venue.exchange_id, runner.symbol, checkpoint) + "|position"
    )
    runner.last_funding_ts = checkpoint
    runner.funding_notional_price = runner.entry_price
    runner.funding_notional_price_source = "PAPER_RECENT_PRICE_APPROXIMATION"
    runner.funding_notional_price_timestamp = checkpoint
    runner.last_funding_ts = checkpoint


def _runner(tmp_path: Path, funding: pd.Series) -> CarryRunner:
    policy = replace(PAPER_CARRY_POLICY, smooth_days=1)
    return CarryRunner(
        policy=policy,
        state_file=tmp_path / "carry.db",
        venue=_Venue(funding),
        notifier=lambda _message: True,
    )


@pytest.mark.parametrize(
    "duration",
    [pd.Timedelta(hours=1), pd.Timedelta(hours=8), pd.Timedelta(days=1), pd.Timedelta(days=7)],
)
def test_borrow_uses_elapsed_seconds_for_standard_durations(duration: pd.Timedelta) -> None:
    start = pd.Timestamp("2030-01-01T00:00:00Z")
    index = pd.DatetimeIndex([start, start + duration])
    result = borrow_cost_for_intervals(
        index,
        borrow_notional=2_000.0,
        annual_borrow_rate=0.10,
    )
    expected = 2_000.0 * 0.10 * duration.total_seconds() / SECONDS_PER_YEAR
    assert result.sum() == pytest.approx(expected)


def test_borrow_is_invariant_to_intermediate_row_count() -> None:
    start = pd.Timestamp("2030-01-01T00:00:00Z")
    sparse = pd.DatetimeIndex([start, start + pd.Timedelta(days=1)])
    dense = pd.date_range(start, periods=25, freq="h")
    sparse_cost = borrow_cost_for_intervals(
        sparse,
        borrow_notional=2_000.0,
        annual_borrow_rate=0.10,
    ).sum()
    dense_cost = borrow_cost_for_intervals(
        dense,
        borrow_notional=2_000.0,
        annual_borrow_rate=0.10,
    ).sum()
    assert dense_cost == pytest.approx(sparse_cost, rel=1e-12)


def test_borrow_handles_irregular_gap_by_real_duration() -> None:
    start = pd.Timestamp("2030-01-01T00:00:00Z")
    index = pd.DatetimeIndex(
        [
            start,
            start + pd.Timedelta(hours=1),
            start + pd.Timedelta(hours=9),
            start + pd.Timedelta(days=1),
        ]
    )
    cost = borrow_cost_for_intervals(
        index,
        borrow_notional=2_000.0,
        annual_borrow_rate=0.10,
    ).sum()
    assert cost == pytest.approx(2_000.0 * 0.10 * 24 / 365.25 / 24)


def test_dst_is_converted_to_continuous_utc_time() -> None:
    local = pd.date_range("2030-03-31 00:00", periods=3, freq="h", tz="Europe/Paris")
    utc = local.tz_convert("UTC")
    assert elapsed_years_between(utc[0], utc[-1]) == pytest.approx(2 * 3600 / SECONDS_PER_YEAR)
    assert borrow_cost_for_intervals(
        utc,
        borrow_notional=2_000.0,
        annual_borrow_rate=0.10,
    ).sum() == pytest.approx(2_000.0 * 0.10 * 2 / (365.25 * 24))


def test_borrow_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        borrow_cost_for_intervals(
            pd.DatetimeIndex([pd.Timestamp("2030-01-01"), pd.Timestamp("2030-01-02")]),
            borrow_notional=1_000.0,
            annual_borrow_rate=0.10,
        )


@pytest.mark.parametrize(
    ("index", "values", "message"),
    [
        ([pd.Timestamp("2030-01-01")], [0.1], "fuseau"),
        (
            pd.DatetimeIndex(["2030-01-01T00:00Z", "2030-01-01T00:00Z"]),
            [0.1, 0.1],
            "DUPLICATE",
        ),
        (
            pd.DatetimeIndex(["2030-01-01T01:00Z", "2030-01-01T00:00Z"]),
            [0.1, 0.1],
            "OUT_OF_ORDER",
        ),
        (
            pd.DatetimeIndex(["2030-01-01T00:00Z"]),
            [float("nan")],
            "NaN",
        ),
    ],
)
def test_funding_event_validation_rejects_structural_errors(
    index: object, values: list[float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_funding_events(pd.Series(values, index=index))


def test_elapsed_time_rejects_naive_and_negative_ranges() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        elapsed_years_between(pd.Timestamp("2030-01-01"), pd.Timestamp("2030-01-02"))
    with pytest.raises(ValueError, match="négative"):
        elapsed_years_between(pd.Timestamp("2030-01-02T00:00Z"), pd.Timestamp("2030-01-01T00:00Z"))


def test_event_identity_and_gap_report_are_deterministic() -> None:
    naive = funding_event_id("Hyperliquid", "BTC/USDC:USDC", pd.Timestamp("2030-01-01"))
    aware = funding_event_id("hyperliquid", "BTC/USDC:USDC", pd.Timestamp("2030-01-01T00:00:00Z"))
    assert naive == aware
    report = funding_event_gaps(
        _hourly(3).drop(pd.Timestamp("2030-01-01T01:00Z")), funding_interval="1h"
    )
    assert report["missing_events"] == 1


def test_borrow_accepts_series_inputs_and_active_mask() -> None:
    index = pd.date_range("2030-01-01T00:00Z", periods=3, freq="h")
    result = borrow_cost_for_intervals(
        index,
        borrow_notional=pd.Series([1_000.0, 2_000.0, 3_000.0], index=index),
        annual_borrow_rate=pd.Series(0.10, index=index),
        active=pd.Series([0.0, 1.0, 0.0], index=index),
    )
    assert result.iloc[0] == 0.0
    assert result.iloc[1] > 0.0
    assert result.iloc[2] == 0.0


def test_smoothing_contract_rejects_invalid_window_parameters() -> None:
    with pytest.raises(ValueError, match="au moins égal"):
        smooth_funding_events(_hourly(2), smooth_days=0)
    with pytest.raises(ValueError, match="coverage"):
        smooth_funding_events(_hourly(2), smooth_days=1, min_coverage_ratio=0.0)


def test_smoothing_requires_fourteen_calendar_days_of_hourly_history() -> None:
    result = smooth_funding_events(_hourly(14 * 24 + 1), smooth_days=14, funding_interval="1h")
    assert result.coverage.iloc[-1]["status"] == "OK"
    assert result.coverage.iloc[-1]["observed_events"] == 14 * 24
    short = smooth_funding_events(_hourly(14 * 24), smooth_days=14, funding_interval="1h")
    assert short.coverage.iloc[-1]["status"] == "INSUFFICIENT_FUNDING_HISTORY"


def test_smoothing_does_not_fill_a_missing_hour() -> None:
    funding = _hourly(14 * 24 + 1).drop(pd.Timestamp("2030-01-05T04:00:00Z"))
    result = smooth_funding_events(funding, smooth_days=14, funding_interval="1h")
    assert result.coverage.iloc[-1]["missing_events"] == 1
    assert result.coverage.iloc[-1]["status"] == "INSUFFICIENT_FUNDING_HISTORY"
    assert pd.isna(result.annualized.iloc[-1])


def test_smoothing_supports_legacy_eight_hour_events_without_row_assumption() -> None:
    index = pd.date_range("2030-01-01T00:00:00Z", periods=43, freq="8h")
    result = smooth_funding_events(
        pd.Series(0.001, index=index), smooth_days=14, funding_interval="8h"
    )
    assert result.coverage.iloc[-1]["status"] == "OK"
    assert result.coverage.iloc[-1]["expected_events"] == 42


def test_smoothing_is_not_lookahead_biased() -> None:
    full = _hourly(14 * 24 + 25)
    cutoff = full.index[14 * 24]
    prefix_result = smooth_funding_events(full.loc[:cutoff], smooth_days=14, funding_interval="1h")
    full_result = smooth_funding_events(full, smooth_days=14, funding_interval="1h")
    assert prefix_result.annualized.loc[cutoff] == pytest.approx(full_result.annualized.loc[cutoff])


def test_jittered_hourly_events_are_kept_without_forward_fill() -> None:
    base = pd.date_range("2030-01-01T00:00:00Z", periods=14 * 24 + 1, freq="h")
    index = base + pd.to_timedelta(0.261, unit="s")
    result = smooth_funding_events(
        pd.Series(0.001, index=index), smooth_days=14, funding_interval="1h"
    )
    assert result.coverage.iloc[-1]["status"] == "OK"
    assert result.coverage.iloc[-1]["observed_events"] == 14 * 24


@pytest.mark.parametrize("offset", [0.999, 0.261])
def test_funding_event_jitter_within_one_second_is_allowed(offset: float) -> None:
    base = pd.date_range("2030-01-01T00:00:00Z", periods=4, freq="h")
    report = funding_event_gaps(
        pd.Series(0.001, index=base + pd.to_timedelta(offset, unit="s")),
        funding_interval="1h",
    )
    assert report["cadence_anomalies"] == []


def test_funding_event_jitter_over_one_second_is_reported() -> None:
    base = pd.date_range("2030-01-01T00:00:00Z", periods=4, freq="h")
    report = funding_event_gaps(
        pd.Series(0.001, index=base + pd.to_timedelta(1.001, unit="s")),
        funding_interval="1h",
    )
    assert len(report["cadence_anomalies"]) == 4


def test_funding_is_applied_only_while_position_is_open(tmp_path: Path) -> None:
    funding = _hourly(6)
    runner = _runner(tmp_path, funding)
    runner.last_funding_ts = funding.index[0]
    runner._save_state()
    runner._apply_funding(funding.iloc[:3])
    before_entry = runner.equity

    _mark_open(runner, funding.index[2])
    runner._apply_funding(funding.iloc[2:4])
    during_position = runner.equity
    assert during_position > before_entry

    runner.in_position = False
    runner._apply_funding(funding.iloc[3:])
    assert runner.equity == pytest.approx(during_position)


def test_funding_replay_is_exactly_once_after_checkpoint_rewind(tmp_path: Path) -> None:
    funding = _hourly(4)
    runner = _runner(tmp_path, funding)
    _mark_open(runner, funding.index[0])
    runner._save_state()
    runner._apply_funding(funding)
    first_equity = runner.equity
    runner.last_funding_ts = funding.index[0]
    runner._apply_funding(funding)
    assert runner.equity == pytest.approx(first_equity)


def test_missing_funding_is_fail_closed_and_survives_restart(tmp_path: Path) -> None:
    funding = _hourly(4).drop(pd.Timestamp("2030-01-01T01:00:00Z"))
    runner = _runner(tmp_path, funding)
    _mark_open(runner, funding.index[0])
    runner._save_state()
    initial_equity = runner.equity
    runner._apply_funding(funding)
    assert runner.accounting_uncertain
    assert runner.accounting_uncertainty_reason
    assert runner.equity == pytest.approx(initial_equity)
    assert runner.store.read_incidents(open_only=True)[0]["severity"] == "CRITICAL"

    revived = _runner(tmp_path, funding)
    assert revived.accounting_uncertain
    revived._tick()
    assert revived.equity == pytest.approx(initial_equity)


def test_historical_backlog_without_price_history_fails_closed(tmp_path: Path) -> None:
    funding = _hourly(4)
    runner = _runner(tmp_path, funding)
    _mark_open(runner, funding.index[0])
    runner.venue.funding_notional_prices_since = None
    initial_equity = runner.equity

    runner._apply_funding(funding)

    assert runner.accounting_uncertain
    assert "PRICE_HISTORY_UNAVAILABLE" in runner.accounting_uncertainty_reason
    assert runner.equity == pytest.approx(initial_equity)
    assert runner.store.read_funding_ledger() == []


def test_runner_does_not_releverage_when_equity_changes_mid_position(tmp_path: Path) -> None:
    funding = _hourly(3, rate=0.001)
    runner = _runner(tmp_path, funding)
    _mark_open(runner, funding.index[0])
    runner.perp_qty = 1.0
    runner.perp_notional = 100_000.0
    runner.borrow_principal = 20_000.0
    runner._save_state()

    runner._apply_funding(funding.iloc[:2])
    runner.equity += 5_000.0
    runner._apply_funding(funding.iloc[1:])

    ledger = runner.store.read_funding_ledger()
    assert len(ledger) == 2
    assert runner.perp_qty == 1.0
    assert [row["funding_notional"] for row in ledger] == [100_000.0, 100_000.0]
    assert [row["funding_notional_price"] for row in ledger] == [100_000.0, 100_000.0]
    assert [row["funding_notional_price_source"] for row in ledger] == [
        "PAPER_RECENT_PRICE_APPROXIMATION",
        "PAPER_RECENT_PRICE_APPROXIMATION",
    ]
    assert [row["borrow_principal"] for row in ledger] == [20_000.0, 20_000.0]
    expected_borrow = 20_000.0 * 0.10 * 3_600.0 / SECONDS_PER_YEAR
    assert [row["funding_pnl"] for row in ledger] == [100.0, 100.0]
    assert [row["borrow_cost"] for row in ledger] == pytest.approx(
        [expected_borrow, expected_borrow]
    )


def test_paper_open_and_close_costs_use_actual_execution_notionals(tmp_path: Path) -> None:
    funding = _hourly(2)
    runner = _runner(tmp_path, funding)
    fee_rate = runner.policy.fee_rate
    slippage_rate = runner.policy.slippage_bps / 10_000.0
    runner._open_position(
        0.0,
        100_000.0,
        "PAPER_RECENT_PRICE_APPROXIMATION",
        funding.index[0],
    )
    open_notional = runner.perp_qty * 100_000.0
    expected_after_open = runner.policy.capital - open_notional * 2.0 * (fee_rate + slippage_rate)
    assert runner.equity == pytest.approx(expected_after_open)

    runner._close_position(
        0.0,
        reason="test",
        exit_price=120_000.0,
        price_source="PAPER_RECENT_PRICE_APPROXIMATION",
        price_timestamp=funding.index[1],
    )
    close_notional = (runner.policy.capital * runner.leverage / 100_000.0) * 120_000.0
    expected = expected_after_open - close_notional * 2.0 * (fee_rate + slippage_rate)
    assert runner.equity == pytest.approx(expected)
    assert not runner.in_position


def test_backtest_funding_notional_uses_fixed_entry_quantity_and_price() -> None:
    funding = pd.Series(
        0.0001,
        index=pd.date_range("2030-01-01T00:00:00Z", periods=50, freq="h"),
    )
    prices = pd.Series(
        [100_000.0 + 1_000.0 * i for i in range(50)],
        index=funding.index,
    )
    result = backtest_carry(
        funding,
        leverage=2.0,
        borrow_rate_ann=0.0,
        fee_rate=0.0,
        slippage_bps=0.0,
        enter_ann=-1.0,
        exit_ann=-2.0,
        smooth_days=1,
        funding_interval="1h",
        initial_capital=1_000.0,
        funding_notional_price=prices,
    )
    entry_timestamp = pd.Timestamp(result["entry_timestamps"][0])
    entry_price = float(prices.loc[entry_timestamp])
    entry_qty = 1_000.0 * 2.0 / entry_price
    expected = float(
        (entry_qty * prices.loc[entry_timestamp + pd.Timedelta(hours=1) :] * 0.0001).sum()
    )
    assert entry_timestamp == funding.index[24]
    assert result["funding_notional_mode"] == "perp_qty_times_price"
    assert result["funding_notional_price_source"] == "OHLC_APPROXIMATION"
    assert result["basis_mode"] == "synthetic_zero"
    assert result["funding_pnl"] == pytest.approx(expected)


def test_backtest_entry_and_exit_follow_funding_event_timeline() -> None:
    index = pd.date_range("2030-01-01T00:00:00Z", periods=4, freq="h")
    funding = pd.Series([0.01, 0.02, 0.03, 0.04], index=index)
    decision = pd.Series([True, True, False, False], index=index)
    exposure = pd.Series([False, True, True, False], index=index)
    prices = pd.Series([100.0, 110.0, 120.0, 130.0], index=index)
    result = _carry_fixed_position_amounts(
        funding,
        decision,
        exposure,
        initial_capital=1_000.0,
        leverage=2.0,
        fee_rate=0.001,
        slippage_bps=0.0,
        borrow_rates=pd.Series(0.10, index=index),
        basis_return=pd.Series(0.0, index=index),
        funding_notional_price=prices,
    )
    _, funding_amounts, borrow_amounts, _, fee_amounts, _, _, _, entries, exits = result
    quantity = 2_000.0 / 100.0
    assert entries == [index[0]]
    assert exits == [index[2]]
    assert funding_amounts.iloc[0] == 0.0
    assert funding_amounts.iloc[1] == pytest.approx(quantity * 110.0 * 0.02)
    assert funding_amounts.iloc[2] == pytest.approx(quantity * 120.0 * 0.03)
    assert funding_amounts.iloc[3] == 0.0
    one_hour_borrow = 1_000.0 * 0.10 / SECONDS_PER_YEAR * 3_600.0
    assert borrow_amounts.iloc[0] == 0.0
    assert borrow_amounts.iloc[1] == pytest.approx(one_hour_borrow)
    assert borrow_amounts.iloc[2] == pytest.approx(one_hour_borrow)
    assert borrow_amounts.iloc[3] == 0.0
    assert fee_amounts.iloc[0] == pytest.approx(2.0 * 0.001 * 2_000.0)
    assert fee_amounts.iloc[2] == pytest.approx(2.0 * 0.001 * 2_400.0)
