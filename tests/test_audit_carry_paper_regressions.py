from __future__ import annotations

import pandas as pd
import pytest

from btcquant.carry import CarryPolicy
from btcquant.config import CarryExecutionConfig
from btcquant.domain.execution import ExecutionConfig
from btcquant.execution.broker import PaperBroker
from btcquant.execution.carry_paper import (
    CarryAccountingState,
    CarryExecutionState,
    CarryMarketState,
)
from btcquant.execution.carry_runner import CarryRunner


def _market(timestamp: str = "2030-01-01T10:00:00Z") -> CarryMarketState:
    return CarryMarketState(
        timestamp=timestamp,
        spot_bid=99.0,
        spot_ask=101.0,
        perp_bid=100.0,
        perp_ask=102.0,
        spot_mark=100.0,
        perp_mark=101.0,
        source="audit-fixture",
        spot_order_book={"bids": [[99.0, 200.0]], "asks": [[101.0, 200.0]]},
        perp_order_book={"bids": [[100.0, 200.0]], "asks": [[102.0, 200.0]]},
    )


class _QualifiedVenue:
    exchange_id = "fixture"
    payments_per_day = 24
    native_funding_interval = pd.Timedelta(hours=1)

    def __init__(self, market: CarryMarketState):
        self.market = market
        self.references: list[pd.Timestamp] = []

    def current_carry_market_state(self) -> CarryMarketState:
        return self.market

    def funding_history_since(self, since: pd.Timestamp) -> pd.Series:
        del since
        return pd.Series(dtype=float)

    def funding_reference_price(self, timestamp: pd.Timestamp) -> dict[str, object]:
        self.references.append(timestamp)
        return {
            "price": 100.0,
            "timestamp": timestamp - pd.Timedelta(hours=1),
            "source": "FIXTURE_ASOF_REFERENCE",
        }


def _qualified_runner(tmp_path, venue: _QualifiedVenue) -> CarryRunner:
    policy = CarryPolicy(
        capital=10_000.0,
        leverage=1.0,
        enter_ann=0.01,
        exit_ann=0.0,
        smooth_days=1,
    )
    config = CarryExecutionConfig(
        model="two_leg_execution_v1",
        borrow_enabled=True,
        max_borrow=20_000.0,
        max_leverage=3.0,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.05,
        holding_days=30.0,
        fee_source="fixture",
        financing_source="fixture",
    )
    return CarryRunner(
        policy=policy,
        state_file=tmp_path / "carry.db",
        venue=venue,
        carry_execution=config,
        paper_execution_config=ExecutionConfig(
            fee_rate=0.0, slippage_bps=0.0, liquidity_model="order_book"
        ),
        notifier=lambda _message: True,
    )


def test_restored_paper_stop_ids_never_collide_with_new_stops():
    broker = PaperBroker(simulate_exchange_stops=True)
    broker.restore_protective_stop(
        "paper-stop-1", qty=1.0, stop_price=90.0, direction=1, client_order_id="old-1"
    )
    broker.restore_protective_stop(
        "paper-stop-7", qty=2.0, stop_price=89.0, direction=1, client_order_id="old-7"
    )

    new_id = broker.place_stop(3.0, 88.0, direction=1, client_order_id="new")

    assert new_id == "paper-stop-8"
    assert broker.protective_order_snapshot("paper-stop-1").requested_qty == pytest.approx(1.0)
    assert broker.protective_order_snapshot("paper-stop-7").requested_qty == pytest.approx(2.0)
    assert broker.protective_order_snapshot(new_id).requested_qty == pytest.approx(3.0)


def test_two_leg_entry_never_emits_when_halted_or_daily_locked(tmp_path):
    market = _market()
    for field in ("halted", "daily_lockout"):
        venue = _QualifiedVenue(market)
        runner = _qualified_runner(tmp_path / field, venue)
        setattr(runner, field, True)

        runner._open_two_leg(
            smooth_ann=0.20,
            market=market,
            decision_timestamp=market.timestamp,
        )

        assert runner.two_leg_state is CarryExecutionState.FLAT
        assert not runner.in_position
        assert runner.two_leg_balance.applied_event_ids == []


def test_two_leg_funding_consumes_venue_reference_and_skips_pre_entry_events(tmp_path):
    market = _market()
    venue = _QualifiedVenue(market)
    runner = _qualified_runner(tmp_path, venue)
    runner._open_two_leg(
        smooth_ann=0.20,
        market=market,
        decision_timestamp=market.timestamp,
    )
    assert runner.in_position
    entry = runner.last_funding_ts
    assert entry == market.timestamp

    before = entry - pd.Timedelta(hours=1)
    after = entry + pd.Timedelta(hours=1)
    runner._apply_two_leg_funding(pd.Series([0.01, 0.01], index=[before, after]))

    assert venue.references == [after]
    assert runner.last_funding_ts == after
    payment = [
        row for row in runner.two_leg_journal if row["event_type"] == "carry_funding_payment"
    ]
    assert len(payment) == 1
    assert payment[0]["reference_source"] == "FIXTURE_ASOF_REFERENCE"
    assert payment[0]["reference_timestamp"] == (after - pd.Timedelta(hours=1)).isoformat()
    assert runner.two_leg_balance.accrued_interest > 0

    revived = _qualified_runner(tmp_path, venue)
    assert not revived.accounting_uncertain
    assert revived.last_funding_ts == after
    assert revived.two_leg_balance.last_interest_timestamp == after


def test_two_leg_tick_respects_a_persisted_halt_before_signal_entry(tmp_path):
    market = _market()
    venue = _QualifiedVenue(market)
    runner = _qualified_runner(tmp_path, venue)
    runner.halted = True
    runner._recent_funding = lambda: pd.Series(
        0.01,
        index=pd.date_range("2030-01-01T00:00:00Z", periods=24, freq="h"),
    )

    runner._tick_two_leg()

    assert not runner.in_position
    assert runner.two_leg_state is CarryExecutionState.FLAT
    assert runner.two_leg_balance.applied_event_ids == []


def test_interest_is_accrued_as_liability_then_settled_once():
    balance = CarryAccountingState(initial_cash=10_000.0, debt_principal=2_000.0)
    start = pd.Timestamp("2030-01-01T00:00:00Z")
    end = start + pd.Timedelta(days=1)
    balance.last_interest_timestamp = start
    before = balance.equity

    cost = balance.accrue_interest(annual_rate=0.10, until=end, event_id="interest:1")

    expected = 2_000.0 * 0.10 / 365.25
    assert cost == pytest.approx(expected)
    assert balance.cash_available == pytest.approx(10_000.0)
    assert balance.accrued_interest == pytest.approx(expected)
    assert balance.equity == pytest.approx(before - expected)

    paid = balance.settle_interest(event_id="interest:settle", until=end)
    assert paid == pytest.approx(expected)
    assert balance.accrued_interest == pytest.approx(0.0)
    assert balance.cash_available == pytest.approx(10_000.0 - expected)
    assert balance.equity == pytest.approx(before - expected)
