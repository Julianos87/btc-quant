"""Tests du noyau métier commun au backtest et au runner."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from btcquant.domain import (
    EntryRequested,
    ExitRequested,
    FundingAccrued,
    PyramidRequested,
    StopTightened,
    decide_bar_close,
)
from btcquant.execution.broker import PaperBroker
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Position, Strategy


class KernelStrategy(Strategy):
    name = "kernel"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {"exit_after": 2}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["signal"] = out.get("signal", 0)
        return out

    def entry_signal(self, row: pd.Series) -> int:
        return int(row["signal"])

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * 10.0

    def trailing_stop(self, row: pd.Series, position: Position) -> float:
        return float(row["close"]) - position.direction * 5.0

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        return position.bars_held >= int(self.params["exit_after"])

    def warmup_bars(self) -> int:
        return 1


class PyramidKernelStrategy(KernelStrategy):
    def pyramid_fraction(self, row: pd.Series, position: Position) -> float:
        return 0.30


def make_position(**changes) -> Position:
    position = Position(
        entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
        entry_price=100.0,
        qty=2.0,
        stop_price=90.0,
        direction=1,
        bars_held=0,
        best_close=100.0,
    )
    return replace(position, **changes)


def test_same_inputs_produce_same_decision_without_mutating_position():
    strategy = KernelStrategy()
    row = pd.Series({"close": 110.0, "signal": 0.0})
    original = make_position()

    first = decide_bar_close(strategy, row, original, funding_rate=0.001)
    second = decide_bar_close(strategy, row, original, funding_rate=0.001)

    assert first == second
    assert original == make_position()
    assert first.position == make_position(bars_held=1, best_close=110.0, stop_price=105.0)
    assert first.events == (
        FundingAccrued(rate=0.001, amount=0.22),
        StopTightened(previous_price=90.0, new_price=105.0),
    )


def test_exit_uses_advanced_position_and_kill_switch_has_priority():
    strategy = KernelStrategy()
    row = pd.Series({"close": 110.0, "signal": 0.0})
    position = make_position(bars_held=1)

    signal_decision = decide_bar_close(strategy, row, position)
    halted_decision = decide_bar_close(strategy, row, position, halted=True)

    assert ExitRequested("signal") in signal_decision.events
    assert ExitRequested("kill_switch") in halted_decision.events
    assert ExitRequested("signal") not in halted_decision.events


def test_pyramid_is_requested_only_when_no_exit_has_priority():
    row = pd.Series({"close": 110.0, "signal": 0.0})

    held = decide_bar_close(
        PyramidKernelStrategy(exit_after=99),
        row,
        make_position(),
    )
    exiting = decide_bar_close(
        PyramidKernelStrategy(exit_after=1),
        row,
        make_position(),
    )

    assert PyramidRequested(0.30) in held.events
    assert not any(isinstance(event, PyramidRequested) for event in exiting.events)


def test_entry_policy_is_centralized_and_rejects_invalid_directions():
    strategy = KernelStrategy()

    short_row = pd.Series({"close": 100.0, "signal": -1.0})
    assert decide_bar_close(strategy, short_row, None).events == (EntryRequested(-1),)
    assert decide_bar_close(strategy, short_row, None, allow_short=False).events == ()
    assert decide_bar_close(strategy, short_row, None, can_enter=False).events == ()

    invalid_row = pd.Series({"close": 100.0, "signal": 2.0})
    with pytest.raises(ValueError, match="Direction de signal invalide"):
        decide_bar_close(strategy, invalid_row, None)


def test_paper_runner_applies_exactly_the_kernel_decision(tmp_path, monkeypatch):
    strategy = KernelStrategy(exit_after=99)
    slot = StrategySlot(strategy, 1.0, 10_000.0)
    slot.position = make_position()
    runner = LiveRunner(
        [slot],
        PaperBroker(),
        RiskConfig(initial_capital=10_000.0),
        "binance",
        "BTC/USDT",
        tmp_path / "state.json",
    )
    index = pd.date_range("2026-01-01", periods=40, freq="4h", tz="UTC")
    close = np.linspace(100.0, 110.0, len(index))
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.ones(len(index)),
        },
        index=index,
    )
    monkeypatch.setattr(runner, "_fetch_frame", lambda _strategy: frame)
    monkeypatch.setattr(runner.venue, "funding_rate_8h", lambda: 0.0008)

    expected_row = strategy.prepare(frame).iloc[-1]
    expected = decide_bar_close(
        strategy,
        expected_row,
        make_position(),
    )
    actual = runner._process_bar(slot, float(frame["close"].iloc[-1]))

    assert actual == expected
    assert slot.position == expected.position
    assert slot.cash == pytest.approx(10_000.0)


def test_runner_applies_native_funding_payments_once(tmp_path, monkeypatch):
    strategy = KernelStrategy(exit_after=99)
    slot = StrategySlot(strategy, 1.0, 10_000.0)
    slot.position = make_position()
    runner = LiveRunner(
        [slot],
        PaperBroker(),
        RiskConfig(initial_capital=10_000.0),
        "binance",
        "BTC/USDT",
        tmp_path / "state.json",
    )
    now = pd.Timestamp.now(tz="UTC")
    runner.last_funding_ts = now - pd.Timedelta(hours=2)
    payment_ts = now - pd.Timedelta(hours=1)
    payments = pd.Series([0.0001], index=pd.DatetimeIndex([payment_ts]))
    monkeypatch.setattr(runner.venue, "funding_history_since", lambda _since: payments)

    runner._apply_funding_payments(110.0)
    runner.funding_service.last_poll_monotonic = 0.0
    runner._apply_funding_payments(110.0)

    assert slot.cash == pytest.approx(10_000.0 - 2.0 * 110.0 * 0.0001)
    events = [
        event
        for event in runner.store.read_events("trend")
        if event["event_type"] == "funding_payments_applied"
    ]
    assert len(events) == 1
