from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import sqlite3
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
    initial_checkpoint,
    state_decimal,
    utc_string,
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
    taker_commission: str | None = None,
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
        taker_commission=Decimal(taker_commission) if taker_commission is not None else None,
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
    sequence: int | None = 123,
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
        sequence=sequence,
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
        self.requested_depths: list[tuple[str, int]] = []

    def list_active_instruments(self) -> tuple[DatedInstrument, ...]:
        return self.instruments

    def get_book(self, instrument_id: str, *, depth: int = 100) -> DatedBook:
        self.requested_books.append(instrument_id)
        self.requested_depths.append((instrument_id, depth))
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
        return tuple(ordered[cursor:])


class AdvancingFakeMarket(FakeMarket):
    """Model public responses whose receipt timestamps follow the cycle start."""

    def __init__(
        self,
        instruments: tuple[DatedInstrument, ...],
        books: dict[str, DatedBook],
        *,
        timestamp_metadata_on_response: bool,
    ) -> None:
        super().__init__(instruments, books)
        self.clock: list[datetime] | None = None
        self.timestamp_metadata_on_response = timestamp_metadata_on_response

    def list_active_instruments(self) -> tuple[DatedInstrument, ...]:
        if self.timestamp_metadata_on_response:
            self._advance()
            self.instruments = tuple(
                replace(item, metadata_timestamp=self._current_time()) for item in self.instruments
            )
        return self.instruments

    def get_book(self, instrument_id: str, *, depth: int = 100) -> DatedBook:
        super().get_book(instrument_id, depth=depth)
        self._advance()
        received = self._current_time()
        original = self.books[instrument_id]
        return replace(
            original,
            market_timestamp=received - timedelta(milliseconds=500),
            received_timestamp=received,
            content_hash=hashlib.sha256(
                f"{instrument_id}:{received.isoformat()}".encode()
            ).hexdigest(),
        )

    def _advance(self) -> None:
        assert self.clock is not None
        self.clock[0] += timedelta(seconds=2)

    def _current_time(self) -> datetime:
        assert self.clock is not None
        return self.clock[0]


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
    market_evidence_quota_bytes: int = 5 * 1024 * 1024 * 1024,
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
        market_evidence_quota_bytes=market_evidence_quota_bytes,
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
    assert runner.market_snapshot_lock_path == tmp_path / "market.sqlite3.snapshot.lock"

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
    assert market.requested_depths == [(SPOT_ID, 20), (FUTURE_ID, 20)]
    events = store.read_events("carry_dated")
    market_connection = sqlite3.connect(store.path.parent / "market.sqlite3")
    evidence_by_id = {
        payload["evidence_id"]: payload
        for (payload_json,) in market_connection.execute(
            "SELECT payload_json FROM observations WHERE kind='runner_book'"
        )
        for payload in (json.loads(payload_json),)
    }
    market_connection.close()
    decision = next(
        json.loads(event["payload"])["business"]
        for event in events
        if event["event_type"] == "dated_carry_decision"
    )
    assert len(decision["market_evidence_ids"]) == 2
    assert all(evidence_id in evidence_by_id for evidence_id in decision["market_evidence_ids"])
    assert {
        evidence_by_id[evidence_id]["book_snapshot"]["instrument_id"]
        for evidence_id in decision["market_evidence_ids"]
    } == {SPOT_ID, FUTURE_ID}
    entry_intent = next(
        json.loads(event["payload"])["business"]
        for event in events
        if event["event_type"] == "dated_entry_intent"
    )
    assert len(entry_intent["market_evidence_ids"]) == 2
    assert all(evidence_id in evidence_by_id for evidence_id in entry_intent["market_evidence_ids"])
    entry_fills = [
        json.loads(event["payload"])["business"]
        for event in events
        if event["event_type"] == "dated_paper_fill"
    ]
    assert len(entry_fills) == 2
    for fill in entry_fills:
        evidence = evidence_by_id[fill["market_evidence_id"]]
        assert "book_snapshot" not in fill
        assert fill["book_snapshot_sha256"] == evidence["snapshot_sha256"]
        assert evidence["book_snapshot"]["instrument_id"] == fill["instrument_id"]
    market_connection = sqlite3.connect(store.path.parent / "market.sqlite3")
    assert (
        market_connection.execute(
            "SELECT COUNT(*) FROM observations WHERE kind='runner_book_use'"
        ).fetchone()[0]
        == 0
    )
    market_connection.close()


@pytest.mark.parametrize("timestamp_metadata_on_response", [False, True])
def test_entry_uses_clock_after_public_market_responses(
    tmp_path: Path, timestamp_metadata_on_response: bool
) -> None:
    base = make_market()
    market = AdvancingFakeMarket(
        base.instruments,
        base.books,
        timestamp_metadata_on_response=timestamp_metadata_on_response,
    )
    status_path = tmp_path / "carry_dated_status.json"
    _, runner, clock = make_runner(tmp_path, market, status_path=status_path)
    market.clock = clock

    snapshot = runner.run_once()

    assert snapshot["status"] == "OPEN"
    assert snapshot["reason_code"] == "ECONOMIC_AND_CASH_GATES_PASSED_UNDER_PAPER_ASSUMPTIONS"
    status_snapshot = json.loads(status_path.read_text())
    status_time = datetime.fromisoformat(
        status_snapshot["status_updated_at"].replace("Z", "+00:00")
    )
    assert status_time == clock[0]
    assert clock[0] > NOW


def test_trailing_zero_book_decimals_keep_fill_and_evidence_hashes_identical(
    tmp_path: Path,
) -> None:
    market = make_market()
    market.books[SPOT_ID] = replace(
        market.books[SPOT_ID],
        bids=(BookLevel(Decimal("99900.00"), Decimal("0.0500")),),
        asks=(BookLevel(Decimal("100000.00"), Decimal("0.0500")),),
    )
    market.books[FUTURE_ID] = replace(
        market.books[FUTURE_ID],
        bids=(BookLevel(Decimal("110000.00"), Decimal("0.0500")),),
        asks=(BookLevel(Decimal("110100.00"), Decimal("0.0500")),),
        mark_price=Decimal("110000.00"),
        index_price=Decimal("100000.00"),
    )
    store, runner, _ = make_runner(tmp_path, market)

    assert runner.run_once()["status"] == "OPEN"
    fills = [
        json.loads(event["payload"])["business"]
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_paper_fill"
    ]
    with sqlite3.connect(store.path.parent / "market.sqlite3") as connection:
        evidence_by_id = {
            payload["evidence_id"]: payload
            for (raw,) in connection.execute(
                "SELECT payload_json FROM observations WHERE kind='runner_book'"
            )
            for payload in (json.loads(raw),)
        }

    assert len(fills) == 2
    for fill in fills:
        evidence = evidence_by_id[fill["market_evidence_id"]]
        expected_bid = "99900" if fill["leg"] == "spot" else "110000"
        assert fill["book_snapshot_sha256"] == evidence["snapshot_sha256"]
        assert evidence["book_snapshot"]["bids"][0]["price"] == expected_bid
        assert evidence["book_snapshot"]["bids"][0]["quantity"] == "0.05"


def test_position_risk_control_books_are_requested_and_persisted_at_depth_20(
    tmp_path: Path,
) -> None:
    store, runner, market, clock = open_position(tmp_path)
    clock[0] += timedelta(seconds=1)

    runner.run_once()

    assert market.requested_depths[-2:] == [(SPOT_ID, 20), (FUTURE_ID, 20)]
    market_connection = sqlite3.connect(store.path.parent / "market.sqlite3")
    evidence_by_id = {
        payload["evidence_id"]: payload
        for (payload_json,) in market_connection.execute(
            "SELECT payload_json FROM observations WHERE kind='runner_book'"
        )
        for payload in (json.loads(payload_json),)
    }
    evidence_count = market_connection.execute(
        "SELECT COUNT(*) FROM observations WHERE kind='runner_book'"
    ).fetchone()[0]
    market_connection.close()
    assert evidence_count == 2
    assert {item["book_snapshot"]["instrument_id"] for item in evidence_by_id.values()} == {
        SPOT_ID,
        FUTURE_ID,
    }


def test_runner_rejects_book_response_wider_than_frozen_depth(tmp_path: Path) -> None:
    market = make_market()
    original = market.books[SPOT_ID]
    market.books[SPOT_ID] = replace(
        original,
        bids=tuple(BookLevel(Decimal(100_000 - level), Decimal("0.01")) for level in range(21)),
    )
    _, runner, _ = make_runner(tmp_path, market)

    with pytest.raises(DatedLedgerError, match="frozen 20-level book"):
        runner._get_book(SPOT_ID)

    assert market.requested_depths == [(SPOT_ID, 20)]


def test_market_evidence_quota_blocks_entry_and_leaves_observable_wait(tmp_path: Path) -> None:
    market = make_market()
    store, runner, _ = make_runner(tmp_path, market)
    runner.ledger.market_evidence_recorder.max_total_bytes = 1

    snapshot = runner.run_once()

    assert snapshot["status"] == "FLAT"
    assert snapshot["reason_code"] == "MARKET_EVIDENCE_QUOTA_EXCEEDED"
    assert state_decimal(snapshot, "spot_qty") == 0
    assert state_decimal(snapshot, "future_qty") == 0
    assert not any(
        event["event_type"] == "dated_entry_intent" for event in store.read_events("carry_dated")
    )
    with sqlite3.connect(store.path.parent / "market.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM observations WHERE kind='runner_book'"
            ).fetchone()[0]
            == 0
        )


def test_market_evidence_quota_on_open_position_blocks_writes_without_losing_fills(
    tmp_path: Path,
) -> None:
    store, runner, market, clock = open_position(tmp_path)
    initial_fills = [
        event
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_paper_fill"
    ]
    cash_before = state_decimal(runner.ledger.state, "cash_total")
    runner.ledger.market_evidence_recorder.max_total_bytes = 1
    clock[0] += timedelta(seconds=1)

    snapshot = runner.run_once()

    assert snapshot["status"] == "OPEN"
    assert snapshot["market_evidence_gap"]["reason_code"] == "MARKET_EVIDENCE_QUOTA_EXCEEDED"
    assert state_decimal(snapshot, "cash_total") == cash_before
    assert len(
        [
            event
            for event in store.read_events("carry_dated")
            if event["event_type"] == "dated_paper_fill"
        ]
    ) == len(initial_fills)
    assert market.requested_depths[-2:] == [(SPOT_ID, 20), (FUTURE_ID, 20)]


def test_close_keeps_full_book_evidence_when_archive_quota_is_exhausted(
    tmp_path: Path,
) -> None:
    store, runner, _, clock = open_position(tmp_path)
    runner.ledger.market_evidence_recorder.max_total_bytes = 1
    runner.ledger.begin_close(
        reason_code="TEST_PROTECTIVE_CLOSE",
        reason_message="verify risk exit remains auditable at the evidence quota",
        now=clock[0],
    )

    snapshot = runner.run_once()

    assert Decimal("0") < state_decimal(snapshot, "spot_qty") < Decimal("0.001")
    assert state_decimal(snapshot, "future_qty") == 0
    assert snapshot["status"] == "CLOSING"
    close_fills = [
        json.loads(event["payload"])["business"]
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_paper_fill"
        and json.loads(event["payload"])["business"]["side"] in {"BUY", "SELL"}
    ][-2:]
    assert len(close_fills) == 2
    assert all(fill["market_evidence_id"] is None for fill in close_fills)
    assert all(
        fill["book_snapshot"]["instrument_id"] in {SPOT_ID, FUTURE_ID} for fill in close_fills
    )
    assert all(fill["book_snapshot_sha256"] for fill in close_fills)


def test_effective_instrument_fees_are_persisted_and_applied_on_entry_and_exit(
    tmp_path: Path,
) -> None:
    market = make_market()
    market.instruments = tuple(
        replace(item, taker_commission=Decimal("0.01")) for item in market.instruments
    )
    store, runner, clock = make_runner(tmp_path, market)
    opened = runner.run_once()
    assert opened["status"] == "OPEN"
    assert opened["spot_entry_fee_rate"] == "0.01"
    assert opened["future_taker_fee_rate"] == "0.01"
    assert opened["spot_exit_fee_rate"] == "0.01"
    assert Decimal(opened["decision"]["spot_fee"]) > Decimal("0")
    assert Decimal(opened["decision"]["future_entry_fee"]) > Decimal("0")

    entry_fills = [
        json.loads(event["payload"])["business"]
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_paper_fill"
    ]
    assert {fill["fee_rate"] for fill in entry_fills} == {"0.01"}
    entry_fees = state_decimal(runner.ledger.state, "fees_paid")

    close_at = NOW + timedelta(seconds=10)
    clock[0] = close_at
    market.books[SPOT_ID] = book(SPOT_ID, now=close_at, bid="109900", ask="110000")
    market.books[FUTURE_ID] = book(
        FUTURE_ID, now=close_at, bid="109900", ask="110000", mark="109900"
    )
    runner.ledger.begin_close(
        reason_code="TEST_EXIT",
        reason_message="exercise persisted instrument fee rates",
        now=close_at,
    )
    runner.run_once()
    runner.run_once()

    close_fills = [
        json.loads(event["payload"])["business"]
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_paper_fill"
    ]
    assert len(close_fills) >= 4
    assert {fill["fee_rate"] for fill in close_fills} == {"0.01"}
    assert runner.ledger.state["status"] == "FLAT"
    assert state_decimal(runner.ledger.state, "spot_dust_qty") > 0
    assert state_decimal(runner.ledger.state, "fees_paid") > entry_fees
    assert state_decimal(runner.ledger.state, "fees_paid") == sum(
        Decimal(fill["fee_quote_equivalent"]) for fill in close_fills
    )


def test_fill_event_references_verifiable_book_and_consumed_depth(tmp_path: Path) -> None:
    store, runner, _, _ = open_position(tmp_path)
    event = next(
        event
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_paper_fill"
    )
    business = json.loads(event["payload"])["business"]
    assert business["execution_kind"] == "PAPER_DEPTH_SIMULATION"
    assert business["market_evidence_id"]
    assert "book_snapshot" not in business
    with sqlite3.connect(store.path.parent / "market.sqlite3") as market_connection:
        rows = market_connection.execute(
            "SELECT payload_json FROM observations WHERE kind='runner_book'"
        ).fetchall()
    evidence = next(
        json.loads(payload_json)
        for (payload_json,) in rows
        if json.loads(payload_json)["evidence_id"] == business["market_evidence_id"]
    )
    snapshot = evidence["book_snapshot"]
    assert business["book_hash"] == snapshot["content_hash"]
    assert business["book_source"] == snapshot["source"]
    assert business["book_sequence"] == snapshot["sequence"]
    assert business["market_timestamp"] == snapshot["market_timestamp"]
    assert business["book_received_timestamp"] == snapshot["received_timestamp"]
    snapshot_hash = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert business["book_snapshot_sha256"] == snapshot_hash == evidence["snapshot_sha256"]
    consumed = business["consumed_levels"]
    replayed_qty = sum(Decimal(level["quantity"]) for level in consumed)
    replayed_notional = sum(
        Decimal(level["price"]) * Decimal(level["quantity"]) for level in consumed
    )
    assert replayed_qty == Decimal(business["filled_qty"])
    assert replayed_notional == (
        Decimal(business["filled_qty"]) * Decimal(business["average_price"])
    )
    assert runner.ledger.state["paper_only"] is True


def test_flat_runner_sweeps_accumulated_dust_and_keeps_remaining_value(tmp_path: Path) -> None:
    market = make_market()
    spot = market.instruments[0]
    market.instruments = (spot,)
    store = StateStore(tmp_path / "carry_dated.db")
    state = initial_checkpoint(Decimal("4000"), now=NOW)
    state.update(
        {
            "spot_dust_qty": "0.002",
            "spot_dust_avg_entry": "90000",
            "spot_dust_mark": "99900",
            "spot_dust_mark_timestamp": utc_string(NOW),
            "spot_dust_instrument_id": SPOT_ID,
            "pnl_unrealized": "19.8",
            # The dust was acquired from the original allocation; its 180 USDC
            # basis has already left cash and must not be counted twice.
            "cash_total": "3820",
            "free_cash": "3820",
            "equity": "4019.8",
        }
    )
    store.save_carry_dated_checkpoint(state)
    _, runner, clock = make_runner(tmp_path, market)
    clock[0] = NOW + timedelta(seconds=5)
    market.books[SPOT_ID] = book(SPOT_ID, now=clock[0], bid="99900", ask="100000")

    runner.run_once()

    current = runner.ledger.state
    assert current["status"] == "FLAT"
    assert state_decimal(current, "spot_qty") == 0
    assert Decimal("0") < state_decimal(current, "spot_dust_qty") < Decimal("0.001")
    assert current["reason_code"] == "NO_ELIGIBLE_DATED_FUTURE"
    assert Decimal(current["equity"]) - Decimal(current["capital"]) == (
        state_decimal(current, "pnl_realized") + state_decimal(current, "pnl_unrealized")
    )
    dust_events = [
        json.loads(event["payload"])["business"]
        for event in store.read_events("carry_dated")
        if event["event_type"] == "dated_spot_dust_sweep_fill"
    ]
    assert len(dust_events) == 1
    assert dust_events[0]["fee_rate"] == "0.0005"
    assert dust_events[0]["book_snapshot_sha256"]


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
    assert market.requested_depths == [(SPOT_ID, 20), (FUTURE_ID, 20)]
    with sqlite3.connect(runner.store.path.parent / "market.sqlite3") as connection:
        evidence = [
            json.loads(payload_json)
            for (payload_json,) in connection.execute(
                "SELECT payload_json FROM observations WHERE kind='runner_book'"
            )
        ]
    assert {item["book_snapshot"]["instrument_id"] for item in evidence} == {SPOT_ID, FUTURE_ID}
    assert all(item["requested_depth"] == 20 for item in evidence)
    assert all(item["protocol_version"] == "carry-dated-market-evidence-v2" for item in evidence)
    assert all(item["snapshot_sha256"] for item in evidence)
    assert runner.ledger.state["market_evidence_protocol_version"] == (
        "carry-dated-market-evidence-v2"
    )
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
    next_event = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    first = settlement(
        key="first-after-position",
        kind="daily",
        event=next_event,
        start=next_event - timedelta(days=1),
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
    day = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
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

    # A sub-minimum BASE-fee remainder is segregated and the carry cycle closes.
    runner.run_once()
    assert runner.ledger.state["status"] == "FLAT"
    assert runner.ledger.state["reason_code"] == "CLOSED_WITH_SPOT_DUST"
    assert state_decimal(runner.ledger.state, "spot_qty") == 0
    assert Decimal(runner.ledger.state["spot_dust_qty"]) > 0
    assert Decimal(runner.ledger.state["spot_dust_qty"]) < Decimal("0.001")
    assert Decimal(runner.ledger.state["equity"]) == (
        state_decimal(runner.ledger.state, "cash_total")
        + Decimal(runner.ledger.state["spot_dust_qty"])
        * Decimal(runner.ledger.state["spot_dust_mark"])
    )


def test_settlement_correction_latches_uncertainty_and_blocks_all_position_writes(
    tmp_path: Path,
) -> None:
    _, runner, _, clock = open_position(tmp_path)
    event_time = NOW + timedelta(hours=1)
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
    day1 = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    day2 = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
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


def test_missing_first_daily_settlement_is_pending_then_catches_up_without_fake_pnl(
    tmp_path: Path,
) -> None:
    _, runner, market, clock = open_position(tmp_path)
    first_expected = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    last_available = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
    late = settlement(
        key="daily-after-initial-gap",
        kind="daily",
        event=last_available,
        start=last_available - timedelta(days=1),
        end=last_available,
        price="107000",
    )
    cash_before = state_decimal(runner.ledger.state, "cash_total")
    market.settlements[:] = [late]
    clock[0] = last_available
    market.books[SPOT_ID] = book(SPOT_ID, now=last_available, bid="106900", ask="107000")
    market.books[FUTURE_ID] = book(
        FUTURE_ID, now=last_available, bid="107000", ask="107100", mark="107000"
    )

    runner.run_once()
    assert runner.ledger.state["status"] == "SETTLEMENT_PENDING"
    assert runner.ledger.state["reason_code"] == "DAILY_SETTLEMENT_GAP"
    assert runner.ledger.state["last_settlement_key"] is None
    assert runner.ledger.state["settlements_applied"] == 0
    assert state_decimal(runner.ledger.state, "cash_total") == cash_before

    complete_history = []
    event_time = first_expected
    while event_time <= last_available:
        complete_history.append(
            settlement(
                key=f"daily-recovered-{event_time.date().isoformat()}",
                kind="daily",
                event=event_time,
                start=event_time - timedelta(days=1),
                end=event_time,
                price="108000",
            )
        )
        event_time += timedelta(days=1)
    market.settlements[:] = complete_history
    runner.run_once()
    assert runner.ledger.state["status"] == "OPEN"
    assert runner.ledger.state["settlements_applied"] == len(complete_history)
    assert runner.ledger.state["last_settlement_key"] == complete_history[-1].settlement_key
    assert state_decimal(runner.ledger.state, "cash_total") != cash_before


def test_runner_rechecks_applied_settlement_overlap_for_late_corrections(tmp_path: Path) -> None:
    _, runner, market, clock = open_position(tmp_path)
    day1 = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
    day2 = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    first = settlement(
        key="audit-daily-one",
        kind="daily",
        event=day1,
        start=day1 - timedelta(days=1),
        end=day1,
        price="109000",
    )
    second = settlement(
        key="audit-daily-two",
        kind="daily",
        event=day2,
        start=day2 - timedelta(days=1),
        end=day2,
        price="108000",
    )
    runner.ledger.apply_settlement(first, now=day1)
    runner.ledger.apply_settlement(second, now=day2)
    assert runner.ledger.state["settlement_audit_start_key"] == first.settlement_key
    corrected_first = replace(
        first,
        official_index_price=Decimal("109001"),
        content_hash="e" * 64,
        correction_version="2",
    )
    market.settlements[:] = [corrected_first, second]
    clock[0] = day2 + timedelta(seconds=1)
    market.books[SPOT_ID] = book(SPOT_ID, now=clock[0], bid="107900", ask="108000")
    market.books[FUTURE_ID] = book(
        FUTURE_ID, now=clock[0], bid="108000", ask="108100", mark="108000"
    )

    runner.run_once()
    assert runner.ledger.state["status"] == "ACCOUNTING_UNCERTAIN"
    assert runner.ledger.state["accounting_uncertain"] is True
    assert runner.ledger.state["incident"]["kind"] == "DATED_EVENT_IDENTITY_COLLISION"
    correction = runner.ledger.state["incident"]["settlement_correction"]
    assert correction["previous_business"]["official_index_price"] == "109000"
    assert correction["received_business"]["official_index_price"] == "109001"
    collision_events = [
        json.loads(event["payload"])
        for event in runner.store.read_events("carry_dated")
        if event["event_type"] == "accounting_identity_collision"
    ]
    assert collision_events[-1]["settlement_correction"] == correction


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
        spot_entry_fee_rate=max(fees.spot_taker_rate, spot.taker_commission or Decimal("0")),
        future_taker_fee_rate=max(fees.future_taker_rate, future.taker_commission or Decimal("0")),
        spot_exit_fee_rate=max(fees.spot_exit_taker_rate, spot.taker_commission or Decimal("0")),
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
        spot_entry_fee_rate=max(fees.spot_taker_rate, spot.taker_commission or Decimal("0")),
        future_taker_fee_rate=max(fees.future_taker_rate, future.taker_commission or Decimal("0")),
        spot_exit_fee_rate=max(fees.spot_exit_taker_rate, spot.taker_commission or Decimal("0")),
        spot_fee_currency_scenario=fee_currency,
        hedge_tolerance=tolerance,
    )
    spot_buy = fill_from_book(
        book=spot_book,
        leg="spot",
        side="BUY",
        quantity=decision.spot_gross_quantity,
        fee_rate=max(fees.spot_taker_rate, spot.taker_commission or Decimal("0")),
        fee_currency=fee_currency,
        fill_id=ledger.fill_id(leg="spot", side="BUY"),
    )
    future_sell = fill_from_book(
        book=future_book,
        leg="future",
        side="SELL",
        quantity=decision.quantity,
        fee_rate=max(fees.future_taker_rate, future.taker_commission or Decimal("0")),
        fee_currency="QUOTE",
        fill_id=ledger.fill_id(leg="future", side="SELL"),
    )
    ledger.apply_fill(spot_buy, timestamp=NOW)
    ledger.apply_fill(future_sell, timestamp=NOW)
    ledger.mark_open(decision_id=decision.decision_id, now=NOW)
    quantity = state_decimal(ledger.state, "future_qty")
    entry_future = state_decimal(ledger.state, "future_avg_entry")

    daily_events: list[DatedSettlement] = []
    day = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
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
        "book_depth_levels": 20,
        "market_evidence_protocol_version": "carry-dated-market-evidence-v2",
        "market_evidence_quota_bytes": 5 * 1024 * 1024 * 1024,
        "market_evidence_min_free_bytes": 10 * 1024 * 1024 * 1024,
    }
    config_path.write_text(
        "\n".join(f"{key}: {json.dumps(value)}" for key, value in config.items()),
        encoding="utf-8",
    )
    assert load_experimental_config(config_path)["paper_only"] is True
    config["market_evidence_min_free_bytes"] = 2 * 1024 * 1024 * 1024
    config_path.write_text(
        "\n".join(f"{key}: {json.dumps(value)}" for key, value in config.items()),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="10 GiB"):
        load_experimental_config(config_path)
    config["market_evidence_min_free_bytes"] = 10 * 1024 * 1024 * 1024
    config["database"] = "state/carry-dated-experiment/../../btcquant.db"
    config_path.write_text(
        "\n".join(f"{key}: {json.dumps(value)}" for key, value in config.items()),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="isolated"):
        load_experimental_config(config_path)
