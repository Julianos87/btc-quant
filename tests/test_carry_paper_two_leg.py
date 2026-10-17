from __future__ import annotations

import pandas as pd
import pytest

from btcquant.domain.execution import ExecutionConfig
from btcquant.execution.carry_paper import (
    CarryAccountingState,
    CarryExecutionState,
    CarryFundingEvent,
    CarryLeg,
    CarryMarketState,
    CarryVenueSpec,
    CausalMarketTape,
    FinancingPlan,
    PaperTwoLegExecutor,
    check_margin,
    expected_net_carry,
    serialize_two_leg_state,
)


def market(ts: str, *, spot: float = 100.0, perp: float = 101.0) -> CarryMarketState:
    return CarryMarketState(
        timestamp=ts,
        spot_bid=spot - 1,
        spot_ask=spot + 1,
        perp_bid=perp - 1,
        perp_ask=perp + 1,
        spot_mark=spot,
        perp_mark=perp,
        source="fixture",
        spot_order_book={
            "snapshot_id": ts,
            "bids": [[spot - 1, 200.0]],
            "asks": [[spot + 1, 200.0]],
        },
        perp_order_book={
            "snapshot_id": ts,
            "bids": [[perp - 1, 200.0]],
            "asks": [[perp + 1, 200.0]],
        },
    )


def test_unknown_financing_is_not_qualified_and_never_assumed():
    plan = FinancingPlan.build(
        capital_available=4_000,
        target_notional=12_000,
        spot_price=100,
        perp_mark_price=101,
        leverage=3,
        max_borrow=None,
        initial_margin_rate=None,
        spot_fee_rate=0.001,
        perp_fee_rate=0.0005,
        spot_slippage_bps=5,
        perp_slippage_bps=5,
    )
    assert plan.qualified is False
    assert "inconnues" in plan.reason


def test_plan_accounts_for_spot_cash_and_perp_margin_once():
    plan = FinancingPlan.build(
        capital_available=10_000,
        target_notional=15_000,
        spot_price=100,
        perp_mark_price=101,
        leverage=3,
        max_borrow=8_000,
        initial_margin_rate=0.10,
        spot_fee_rate=0.001,
        perp_fee_rate=0.0005,
        spot_slippage_bps=0,
        perp_slippage_bps=0,
    )
    assert plan.qualified
    assert plan.spot_qty == pytest.approx(plan.perp_qty)
    assert plan.total_cash_required <= 10_000
    assert plan.borrow_principal < plan.max_borrow


def test_book_execution_consumes_levels_and_is_causal():
    tape = CausalMarketTape(
        [market("2026-09-21T10:00:00Z"), market("2026-09-21T10:00:01Z", spot=110)]
    )
    executor = PaperTwoLegExecutor(
        ExecutionConfig(fee_rate=0.001, liquidity_model="order_book", latency_ms=500),
        tape,
    )
    fill = executor.execute(
        leg=CarryLeg.SPOT,
        side="BUY",
        qty=50,
        decision_timestamp="2026-09-21T09:59:59.600Z",
        event_id="entry:spot",
    )
    assert fill.timestamp == pd.Timestamp("2026-09-21T10:00:01Z")
    assert fill.price == pytest.approx(111.0)
    with pytest.raises(ValueError, match="aucune donnée"):
        executor.execute(
            leg=CarryLeg.SPOT,
            side="BUY",
            qty=50,
            decision_timestamp="2026-09-21T10:00:01Z",
            event_id="entry:late",
        )


def test_asymmetric_fees_are_charged_on_all_four_legs_without_double_slippage():
    first = market("2026-10-11T10:00:00Z", spot=100, perp=101)
    second = market("2026-10-11T10:00:01Z", spot=102, perp=103)
    executor = PaperTwoLegExecutor(
        ExecutionConfig(
            fee_rate=0.0005,
            slippage_bps=5,
            liquidity_model="order_book",
            quantity_step=0.01,
            min_qty=0.01,
            min_notional=10,
        ),
        CausalMarketTape([first, second]),
        spot_fee_rate=0.01,
        perp_fee_rate=0.02,
        spot_quantity_step=0.01,
        perp_quantity_step=0.01,
        spot_min_notional=10,
        perp_min_notional=10,
    )
    balance = CarryAccountingState(initial_cash=1_000)

    spot_entry = executor.execute(
        leg=CarryLeg.SPOT,
        side="BUY",
        qty=1,
        decision_timestamp="2026-10-11T09:59:59Z",
        event_id="cycle:spot-entry",
    )
    perp_entry = executor.execute(
        leg=CarryLeg.PERP,
        side="SELL",
        qty=1,
        decision_timestamp=spot_entry.timestamp,
        event_id="cycle:perp-entry",
    )
    balance.apply_spot_fill(
        side="BUY",
        qty=spot_entry.filled_qty,
        price=spot_entry.price,
        fee=spot_entry.fee,
        event_id=spot_entry.event_id,
    )
    balance.apply_perp_fill(
        side="SELL",
        qty=perp_entry.filled_qty,
        price=perp_entry.price,
        fee=perp_entry.fee,
        event_id=perp_entry.event_id,
    )

    perp_exit = executor.execute(
        leg=CarryLeg.PERP,
        side="BUY",
        qty=1,
        decision_timestamp="2026-10-11T10:00:00.500Z",
        event_id="cycle:perp-exit",
    )
    spot_exit = executor.execute(
        leg=CarryLeg.SPOT,
        side="SELL",
        qty=1,
        decision_timestamp=perp_exit.timestamp,
        event_id="cycle:spot-exit",
    )
    balance.apply_perp_fill(
        side="BUY",
        qty=perp_exit.filled_qty,
        price=perp_exit.price,
        fee=perp_exit.fee,
        event_id=perp_exit.event_id,
    )
    balance.apply_spot_fill(
        side="SELL",
        qty=spot_exit.filled_qty,
        price=spot_exit.price,
        fee=spot_exit.fee,
        event_id=spot_exit.event_id,
    )

    assert [spot_entry.fee, spot_exit.fee] == pytest.approx([1.01, 1.01])
    assert [perp_entry.fee, perp_exit.fee] == pytest.approx([2.0, 2.08])
    assert balance.spot_fees == pytest.approx(2.02)
    assert balance.perp_fees == pytest.approx(4.08)
    assert balance.fees_total == pytest.approx(6.10)
    # Order-book spread is in the four fill prices. Configured 5 bps remain
    # an admission estimate and are never debited from this realized ledger.
    assert balance.equity == pytest.approx(989.90)
    assert balance.spot_qty == balance.perp_qty == 0


def test_two_leg_accounting_replays_without_double_application_and_sees_basis():
    balance = CarryAccountingState(initial_cash=10_000)
    balance.reserve_margin(1_600)
    balance.apply_spot_fill(side="BUY", qty=100, price=100, fee=1, event_id="spot:1")
    balance.apply_perp_fill(side="SELL", qty=100, price=101, fee=1, event_id="perp:1")
    first = balance.equity
    balance.mark(market("2026-09-21T10:00:00Z", spot=110, perp=120))
    divergent = balance.equity
    balance.apply_funding(
        CarryFundingEvent(
            event_id="funding:1",
            venue="fixture",
            instrument="BTC/USDC:USDC",
            timestamp="2026-09-21T11:00:00Z",
            native_rate=0.01,
            reference_price=100,
            reference_source="fixture_oracle",
        )
    )
    after_funding = balance.equity
    balance.apply_funding(
        CarryFundingEvent(
            event_id="funding:1",
            venue="fixture",
            instrument="BTC/USDC:USDC",
            timestamp="2026-09-21T11:00:00Z",
            native_rate=0.01,
            reference_price=100,
            reference_source="fixture_oracle",
        )
    )
    assert balance.equity == pytest.approx(after_funding)
    assert divergent != pytest.approx(first)
    assert balance.spot_unrealized_pnl != pytest.approx(-balance.perp_unrealized_pnl)
    assert balance.balance_sheet()["identity_residual"] == pytest.approx(0.0)


def test_short_perp_funding_keeps_native_sign_across_positive_negative_and_zero():
    balance = CarryAccountingState(initial_cash=10_000)
    balance.apply_perp_fill(side="SELL", qty=0.1, price=10_000, fee=0, event_id="short")
    cash_before = balance.cash_available

    for event_id, hour, rate in (
        ("positive", 12, 0.001),
        ("negative", 13, -0.002),
        ("zero", 14, 0.0),
    ):
        balance.apply_funding(
            CarryFundingEvent(
                event_id=f"funding:{event_id}",
                venue="fixture",
                instrument="BTC/USDC:USDC",
                timestamp=f"2026-09-21T{hour:02d}:00:00Z",
                native_rate=rate,
                reference_price=10_000,
                reference_source="fixture_oracle",
            )
        )

    # A short perp earns a positive native payment and pays a negative one.
    assert balance.funding_received == pytest.approx(1.0)
    assert balance.funding_paid == pytest.approx(2.0)
    assert balance.cash_available == pytest.approx(cash_before - 1.0)

    equity_after_all_payments = balance.equity
    balance.apply_funding(
        CarryFundingEvent(
            event_id="funding:negative",
            venue="fixture",
            instrument="BTC/USDC:USDC",
            timestamp="2026-09-21T13:00:00Z",
            native_rate=-0.002,
            reference_price=10_000,
            reference_source="fixture_oracle",
        )
    )
    assert balance.equity == pytest.approx(equity_after_all_payments)


def test_margin_is_not_hidden_by_economic_hedge():
    balance = CarryAccountingState(initial_cash=10_000)
    balance.reserve_margin(1_000)
    balance.apply_spot_fill(side="BUY", qty=100, price=100, fee=0, event_id="spot")
    balance.apply_perp_fill(side="SELL", qty=100, price=100, fee=0, event_id="perp")
    check = check_margin(
        balance,
        perp_mark=130,
        initial_margin_rate=0.10,
        maintenance_margin_rate=0.05,
    )
    assert check.liquidatable
    assert check.qualified


def test_net_carry_blocks_when_loaded_costs_exceed_funding():
    result = expected_net_carry(
        funding_ann=0.03,
        notional=12_000,
        borrow_rate_ann=0.10,
        debt=8_000,
        holding_days=30,
        entry_cost=100,
        exit_cost=100,
        uncertainty_reserve=10,
    )
    assert result["qualified"] is False
    assert result["net_expected"] < 0
    expected_before_reserve = (
        12_000 * 0.03 * (30 / 365.25) - 8_000 * 0.10 * (30 / 365.25) - 100 - 100
    )
    assert result["net_expected_before_reserve"] == pytest.approx(expected_before_reserve)
    assert result["net_expected_after_reserve"] == pytest.approx(result["net_expected"])
    assert result["basis_expected"] == 0


def test_serialized_state_keeps_model_provenance_and_state_machine():
    spec = CarryVenueSpec.hyperliquid_btc_usdc()
    state = serialize_two_leg_state(
        state=CarryExecutionState.RECONCILIATION_REQUIRED,
        spec=spec,
        balance=CarryAccountingState(initial_cash=4_000),
        active_intent={"intent_id": "x"},
    )
    assert state["model_version"] == "carry_two_leg_execution_v1"
    assert state["qualification"] == "NON_QUALIFIED"
    assert state["active_intent"]["intent_id"] == "x"


def test_runner_paper_profile_blocks_unqualified_two_leg_entry(tmp_path):
    from btcquant.config import CarryExecutionConfig
    from btcquant.carry import CarryPolicy
    from btcquant.execution.carry_runner import CarryRunner

    class Venue:
        exchange_id = "fixture"
        payments_per_day = 3
        native_funding_interval = pd.Timedelta("8h")

        def funding_history_since(self, since):
            index = pd.date_range("2026-09-20", periods=60, freq="8h", tz="UTC")
            return pd.Series(0.0002, index=index)[lambda values: values.index >= since]

        def current_carry_market_state(self):
            return market("2026-09-21T10:00:00Z")

    runner = CarryRunner(
        policy=CarryPolicy(capital=4_000, leverage=1.0, smooth_days=1, enter_ann=0.01),
        state_file=tmp_path / "carry.db",
        venue=Venue(),
        carry_execution=CarryExecutionConfig(model="two_leg_execution_v1"),
        paper_execution_config=ExecutionConfig(fee_rate=0.001, liquidity_model="order_book"),
        notifier=lambda _message: True,
    )
    runner._tick()
    assert not runner.in_position
    assert runner.equity == pytest.approx(4_000)
    assert runner.two_leg_state is CarryExecutionState.FLAT
