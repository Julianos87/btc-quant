from __future__ import annotations

from pathlib import Path

import pandas as pd

from btcquant.execution.broker import PaperBroker
from btcquant.execution.carry_runner import CarryRunner
from btcquant.execution.runner import LiveRunner
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Position


class FakeVenue:
    payments_per_day = 24

    @property
    def payments_per_year(self) -> int:
        return 24 * 365

    def last_price(self) -> float:
        return 100.0

    def fetch_ohlcv(self, timeframe: str, limit: int = 1000) -> list[list]:
        return []

    def funding_rate_8h(self) -> float:
        return 0.0

    def funding_history(self, days: float) -> pd.Series:
        return pd.Series(dtype=float)

    def funding_history_since(self, since: pd.Timestamp) -> pd.Series:
        return pd.Series(dtype=float)


class FakeClock:
    now = pd.Timestamp("2030-01-02T03:04:05Z")

    def utc_now(self) -> pd.Timestamp:
        return self.now

    def time(self) -> float:
        return self.now.timestamp()

    def monotonic(self) -> float:
        return 123.0


def test_live_runner_accepts_network_and_notification_ports(tmp_path: Path):
    venue = FakeVenue()
    clock = FakeClock()
    messages: list[str] = []

    runner = LiveRunner(
        slots=[],
        broker=PaperBroker(),
        risk=RiskConfig(initial_capital=1_000),
        exchange_id="must-not-be-created",
        symbol="BTC/USDT",
        state_file=tmp_path / "state.db",
        venue=venue,
        notifier=lambda message: not messages.append(message),
        clock=clock,
    )

    assert runner.venue is venue
    assert runner.clock is clock
    assert runner.notifier("test") is True
    assert messages == ["test"]

    slot = type("Slot", (), {"strategy": type("Strategy", (), {"name": "test"})()})()
    position = Position(
        entry_time=pd.Timestamp("2029-12-01T00:00:00Z"),
        entry_price=100,
        qty=1,
        stop_price=90,
        direction=1,
    )
    trade = runner._trade_payload(slot, position, 110, 10, "test")
    assert trade["exit_ts"] == "2030-01-02T03:04:05+00:00"


def test_carry_runner_accepts_network_and_notification_ports(tmp_path: Path):
    venue = FakeVenue()
    messages: list[str] = []

    runner = CarryRunner(
        state_file=tmp_path / "state.db",
        venue=venue,
        notifier=lambda message: not messages.append(message),
    )

    assert runner.venue is venue
    assert runner.notifier("test") is True
    assert messages == ["test"]
