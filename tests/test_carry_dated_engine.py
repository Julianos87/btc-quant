from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

from btcquant.domain.carry_dated import BookLevel, DatedBook, DatedInstrument, DatedSettlement
from btcquant.domain.carry_dated_policy import FeeSchedule, MarginPolicy, evaluate_entry
from btcquant.execution.carry_dated_ledger import (
    CarryDatedLedger,
    DatedLedgerError,
    common_quantity_step,
    fill_from_book,
    floor_to_step,
    state_decimal,
)
from btcquant.execution.carry_dated_runner import CarryDatedRunner
from btcquant.execution.errors import AccountingIdentityCollision
from btcquant.execution.state_store import StateStore
from btcquant.entrypoints.carry_dated_experimental import load_experimental_config
from dashboard.carry_dated_status import validate_snapshot


NOW = datetime(2026, 10, 12, 7, 0, tzinfo=UTC)
EXPIRY = datetime(2026, 11, 11, 8, 0, tzinfo=UTC)
SOURCE = "https://www.deribit.com/api/v2/public/get_order_book"
SPOT_ID = "BTC_USDC"
FUTURE_ID = "BTC_USDC-11NOV26"


def instrument(
    instrument_id: str,
    *,
    kind: str,
    now: datetime = NOW,
    expiry: datetime | None = None,
    min_amount: str = "0.001",
) -> DatedInstrument:
    return DatedInstrument(
        venue="deribit",
        instrument_id=instrument_id,
        kind=kind,  # type: ignore[arg-type]
        base="BTC",
        quote="USDC",
        settlement_currency="USDC" if kind == "dated_future" else None,
        contract_size=Decimal("1"),
        amount_step=Decimal("0.0001"),
        min_amount=Decimal(min_amount),
        tick_size=Decimal("0.1"),
        expiration_utc=expiry,
        active=True,
        maker_commission=None,
        taker_commission=None,
        fee_currency=None,
        fee_provenance="public metadata; routed spot fee currency unknown",
        metadata_timestamp=now - timedelta(seconds=60),
        metadata_hash=("a" if kind == "spot" else instrument_id[-1]) * 64,
        instrument_state="open",
        source=SOURCE,
    )


def book(
    instrument_id: str,
    *,
    now: datetime,
    bid: str,
    ask: str,
    quantity: str = "0.05",
    mark: str | None = None,
    index: str | None = None,
    market_time: datetime | None = None,
) -> DatedBook:
    stamp = market_time or now - timedelta(seconds=1)
    return DatedBook(
        instrument_id=instrument_id,
        bids=(BookLevel(Decimal(bid), Decimal(quantity)),),
        asks=(BookLevel(Decimal(ask), Decimal(quantity)),),
        market_timestamp=stamp,
        received_timestamp=now,
        source=SOURCE,
        instrument_state="open",
        mark_price=Decimal(mark) if mark else None,
        index_price=Decimal(index) if index else None,
        content_hash=("b" if instrument_id == SPOT_ID else "c") * 64,
    )


def settlement(
    *,
    key: str,
    kind: str,
    event: datetime,
    start: datetime,
    end: datetime,
    price: str,
    content_hash: str | None = None,
) -> DatedSettlement:
    return DatedSettlement(
        venue="deribit",
        instrument_id=FUTURE_ID,
        settlement_key=key,
        kind=kind,  # type: ignore[arg-type]
        period_start=start,
        period_end=end,
        official_index_price=Decimal(price),
        source="public official Deribit settlement history",
        received_at=event + timedelta(seconds=1),
        content_hash=content_hash or (key.encode().hex()[:64].ljust(64, "0")),
        correction_version="1",
        event_timestamp=event,
    )


class FakeMarket:
    def __init__(
        self,
        instruments: tuple[DatedInstrument, ...],
        books: dict[str, DatedBook],
        settlements: tuple[DatedSettlement, ...] = (),
    ) -> None:
        self.instruments = instruments
        self.books = books
        self.settlements = list(settlements)
        self.requested_books: list[str] = []

    def list_active_instruments(self) -> tuple[DatedInstrument, ...]:
        return self.instruments

    def get_book(self, instrument_id: str) -> DatedBook:
        self.requested_books.append(instrument_id)
        return self.books[instrument_id]

    def get_settlements(
        self, instrument_id: str, after_key: str | None = None
    ) -> tuple[DatedSettlement, ...]:
        assert instrument_id == FUTURE_ID
        ordered = sorted(self.settlements, key=lambda item: item.event_timestamp)
        if after_key is None:
            return tuple(ordered)
        cursor = next(
            (index for index, item in enumerate(ordered) if item.settlement_key == after_key),
            None,
        )
        if cursor is None:
            raise AssertionError("test market cursor does not match settlement history")
        return tuple(ordered[cursor + 1 :])


def make_market(*, now: datetime = NOW, expiry: datetime = EXPIRY) -> FakeMarket:
    spot = instrument(SPOT_ID, kind="spot", now=now)
    future = instrument(FUTURE_ID, kind="dated_future", now=now, expiry=expiry)
    return FakeMarket(
        (spot, future),
        {
            SPOT_ID: book(SPOT_ID, now=now, bid="99900", ask="100000"),
            FUTURE_ID: book(
                FUTURE_ID,
                now=now,
                bid="110000",
                ask="110100",
                mark="110000",
                index="100000",
            ),
        },
    )


def make_runner(
    tmp_path: Path,
    market: FakeMarket,
    *,
    now: datetime = NOW,
    capital: Decimal = Decimal("4000"),
    status_path: Path | None = None,
) -> tuple[StateStore, CarryDatedRunner, list[datetime]]:
    store = StateStore(tmp_path / "carry_dated.db")
    clock = [now]
    runner = CarryDatedRunner(
        store=store,
        market=market,
        fee_schedule=FeeSchedule.standard_deribit(),
        margin_policy=MarginPolicy.deribit_ssm_linear_btc(),
        capital=capital,
        clock=lambda: clock[0],
        status_path=status_path or tmp_path / "carry_dated_status.json",
    )
    return store, runner, clock


def open_position(
    tmp_path: Path,
) -> tuple[StateStore, CarryDatedRunner, FakeMarket, list[datetime]]:
    market = make_market()
    status_path = tmp_path / "carry_dated_status.json"
    store, runner, clock = make_runner(tmp_path, market, status_path=status_path)
    runner.run_once()
    assert runner.ledger.state["status"] == "OPEN"
    return store, runner, market, clock


def test_paper_entry_status_parses_and_preserves_trend(tmp_path: Path) -> None:
    market = make_market()
    status_path = tmp_path / "carry_dated_status.json"
    store, runner, _ = make_runner(tmp_path, market, status_path=status_path)
    trend = {"slots": {"trend_ls_20": {"cash": 6000, "stop_order_id": "trend-stop"}}}
    store.save_engine_state("trend", trend)

    snapshot = runner.run_once()

    assert snapshot["status"] == "OPEN"
    assert snapshot["paper_only"] is True
    assert Decimal(snapshot["cash_total"]) >= 0
    assert Decimal(snapshot["free_cash"]) >= 0
    assert Decimal(snapshot["spot_qty"]) > 0
    assert Decimal(snapshot["future_qty"]) > 0
    assert abs(Decimal(snapshot["hedge_residual"])) <= Decimal(snapshot["hedge_tolerance"])
    assert store.load_engine_state("trend") == trend
    assert validate_snapshot(json.loads(status_path.read_text()))["status"] == "OPEN"
    assert not hasattr(market, "place_order")
    assert not hasattr(runner, "order_router")


def test_nearest_invalid_book_waits_without_falling_through(tmp_path: Path) -> None:
    spot = instrument(SPOT_ID, kind="spot")
    nearest = instrument(
        FUTURE_ID,
        kind="dated_future",
        expiry=NOW + timedelta(days=20),
    )
    later = instrument(
        "BTC_USDC-11DEC26",
        kind="dated_future",
        expiry=NOW + timedelta(days=30),
    )
    market = FakeMarket(
        (spot, later, nearest),
        {
            SPOT_ID: book(SPOT_ID, now=NOW, bid="99900", ask="100000"),
            FUTURE_ID: book(
                FUTURE_ID,
                now=NOW,
                bid="110000",
                ask="110100",
                mark="110000",
                market_time=NOW - timedelta(hours=1),
            ),
            later.instrument_id: book(
                later.instrument_id,
                now=NOW,
                bid="120000",
                ask="120100",
                mark="120000",
            ),
        },
    )
    _, runner, _ = make_runner(tmp_path, market)

    snapshot = runner.run_once()

    assert snapshot["status"] == "FLAT"
    assert snapshot["reason_code"] == "BOOK_STALE"
    assert market.requested_books == [SPOT_ID, FUTURE_ID]
    assert (
        validate_snapshot(json.loads((tmp_path / "carry_dated_status.json").read_text()))["status"]
        == "FLAT"
    )


def test_restart_ignores_pre_entry_settlement_and_uses_entry_reference(tmp_path: Path) -> None:
    store, first_runner, market, clock = open_position(tmp_path)
    opened_at = datetime.fromisoformat(
        str(first_runner.ledger.state["position_opened_at"]).replace("Z", "+00:00")
    )
    old = settlement(
        key="old-before-position",
        kind="daily",
        event=opened_at - timedelta(days=1),
        start=opened_at - timedelta(days=2),
        end=opened_at - timedelta(days=1),
        price="90000",
    )
    next_event = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    first = settlement(
        key="first-after-position",
        kind="daily",
        event=next_event,
        start=datetime(2026, 10, 12, 8, 0, tzinfo=UTC),
        end=next_event,
        price="108000",
    )
    market.settlements[:] = [old, first]
    clock[0] = next_event
    market.books[SPOT_ID] = book(SPOT_ID, now=next_event, bid="107900", ask="108000")
    market.books[FUTURE_ID] = book(
        FUTURE_ID,
        now=next_event,
        bid="108000",
        ask="108100",
        mark="108000",
    )
    restarted = CarryDatedRunner(
        store=store,
        market=market,
        fee_schedule=FeeSchedule.standard_deribit(),
        margin_policy=MarginPolicy.deribit_ssm_linear_btc(),
        capital=Decimal("4000"),
        clock=lambda: clock[0],
        status_path=tmp_path / "restarted.json",
    )
    entry_future_price = state_decimal(restarted.ledger.state, "future_reference_price")
    quantity = state_decimal(restarted.ledger.state, "future_qty")
    cash_before = state_decimal(restarted.ledger.state, "cash_total")

    restarted.run_once()

    state = restarted.ledger.state
    assert state["settlements_applied"] == 1
    assert state["last_settlement_key"] == first.settlement_key
    assert state["future_reference_price"] == "108000"
    assert Decimal(state["cash_total"]) - cash_before == quantity * (entry_future_price - 108000)
    assert state["status"] == "OPEN"
    cash_after_first = state_decimal(state, "cash_total")
    assert restarted.ledger.apply_settlement(first, now=next_event) == "replayed"
    assert state_decimal(restarted.ledger.state, "cash_total") == cash_after_first


def test_risk_liquidity_excludes_unrealized_spot_gain_and_includes_short_loss(
    tmp_path: Path,
) -> None:
    store, runner, _, clock = open_position(tmp_path)
    state = runner.ledger.state
    quantity = state_decimal(state, "future_qty")
    reference = state_decimal(state, "future_reference_price")
    mark = reference * Decimal("1.10")
    cash = state_decimal(state, "cash_total")
    spot_basis_market_value = state_decimal(state, "spot_qty") * Decimal("200000")
    clock[0] += timedelta(seconds=1)

    runner.ledger.mark_to_market(spot_mark=Decimal("200000"), future_mark=mark, now=clock[0])
    assessment = runner.margin_policy.assess(quantity, mark)
    cash_after_stress = runner.ledger.record_margin_assessment(
        assessment, future_mark=mark, now=clock[0]
    )

    expected_short_loss = quantity * (mark - reference)
    assert Decimal(runner.ledger.state["equity"]) > cash
    assert spot_basis_market_value > expected_short_loss
    assert (
        state_decimal(runner.ledger.state, "derivative_cash_after_unrealized_short_loss")
        == cash - expected_short_loss
    )
    assert cash_after_stress == (
        cash
        - expected_short_loss
        - assessment.initial_margin_usdc
        - assessment.stress_cash_buffer_usdc
    )


def test_protective_exit_uses_derivative_cash_not_large_spot_mark(tmp_path: Path) -> None:
    _, runner, market, clock = open_position(tmp_path)
    state = runner.ledger.state
    reference = state_decimal(state, "future_reference_price")
    now = NOW + timedelta(seconds=30)
    clock[0] = now
    market.books[SPOT_ID] = book(SPOT_ID, now=now, bid="150000", ask="150100", mark="200000")
    market.books[FUTURE_ID] = book(
        FUTURE_ID,
        now=now,
        bid="137400",
        ask="137600",
        mark=str(reference * Decimal("1.25")),
    )

    runner.run_once()

    state = runner.ledger.state
    assert state["reason_code"] == "PROTECTIVE_MARGIN_STRESS_EXIT"
    assert "Derivative quote cash" in state["reason_message"]
    assert state["status"] == "CLOSING"
    assert state_decimal(state, "future_qty") == 0
    assert state_decimal(state, "spot_qty") > 0


def test_daily_and_final_settlements_telescope_and_late_spot_exit_keeps_dust_visible(
    tmp_path: Path,
) -> None:
    store, runner, market, clock = open_position(tmp_path)
    initial = runner.ledger.state
    initial_future_qty = state_decimal(initial, "future_qty")
    entry_future_price = state_decimal(initial, "future_avg_entry")
    entry_future_fee = Decimal(initial["decision"]["future_entry_fee"])
    entry_spot_fee = Decimal(initial["decision"]["spot_fee"])

    pre_open = settlement(
        key="history-pre-open",
        kind="daily",
        event=datetime(2026, 10, 11, 8, 0, tzinfo=UTC),
        start=datetime(2026, 10, 10, 8, 0, tzinfo=UTC),
        end=datetime(2026, 10, 11, 8, 0, tzinfo=UTC),
        price="90000",
    )
    daily_events: list[DatedSettlement] = []
    day = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    while day <= datetime(2026, 11, 10, 8, 0, tzinfo=UTC):
        daily_events.append(
            settlement(
                key=f"daily-{day.date().isoformat()}",
                kind="daily",
                event=day,
                start=day - timedelta(days=1),
                end=day,
                price="108000",
            )
        )
        day += timedelta(days=1)
    market.settlements[:] = [pre_open, *daily_events]
    clock[0] = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    market.books[SPOT_ID] = book(SPOT_ID, now=clock[0], bid="108000", ask="108100")
    market.books[FUTURE_ID] = book(
        FUTURE_ID,
        now=clock[0],
        bid="108000",
        ask="108100",
        mark="108000",
    )
    runner.run_once()
    daily_state = runner.ledger.state
    assert daily_state["settlements_applied"] == len(daily_events)
    assert daily_state["last_settlement_key"] == daily_events[-1].settlement_key
    assert daily_state["future_reference_price"] == "108000"
    fees_before_expiry = state_decimal(daily_state, "fees_paid")
    assert fees_before_expiry == entry_future_fee + entry_spot_fee
    assert daily_state["delivery_fee_applied"] is None

    final = settlement(
        key="expiry-official",
        kind="expiry",
        event=EXPIRY,
        start=EXPIRY - timedelta(minutes=30),
        end=EXPIRY,
        price="105000",
    )
    market.settlements.append(final)
    clock[0] = EXPIRY + timedelta(minutes=10)
    market.books[SPOT_ID] = book(SPOT_ID, now=clock[0], bid="102000", ask="102100")
    runner.run_once()

    after_expiry = runner.ledger.state
    expected_derivative_pnl = initial_future_qty * (entry_future_price - Decimal("105000"))
    settlement_events = [
        event
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_future_settlement"
    ]
    booked_settlement_pnl = sum(
        Decimal(json.loads(event["payload"])["application"]["cash_pnl"])
        for event in settlement_events
        if json.loads(event["payload"]).get("application")
    )
    spot_exit_fees = sum(
        Decimal(json.loads(event["payload"])["business"]["fee_quote_equivalent"])
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_paper_fill"
        and json.loads(event["payload"]).get("business", {}).get("leg") == "spot"
        and json.loads(event["payload"]).get("business", {}).get("side") == "SELL"
    )
    applied_delivery_fee = initial_future_qty * Decimal("105000") * Decimal("0.00025")
    assert booked_settlement_pnl == expected_derivative_pnl
    assert state_decimal(after_expiry, "delivery_fee_applied") == applied_delivery_fee
    assert (
        state_decimal(after_expiry, "fees_paid") - fees_before_expiry
        == applied_delivery_fee + spot_exit_fees
    )
    assert after_expiry["expiry_spot_liquidation_late"] is True
    assert after_expiry["status"] == "CLOSING"
    assert state_decimal(after_expiry, "future_qty") == 0
    assert state_decimal(after_expiry, "spot_qty") > 0
    assert state_decimal(after_expiry, "spot_qty") < Decimal("0.001")
    assert initial["spot_fee_currency_applied_scenario"] == "BASE"
    assert after_expiry["reason_code"] == "EXPIRY_SPOT_LIQUIDATION_LATE"

    # A sub-minimum BASE-fee remainder remains explicit; it is not called flat.
    runner.run_once()
    assert runner.ledger.state["status"] == "SETTLEMENT_PENDING"
    assert runner.ledger.state["reason_code"] == "SPOT_DUST_REQUIRES_REVIEW"
    assert state_decimal(runner.ledger.state, "spot_qty") > 0


def test_settlement_correction_latches_uncertainty_and_blocks_all_position_writes(
    tmp_path: Path,
) -> None:
    _, runner, _, clock = open_position(tmp_path)
    event_time = NOW + timedelta(days=1, hours=1)
    original = settlement(
        key="stable-daily-id",
        kind="daily",
        event=event_time,
        start=event_time - timedelta(days=1),
        end=event_time,
        price="108000",
    )
    runner.ledger.apply_settlement(original, now=event_time)
    corrected = replace(
        original,
        official_index_price=Decimal("108001"),
        content_hash="f" * 64,
        correction_version="2",
    )

    with pytest.raises(AccountingIdentityCollision):
        runner.ledger.apply_settlement(corrected, now=event_time + timedelta(seconds=2))

    assert runner.ledger.state["status"] == "ACCOUNTING_UNCERTAIN"
    assert runner.ledger.state["accounting_uncertain"] is True
    with pytest.raises(DatedLedgerError, match="ACCOUNTING_UNCERTAIN"):
        runner.ledger.begin_close(
            reason_code="test", reason_message="must not write", now=event_time
        )
    runner.ledger.mark_settlement_pending(
        reason_code="stale", reason_message="must not overwrite latch", now=clock[0]
    )
    assert runner.ledger.state["status"] == "ACCOUNTING_UNCERTAIN"
    assert runner.ledger.state["accounting_uncertain"] is True


def test_missing_daily_settlement_stays_pending_then_recovers_in_order(tmp_path: Path) -> None:
    _, runner, market, clock = open_position(tmp_path)
    day1 = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    day2 = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
    first = settlement(
        key="daily-one",
        kind="daily",
        event=day1,
        start=day1 - timedelta(days=1),
        end=day1,
        price="109000",
    )
    missing_then_second = settlement(
        key="daily-two",
        kind="daily",
        event=day2,
        start=day2 - timedelta(days=1),
        end=day2,
        price="108000",
    )
    market.settlements[:] = [first]
    clock[0] = day1
    market.books[SPOT_ID] = book(SPOT_ID, now=day1, bid="108900", ask="109000")
    market.books[FUTURE_ID] = book(FUTURE_ID, now=day1, bid="109000", ask="109100", mark="109000")
    runner.run_once()
    assert runner.ledger.state["last_settlement_key"] == first.settlement_key

    # Add a third day while leaving the second day's official event unavailable.
    day3 = day2 + timedelta(days=1)
    third = settlement(
        key="daily-three",
        kind="daily",
        event=day3,
        start=day3 - timedelta(days=1),
        end=day3,
        price="107000",
    )
    market.settlements[:] = [first, third]
    clock[0] = day3
    market.books[SPOT_ID] = book(SPOT_ID, now=day3, bid="106900", ask="107000")
    market.books[FUTURE_ID] = book(FUTURE_ID, now=day3, bid="107000", ask="107100", mark="107000")
    runner.run_once()
    assert runner.ledger.state["status"] == "SETTLEMENT_PENDING"
    assert runner.ledger.state["reason_code"] == "DAILY_SETTLEMENT_GAP"
    assert runner.ledger.state["last_settlement_key"] == first.settlement_key
    assert runner.ledger.state["settlements_applied"] == 1

    market.settlements[:] = [first, missing_then_second, third]
    clock[0] = day3 + timedelta(seconds=10)
    market.books[SPOT_ID] = book(SPOT_ID, now=clock[0], bid="106900", ask="107000")
    market.books[FUTURE_ID] = book(
        FUTURE_ID, now=clock[0], bid="107000", ask="107100", mark="107000"
    )
    runner.run_once()
    assert runner.ledger.state["status"] == "OPEN"
    assert runner.ledger.state["last_settlement_key"] == third.settlement_key
    assert runner.ledger.state["settlements_applied"] == 3


def test_transient_stale_books_resume_open_after_settlement_backlog_checked(tmp_path: Path) -> None:
    _, runner, market, clock = open_position(tmp_path)
    stale = NOW + timedelta(seconds=1)
    market.books[SPOT_ID] = book(
        SPOT_ID,
        now=stale,
        bid="99900",
        ask="100000",
        market_time=stale - timedelta(minutes=1),
    )
    market.books[FUTURE_ID] = book(
        FUTURE_ID,
        now=stale,
        bid="110000",
        ask="110100",
        mark="110000",
        market_time=stale - timedelta(minutes=1),
    )
    clock[0] = stale
    runner.run_once()
    assert runner.ledger.state["status"] == "SETTLEMENT_PENDING"
    assert runner.ledger.state["reason_code"] == "STALE_MARKET_RISK_REVIEW"

    fresh = stale + timedelta(seconds=30)
    market.books[SPOT_ID] = book(SPOT_ID, now=fresh, bid="99900", ask="100000")
    market.books[FUTURE_ID] = book(
        FUTURE_ID,
        now=fresh,
        bid="110000",
        ask="110100",
        mark="110000",
    )
    clock[0] = fresh
    runner.run_once()
    assert runner.ledger.state["status"] == "OPEN"
    assert runner.ledger.state["reason_code"] == "POSITION_OPERATIONAL_RECOVERY"


def test_restart_resumes_partial_close_saga_without_duplicate_future_cover(tmp_path: Path) -> None:
    store, runner, market, clock = open_position(tmp_path)
    now = NOW + timedelta(seconds=5)
    runner.ledger.begin_close(
        reason_code="TEST_INTERRUPTED_CLOSE",
        reason_message="fixture simulates a crash between legs",
        now=now,
    )
    future_quantity = state_decimal(runner.ledger.state, "future_qty")
    cover = fill_from_book(
        book=market.books[FUTURE_ID],
        leg="future",
        side="BUY",
        quantity=future_quantity,
        fee_rate=Decimal("0.00035"),
        fee_currency="QUOTE",
        fill_id=runner.ledger.fill_id(leg="future", side="BUY"),
    )
    runner.ledger.apply_fill(cover, timestamp=now)
    assert state_decimal(runner.ledger.state, "future_qty") == 0
    assert state_decimal(runner.ledger.state, "spot_qty") > 0

    clock[0] = now + timedelta(seconds=30)
    market.books[SPOT_ID] = book(SPOT_ID, now=clock[0], bid="99900", ask="100000")
    restarted = CarryDatedRunner(
        store=store,
        market=market,
        fee_schedule=FeeSchedule.standard_deribit(),
        margin_policy=MarginPolicy.deribit_ssm_linear_btc(),
        capital=Decimal("4000"),
        clock=lambda: clock[0],
        status_path=tmp_path / "restarted-close.json",
    )
    restarted.run_once()
    future_cover_events = [
        json.loads(event["payload"])
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_paper_fill"
        and json.loads(event["payload"]).get("business", {}).get("leg") == "future"
        and json.loads(event["payload"]).get("business", {}).get("side") == "BUY"
    ]
    assert len(future_cover_events) == 1
    assert state_decimal(restarted.ledger.state, "future_qty") == 0
    assert state_decimal(restarted.ledger.state, "spot_qty") > 0


def test_partial_opening_leg_is_compensated_after_process_restart(tmp_path: Path) -> None:
    market = make_market()
    store = StateStore(tmp_path / "carry_dated.db")
    fees = FeeSchedule.standard_deribit()
    margin = MarginPolicy.deribit_ssm_linear_btc()
    ledger = CarryDatedLedger(store, Decimal("4000"))
    spot, future = market.instruments
    spot_book = market.books[SPOT_ID]
    future_book = market.books[FUTURE_ID]
    decision = evaluate_entry(
        ((spot, future, spot_book, future_book),),
        capital=Decimal("4000"),
        fee_schedule=fees,
        margin_policy=margin,
        now=NOW,
    )
    assert decision.action == "OPEN"
    tolerance = common_quantity_step(spot.amount_step, future.amount_step) + abs(
        decision.expected_hedge_residual
    )
    assessment = margin.assess(decision.quantity, future_book.mark_price)
    ledger.begin_entry(
        decision=decision,
        spot_instrument=spot,
        future_instrument=future,
        spot_book=spot_book,
        future_book=future_book,
        reserved_margin=assessment.initial_margin_usdc,
        stress_cash_buffer=assessment.stress_cash_buffer_usdc,
        delivery_fee_estimate=decision.delivery_fee_estimate,
        delivery_fee_rate=fees.delivery_rate,
        delivery_fee_model_version=fees.version,
        spot_fee_currency_scenario="BASE",
        hedge_tolerance=tolerance,
    )
    partial_book = book(
        SPOT_ID,
        now=NOW,
        bid="99900",
        ask="100000",
        quantity="0.01",
    )
    partial_fill = fill_from_book(
        book=partial_book,
        leg="spot",
        side="BUY",
        quantity=decision.spot_gross_quantity,
        fee_rate=fees.spot_taker_rate,
        fee_currency="BASE",
        fill_id=ledger.fill_id(leg="spot", side="BUY"),
    )
    assert not partial_fill.complete
    ledger.apply_fill(partial_fill, timestamp=NOW)
    assert ledger.state["status"] == "OPENING"

    clock = [NOW + timedelta(seconds=30)]
    market.books[SPOT_ID] = book(SPOT_ID, now=clock[0], bid="99900", ask="100000")
    restarted = CarryDatedRunner(
        store=store,
        market=market,
        fee_schedule=fees,
        margin_policy=margin,
        capital=Decimal("4000"),
        clock=lambda: clock[0],
        status_path=tmp_path / "partial-restart.json",
    )
    restarted.run_once()

    assert restarted.ledger.state["status"] == "CLOSING"
    assert state_decimal(restarted.ledger.state, "future_qty") == 0
    assert state_decimal(restarted.ledger.state, "spot_qty") > 0
    assert restarted.ledger.state["reason_code"] == "ENTRY_PARTIAL_COMPENSATION"


def test_atomic_event_checkpoint_rolls_back_when_event_append_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "carry_dated.db")
    CarryDatedLedger(store, Decimal("4000"))
    initial = store.load_engine_state("carry_dated")
    assert initial is not None
    changed = dict(initial)
    changed["reason_code"] = "SHOULD_ROLL_BACK"
    event_count = len(store.read_events("carry_dated"))

    def fail_event(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated crash at event append")

    monkeypatch.setattr(store, "_insert_event", fail_event)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.apply_carry_dated_event_and_checkpoint(
            event_type="dated_test_transition",
            aggregate_id="atomic-test",
            business_payload={"amount": "1"},
            state=changed,
        )

    assert store.load_engine_state("carry_dated") == initial
    assert len(store.read_events("carry_dated")) == event_count
    assert not store.has_event("carry_dated", "dated_test_transition", "atomic-test")


def test_base_and_quote_spot_fee_sale_pnl_reconciles_cash_at_different_exit_price(
    tmp_path: Path,
) -> None:
    # Run the same isolated ledger transition under both possible fee assets.
    for currency in ("BASE", "QUOTE"):
        local = tmp_path / currency
        local.mkdir()
        market = make_market()
        store = StateStore(local / "carry_dated.db")
        runner = CarryDatedRunner(
            store=store,
            market=market,
            fee_schedule=FeeSchedule.standard_deribit(),
            margin_policy=MarginPolicy.deribit_ssm_linear_btc(),
            capital=Decimal("4000"),
            clock=lambda: NOW,
            status_path=local / "status.json",
        )
        runner.run_once()
        ledger = runner.ledger
        original_realized = state_decimal(ledger.state, "pnl_realized")
        ledger.state["spot_fee_currency_applied_scenario"] = currency
        # Persist a legal scenario switch only for this isolated arithmetic fixture.
        store.save_carry_dated_checkpoint(ledger.state)
        ledger.state = store.load_engine_state("carry_dated") or ledger.state
        original_cash = state_decimal(ledger.state, "cash_total")
        held = state_decimal(ledger.state, "spot_qty")
        spot_basis = state_decimal(ledger.state, "spot_avg_entry")
        requested = held if currency == "QUOTE" else held / (Decimal("1") + Decimal("0.001"))
        fill = fill_from_book(
            book=book(SPOT_ID, now=NOW, bid="110000", ask="110100"),
            leg="spot",
            side="SELL",
            quantity=requested,
            fee_rate=Decimal("0.001"),
            fee_currency=currency,
            fill_id=f"fee-case:{currency}",
        )
        ledger.begin_close(reason_code="TEST_CLOSE", reason_message="arithmetic fixture", now=NOW)
        ledger.apply_fill(fill, timestamp=NOW)
        new_cash = state_decimal(ledger.state, "cash_total")
        expected_cash_delta = fill.notional
        if currency == "QUOTE":
            expected_cash_delta -= fill.fee_quote_equivalent
        assert new_cash - original_cash == expected_cash_delta
        expected_realized_spot_delta = (
            fill.notional - fill.base_consumed * spot_basis
            if currency == "BASE"
            else fill.filled_qty * (fill.average_price - spot_basis) - fill.fee_quote_equivalent
        )
        assert state_decimal(ledger.state, "pnl_realized") - original_realized == (
            expected_realized_spot_delta
        )


@pytest.mark.parametrize("fee_currency", ("BASE", "QUOTE"))
def test_total_paper_equity_pnl_identity_with_settlements_and_fee_scenarios(
    tmp_path: Path, fee_currency: str
) -> None:
    local = tmp_path / fee_currency
    local.mkdir()
    market = make_market()
    store = StateStore(local / "carry_dated.db")
    ledger = CarryDatedLedger(store, Decimal("4000"))
    fees = FeeSchedule.standard_deribit()
    margin = MarginPolicy.deribit_ssm_linear_btc()
    spot, future = market.instruments
    spot_book = market.books[SPOT_ID]
    future_book = market.books[FUTURE_ID]
    decision = evaluate_entry(
        ((spot, future, spot_book, future_book),),
        capital=Decimal("4000"),
        fee_schedule=fees,
        margin_policy=margin,
        now=NOW,
    )
    assert decision.action == "OPEN"
    assessment = margin.assess(decision.quantity, future_book.mark_price)
    tolerance = common_quantity_step(spot.amount_step, future.amount_step) + abs(
        decision.expected_hedge_residual
    )
    ledger.begin_entry(
        decision=decision,
        spot_instrument=spot,
        future_instrument=future,
        spot_book=spot_book,
        future_book=future_book,
        reserved_margin=assessment.initial_margin_usdc,
        stress_cash_buffer=assessment.stress_cash_buffer_usdc,
        delivery_fee_estimate=decision.delivery_fee_estimate,
        delivery_fee_rate=fees.delivery_rate,
        delivery_fee_model_version=fees.version,
        spot_fee_currency_scenario=fee_currency,
        hedge_tolerance=tolerance,
    )
    spot_buy = fill_from_book(
        book=spot_book,
        leg="spot",
        side="BUY",
        quantity=decision.spot_gross_quantity,
        fee_rate=fees.spot_taker_rate,
        fee_currency=fee_currency,
        fill_id=ledger.fill_id(leg="spot", side="BUY"),
    )
    future_sell = fill_from_book(
        book=future_book,
        leg="future",
        side="SELL",
        quantity=decision.quantity,
        fee_rate=fees.future_taker_rate,
        fee_currency="QUOTE",
        fill_id=ledger.fill_id(leg="future", side="SELL"),
    )
    ledger.apply_fill(spot_buy, timestamp=NOW)
    ledger.apply_fill(future_sell, timestamp=NOW)
    ledger.mark_open(decision_id=decision.decision_id, now=NOW)
    quantity = state_decimal(ledger.state, "future_qty")
    entry_future = state_decimal(ledger.state, "future_avg_entry")

    daily_events: list[DatedSettlement] = []
    day = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    while day <= datetime(2026, 11, 10, 8, 0, tzinfo=UTC):
        item = settlement(
            key=f"{fee_currency}-daily-{day.date().isoformat()}",
            kind="daily",
            event=day,
            start=day - timedelta(days=1),
            end=day,
            price="108000",
        )
        assert ledger.apply_settlement(item, now=day) == "applied"
        daily_events.append(item)
        day += timedelta(days=1)
    cash_after_daily = state_decimal(ledger.state, "cash_total")
    assert (
        ledger.apply_settlement(daily_events[-1], now=daily_events[-1].event_timestamp)
        == "replayed"
    )
    assert state_decimal(ledger.state, "cash_total") == cash_after_daily

    final = settlement(
        key=f"{fee_currency}-expiry",
        kind="expiry",
        event=EXPIRY,
        start=EXPIRY - timedelta(minutes=30),
        end=EXPIRY,
        price="105000",
    )
    assert ledger.apply_settlement(final, now=EXPIRY) == "applied"
    assert state_decimal(ledger.state, "future_qty") == 0
    ledger.begin_close(reason_code="EXPIRY", reason_message="fixture exit", now=EXPIRY)
    spot_qty = state_decimal(ledger.state, "spot_qty")
    sell_qty = (
        floor_to_step(spot_qty / (Decimal("1") + fees.spot_exit_taker_rate), spot.amount_step)
        if fee_currency == "BASE"
        else spot_qty
    )
    spot_exit_book = book(SPOT_ID, now=EXPIRY, bid="102000", ask="102100")
    spot_sell = fill_from_book(
        book=spot_exit_book,
        leg="spot",
        side="SELL",
        quantity=sell_qty,
        fee_rate=fees.spot_exit_taker_rate,
        fee_currency=fee_currency,
        fill_id=ledger.fill_id(leg="spot", side="SELL"),
    )
    ledger.apply_fill(spot_sell, timestamp=EXPIRY)
    ledger.mark_to_market(spot_mark=Decimal("102000"), future_mark=Decimal("105000"), now=EXPIRY)

    settlement_events = [
        json.loads(event["payload"])
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_future_settlement"
    ]
    derivative_variation = sum(
        Decimal(item["application"]["cash_pnl"])
        for item in settlement_events
        if "application" in item
    )
    assert derivative_variation == quantity * (entry_future - Decimal("105000"))
    assert Decimal(ledger.state["equity"]) - Decimal(ledger.state["capital"]) == (
        state_decimal(ledger.state, "pnl_realized") + state_decimal(ledger.state, "pnl_unrealized")
    )


def test_experimental_config_rejects_traversal_and_requires_isolated_db(tmp_path: Path) -> None:
    config_path = tmp_path / "carry.yaml"
    config = {
        "strategy": "carry_btc_dated",
        "mode": "paper",
        "paper_only": True,
        "capital": "4000",
        "database": "state/carry-dated-experiment/carry_dated.db",
        "status_file": "state/carry-dated-experiment/carry_dated_status.json",
        "poll_seconds": 30,
        "max_book_age_seconds": "15",
        "max_book_skew_seconds": "5",
        "max_metadata_age_seconds": "300",
    }
    config_path.write_text(
        "\n".join(f"{key}: {json.dumps(value)}" for key, value in config.items()),
        encoding="utf-8",
    )
    assert load_experimental_config(config_path)["paper_only"] is True
    config["database"] = "state/carry-dated-experiment/../../btcquant.db"
    config_path.write_text(
        "\n".join(f"{key}: {json.dumps(value)}" for key, value in config.items()),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="isolated"):
        load_experimental_config(config_path)
