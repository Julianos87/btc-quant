from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from btcquant.domain.carry_dated import BookLevel, DatedBook, DatedInstrument, DatedSettlement
from btcquant.domain.carry_dated_policy import FeeSchedule, MarginPolicy, evaluate_entry
from btcquant.research.carry_dated_replay import (
    ProtectiveObservation,
    compare_spot_fee_currency_scenarios,
    protective_exit_required,
    protective_observation_is_actionable,
    replay_expiry_cycle,
    replay_protective_close,
)


NOW = datetime(2026, 10, 12, 7, 0, tzinfo=UTC)
SOURCE = "https://www.deribit.com/api/v2/public/get_order_book"
EXPIRY = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)


def market_book(
    instrument_id: str,
    *,
    bid: str,
    ask: str,
    timestamp: datetime,
    received: datetime,
    quantity: str = "1",
    mark: str | None = None,
    index: str | None = None,
    content_hash: str | None = None,
) -> DatedBook:
    return DatedBook(
        instrument_id=instrument_id,
        bids=(BookLevel(Decimal(bid), Decimal(quantity)),),
        asks=(BookLevel(Decimal(ask), Decimal(quantity)),),
        market_timestamp=timestamp,
        received_timestamp=received,
        source=SOURCE,
        instrument_state="open",
        mark_price=Decimal(mark) if mark else None,
        index_price=Decimal(index) if index else None,
        content_hash=content_hash or ("d" if instrument_id == "BTC_USDC" else "e") * 64,
    )


def instruments_and_decision():
    metadata_hash = "a" * 64
    spot = DatedInstrument(
        venue="deribit",
        instrument_id="BTC_USDC",
        kind="spot",
        base="BTC",
        quote="USDC",
        settlement_currency=None,
        contract_size=Decimal("1"),
        amount_step=Decimal("0.0001"),
        min_amount=Decimal("0.0001"),
        tick_size=Decimal("0.1"),
        expiration_utc=None,
        active=True,
        maker_commission=None,
        taker_commission=None,
        fee_currency=None,
        fee_provenance="spot fee currency not documented",
        metadata_timestamp=NOW - timedelta(minutes=1),
        metadata_hash=metadata_hash,
        instrument_state="open",
        source=SOURCE,
    )
    future = DatedInstrument(
        venue="deribit",
        instrument_id="BTC_USDC-26OCT26",
        kind="dated_future",
        base="BTC",
        quote="USDC",
        settlement_currency="USDC",
        contract_size=Decimal("1"),
        amount_step=Decimal("0.0001"),
        min_amount=Decimal("0.0001"),
        tick_size=Decimal("0.1"),
        expiration_utc=EXPIRY,
        active=True,
        maker_commission=None,
        taker_commission=None,
        fee_currency=None,
        fee_provenance="public linear future spec",
        metadata_timestamp=NOW - timedelta(minutes=1),
        metadata_hash="b" * 64,
        instrument_state="open",
        source=SOURCE,
    )
    spot_book = market_book(
        "BTC_USDC",
        bid="99900",
        ask="100000",
        timestamp=NOW - timedelta(seconds=1),
        received=NOW,
        quantity="1",
        content_hash="c" * 64,
    )
    future_book = market_book(
        future.instrument_id,
        bid="110000",
        ask="110100",
        timestamp=NOW - timedelta(seconds=1),
        received=NOW,
        quantity="1",
        mark="110000",
        index="100000",
        content_hash="f" * 64,
    )
    fees = FeeSchedule.standard_deribit()
    margin = MarginPolicy()
    decision = evaluate_entry(
        ((spot, future, spot_book, future_book),),
        capital=Decimal("4000"),
        fee_schedule=fees,
        margin_policy=margin,
        now=NOW,
    )
    assert decision.action == "OPEN"
    return decision, fees, margin


def settlements_for(decision, *, final_index: str = "105000"):
    times: list[datetime] = []
    current = NOW.replace(hour=8, minute=0, second=0, microsecond=0)
    while current < decision.expiry_utc:
        times.append(current)
        current += timedelta(days=1)
    events: list[DatedSettlement] = []
    for index, period_end in enumerate(times):
        events.append(
            DatedSettlement(
                venue="deribit",
                instrument_id=decision.selected_instrument_id,
                settlement_key=f"daily:{period_end.isoformat()}",
                kind="daily",
                period_start=period_end - timedelta(minutes=30),
                period_end=period_end,
                official_index_price=Decimal("100000") + Decimal(index * 50),
                source="public/get_delivery_prices",
                received_at=period_end + timedelta(seconds=1),
                content_hash=f"{index + 1:064x}",
                correction_version="v1",
                event_timestamp=period_end,
            )
        )
    expiry_at = decision.expiry_utc
    events.append(
        DatedSettlement(
            venue="deribit",
            instrument_id=decision.selected_instrument_id,
            settlement_key=f"expiry:{expiry_at.isoformat()}",
            kind="expiry",
            period_start=expiry_at - timedelta(minutes=30),
            period_end=expiry_at,
            official_index_price=Decimal(final_index),
            source="public/get_delivery_prices",
            received_at=expiry_at + timedelta(seconds=1),
            content_hash="9" * 64,
            correction_version="v1",
            event_timestamp=expiry_at,
        )
    )
    return tuple(events)


def expiry_spot_book(
    decision, *, bid: str = "104900", received_delay: int = 2, quantity: str = "1"
):
    timestamp = decision.expiry_utc + timedelta(seconds=2)
    received = decision.expiry_utc + timedelta(seconds=received_delay)
    return market_book(
        "BTC_USDC",
        bid=bid,
        ask=str(Decimal(bid) + Decimal("100")),
        timestamp=timestamp,
        received=received,
        quantity=quantity,
        content_hash="8" * 64,
    )


def replay(decision, fees, margin, events=None, spot_book=None, *, scenario="QUOTE"):
    return replay_expiry_cycle(
        decision=decision,
        entry_filled_at=NOW + timedelta(seconds=2),
        spot_entry_fill_quantity=decision.spot_gross_quantity,
        spot_entry_fill_vwap=decision.spot_estimated_price,
        future_entry_fill_quantity=decision.quantity,
        future_entry_fill_vwap=decision.future_estimated_price,
        entry_margin_reference_price=decision.future_estimated_price,
        settlements=settlements_for(decision) if events is None else events,
        expiry_spot_book=expiry_spot_book(decision) if spot_book is None else spot_book,
        spot_fee_currency_scenario=scenario,
        spot_exit_fee_currency_scenario="BASE",
        spot_amount_step=Decimal("0.0001"),
        execution_label="FIXTURE",
        capital=Decimal("4000"),
        fee_schedule=fees,
        margin_policy=margin,
    )


def test_expiry_replay_telescopes_daily_cash_and_uses_observed_spot_bid() -> None:
    decision, fees, margin = instruments_and_decision()
    events = settlements_for(decision, final_index="105000")
    observed_bid = expiry_spot_book(decision, bid="104900")

    result = replay(decision, fees, margin, events, observed_bid)

    expected_future = decision.quantity * (decision.future_estimated_price - Decimal("105000"))
    assert result.future_settlement_pnl_usdc == expected_future
    assert sum((flow.cash_pnl for flow in result.settlement_flows), Decimal("0")) == expected_future
    assert result.terminal_spot_exit_price_usdc == Decimal("104900")
    assert result.spot_realized_pnl_usdc is not None
    assert result.net_pnl_usdc is not None
    assert result.economic_status == "INSUFFICIENT_EVIDENCE"
    assert result.execution_label == "FIXTURE"
    assert result.margin_is_cost is False
    assert result.terminal_basis_stress_loss_usdc == (
        result.spot_net_quantity_btc * Decimal("105000") * Decimal("0.05")
    )
    assert result.net_pnl_under_terminal_basis_stress_usdc == (
        result.net_pnl_usdc - result.terminal_basis_stress_loss_usdc
    )
    assert result.terminal_basis_stress_covered_by_reserve is False
    assert result.terminal_basis_stress_loss_usdc > result.admission_reserve_usdc


def test_expiry_cycle_pnl_matches_frozen_short_future_plus_spot_identity() -> None:
    decision, fees, margin = instruments_and_decision()
    q = decision.quantity
    matched_decision = replace(
        decision,
        spot_gross_quantity=q,
        spot_expected_net_quantity=q,
        expected_hedge_residual=Decimal("0"),
    )
    spot_entry = decision.spot_estimated_price
    future_entry = decision.future_estimated_price
    final_index = Decimal("105000")
    spot_exit = Decimal("104900")
    assert spot_entry is not None and future_entry is not None
    result = replay_expiry_cycle(
        decision=matched_decision,
        entry_filled_at=NOW + timedelta(seconds=2),
        spot_entry_fill_quantity=q,
        spot_entry_fill_vwap=spot_entry,
        future_entry_fill_quantity=q,
        future_entry_fill_vwap=future_entry,
        entry_margin_reference_price=future_entry,
        settlements=settlements_for(matched_decision, final_index=str(final_index)),
        expiry_spot_book=expiry_spot_book(matched_decision, bid=str(spot_exit)),
        spot_fee_currency_scenario="QUOTE",
        spot_exit_fee_currency_scenario="QUOTE",
        spot_amount_step=Decimal("0.0001"),
        execution_label="FIXTURE",
        capital=Decimal("4000"),
        fee_schedule=fees,
        margin_policy=margin,
    )
    expected = (
        q * (future_entry - final_index)
        + q * (spot_exit - spot_entry)
        - q * spot_entry * fees.spot_taker_rate
        - q * future_entry * fees.future_taker_rate
        - q * final_index * fees.delivery_rate
        - q * spot_exit * fees.spot_exit_taker_rate
    )

    assert result.net_pnl_usdc == expected
    assert result.hedge_residual_btc == 0


def test_missing_settlement_or_expiry_book_never_fabricates_terminal_pnl() -> None:
    decision, fees, margin = instruments_and_decision()
    events = settlements_for(decision)
    missing_daily = replay(decision, fees, margin, events[1:])
    missing_expiry = replay(decision, fees, margin, events[:-1])
    late_book = expiry_spot_book(decision, received_delay=301)
    late = replay(decision, fees, margin, events, late_book)

    assert missing_daily.status == "PENDING_ACCOUNTING"
    assert missing_daily.reason_code == "SETTLEMENT_BACKLOG"
    assert missing_daily.net_pnl_usdc is None
    assert missing_expiry.reason_code == "EXPIRY_SETTLEMENT_MISSING_OR_AMBIGUOUS"
    assert missing_expiry.net_pnl_usdc is None
    assert late.reason_code == "EXPIRY_SPOT_EXIT_BOOK_INVALID_OR_LATE"
    assert late.net_pnl_usdc is None

    premature_event = replace(
        events[0], received_at=events[0].event_timestamp - timedelta(seconds=1)
    )
    premature = replay(decision, fees, margin, (premature_event, *events[1:]))
    assert premature.reason_code == "SETTLEMENT_RECEIVED_BEFORE_EVENT"
    assert premature.net_pnl_usdc is None


def test_duplicate_settlement_is_idempotent_but_correction_conflict_is_pending() -> None:
    decision, fees, margin = instruments_and_decision()
    events = settlements_for(decision)
    duplicate = replay(decision, fees, margin, (*events, events[0]))
    corrected = replace(
        events[0],
        official_index_price=events[0].official_index_price + Decimal("1"),
        content_hash="7" * 64,
        correction_version="v2",
    )
    conflict = replay(decision, fees, margin, (*events, corrected))

    assert len(duplicate.settlement_flows) == len(events)
    assert duplicate.net_pnl_usdc is not None
    assert conflict.reason_code == "SETTLEMENT_CORRECTION_REQUIRES_REPLAY"
    assert conflict.net_pnl_usdc is None


def test_spot_fee_currency_sensitivity_is_explicit_and_not_double_charged() -> None:
    decision, fees, margin = instruments_and_decision()
    scenarios = compare_spot_fee_currency_scenarios(
        decision=decision,
        entry_filled_at=NOW + timedelta(seconds=2),
        spot_entry_fill_quantity=decision.spot_gross_quantity,
        spot_entry_fill_vwap=decision.spot_estimated_price,
        future_entry_fill_quantity=decision.quantity,
        future_entry_fill_vwap=decision.future_estimated_price,
        entry_margin_reference_price=decision.future_estimated_price,
        settlements=settlements_for(decision),
        expiry_spot_book=expiry_spot_book(decision),
        execution_label="FIXTURE",
        capital=Decimal("4000"),
        fee_schedule=fees,
        margin_policy=margin,
        spot_amount_step=Decimal("0.0001"),
    )
    scenarios_by_pair = {(entry, exit_): result for entry, exit_, result in scenarios}
    base_base = scenarios_by_pair[("BASE", "BASE")]
    base_quote = scenarios_by_pair[("BASE", "QUOTE")]
    quote_base = scenarios_by_pair[("QUOTE", "BASE")]
    quote_quote = scenarios_by_pair[("QUOTE", "QUOTE")]

    assert base_base.spot_entry_fee_btc > 0
    assert base_base.spot_entry_fee_quote_charged == 0
    assert base_base.spot_exit_fee_btc > 0
    assert base_quote.spot_exit_fee_btc == 0
    assert base_quote.spot_exit_fee_quote_charged_usdc > 0
    assert quote_quote.spot_entry_fee_btc == 0
    assert quote_quote.spot_entry_fee_quote_charged == quote_quote.spot_entry_fee_quote_equivalent
    assert base_base.spot_net_quantity_btc < quote_base.spot_net_quantity_btc
    assert base_base.net_pnl_usdc != base_quote.net_pnl_usdc
    assert quote_base.net_pnl_usdc != quote_quote.net_pnl_usdc


def test_protective_trigger_debits_unrealized_short_loss_but_never_credits_gain() -> None:
    margin = MarginPolicy()
    q = Decimal("0.01")
    # The pre-mark cash alone exceeds the stressed collateral. The $100
    # unrealized short loss since the last settlement makes it insufficient.
    assert protective_exit_required(
        available_derivative_cash=Decimal("380"),
        quantity_btc=q,
        mark_price=Decimal("110000"),
        last_settlement_reference_price=Decimal("100000"),
        margin_policy=margin,
    )
    # A $100 unrealized short gain is ignored: cash 260 remains below the
    # $270 stressed requirement at the lower mark.
    assert protective_exit_required(
        available_derivative_cash=Decimal("260"),
        quantity_btc=q,
        mark_price=Decimal("90000"),
        last_settlement_reference_price=Decimal("100000"),
        margin_policy=margin,
    )
    assert not protective_exit_required(
        available_derivative_cash=Decimal("300"),
        quantity_btc=q,
        mark_price=Decimal("90000"),
        last_settlement_reference_price=Decimal("100000"),
        margin_policy=margin,
    )
    assert protective_exit_required(
        available_derivative_cash=Decimal("-1"),
        quantity_btc=q,
        mark_price=Decimal("100000"),
        last_settlement_reference_price=Decimal("100000"),
        margin_policy=margin,
    )


def test_protective_close_uses_fresh_books_in_order_and_retains_spot_residual() -> None:
    _, fees, margin = instruments_and_decision()
    future_book = market_book(
        "BTC_USDC-26OCT26",
        bid="110400",
        ask="110500",
        timestamp=NOW - timedelta(seconds=1),
        received=NOW,
        quantity="0.02",
        mark="110000",
        index="100000",
    )
    spot_book = market_book(
        "BTC_USDC",
        bid="99900",
        ask="100000",
        timestamp=NOW - timedelta(seconds=1),
        received=NOW,
        quantity="0.005",
    )
    observation = ProtectiveObservation(
        observed_at=NOW,
        last_settlement_reference_price=Decimal("100000"),
        derivative_cash_available_usdc=Decimal("380"),
        future_book=future_book,
        spot_book=spot_book,
    )
    result = replay_protective_close(
        observation=observation,
        quantity_future_btc=Decimal("0.01"),
        spot_net_quantity_btc=Decimal("0.01"),
        spot_entry_gross_quantity_btc=Decimal("0.0101"),
        spot_entry_fill_vwap_usdc=Decimal("100000"),
        last_settlement_reference_price_usdc=Decimal("100000"),
        accumulated_future_settlement_pnl_usdc=Decimal("0"),
        future_entry_fee_usdc=Decimal("0.4"),
        spot_fee_currency_scenario="BASE",
        spot_exit_fee_currency_scenario="BASE",
        spot_amount_step=Decimal("0.0001"),
        fee_schedule=fees,
        margin_policy=margin,
    )

    assert result.triggered
    assert result.realized_short_loss_since_settlement_usdc == Decimal("100.00")
    assert result.cash_after_unrealized_loss_usdc == Decimal("280.00")
    assert result.future_quantity_closed_btc == Decimal("0.01")
    assert result.spot_quantity_sold_btc == Decimal("0.005")
    assert result.spot_exit_fee_btc == Decimal("0.0000025")
    assert result.spot_quantity_remaining_btc == Decimal("0.0049975")
    assert result.spot_exit_fee_currency_scenario == "BASE"
    assert result.status == "UNBALANCED_PROTECTIVE_CLOSE"


def test_protective_exit_fee_currency_changes_base_consumption_and_cash_fee() -> None:
    _, fees, margin = instruments_and_decision()
    future_book = market_book(
        "BTC_USDC-26OCT26",
        bid="110400",
        ask="110500",
        timestamp=NOW - timedelta(seconds=1),
        received=NOW,
        quantity="0.02",
        mark="110000",
        index="100000",
    )
    spot_book = market_book(
        "BTC_USDC",
        bid="99900",
        ask="100000",
        timestamp=NOW - timedelta(seconds=1),
        received=NOW,
        quantity="0.02",
    )
    observation = ProtectiveObservation(
        observed_at=NOW,
        last_settlement_reference_price=Decimal("100000"),
        derivative_cash_available_usdc=Decimal("380"),
        future_book=future_book,
        spot_book=spot_book,
    )
    common = dict(
        observation=observation,
        quantity_future_btc=Decimal("0.01"),
        spot_net_quantity_btc=Decimal("0.01"),
        spot_entry_gross_quantity_btc=Decimal("0.010005"),
        spot_entry_fill_vwap_usdc=Decimal("100000"),
        last_settlement_reference_price_usdc=Decimal("100000"),
        accumulated_future_settlement_pnl_usdc=Decimal("0"),
        future_entry_fee_usdc=Decimal("0.4"),
        spot_fee_currency_scenario="BASE",
        spot_amount_step=Decimal("0.0001"),
        fee_schedule=fees,
        margin_policy=margin,
    )
    base = replay_protective_close(**common, spot_exit_fee_currency_scenario="BASE")
    quote = replay_protective_close(**common, spot_exit_fee_currency_scenario="QUOTE")

    assert base.spot_exit_fee_btc > 0
    assert base.spot_exit_fee_quote_charged_usdc == 0
    assert base.spot_quantity_remaining_btc > 0  # amount-step rounding leaves dust
    assert quote.spot_exit_fee_btc == 0
    assert quote.spot_exit_fee_quote_charged_usdc > 0
    assert quote.spot_quantity_remaining_btc == 0
    assert base.net_realized_pnl_usdc != quote.net_realized_pnl_usdc


def test_stale_or_not_yet_received_books_never_trigger_automatic_liquidation() -> None:
    _, _, margin = instruments_and_decision()
    stale_future = market_book(
        "BTC_USDC-FUTURE",
        bid="110400",
        ask="110500",
        timestamp=NOW - timedelta(seconds=20),
        received=NOW,
        mark="110000",
    )
    stale_spot = market_book(
        "BTC_USDC",
        bid="99900",
        ask="100000",
        timestamp=NOW - timedelta(seconds=20),
        received=NOW,
    )
    observation = ProtectiveObservation(
        observed_at=NOW,
        last_settlement_reference_price=Decimal("100000"),
        derivative_cash_available_usdc=Decimal("0"),
        future_book=stale_future,
        spot_book=stale_spot,
    )

    assert not protective_observation_is_actionable(
        observation, quantity_btc=Decimal("0.01"), margin_policy=margin
    )
    later_receipt = replace(
        stale_future, market_timestamp=NOW, received_timestamp=NOW + timedelta(seconds=1)
    )
    not_yet_received = replace(
        observation,
        future_book=later_receipt,
        spot_book=replace(
            stale_spot, market_timestamp=NOW, received_timestamp=NOW + timedelta(seconds=1)
        ),
    )
    assert not protective_observation_is_actionable(
        not_yet_received, quantity_btc=Decimal("0.01"), margin_policy=margin
    )
