"""Tests de la frontière read-only des fills individuels."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import ccxt
import pytest

from btcquant.execution.errors import ExternalFillConflict
from btcquant.execution.external_fill_evidence_reader import (
    CcxtExternalFillEvidenceReader,
    ExternalFillEvidencePersistence,
    FillEvidenceLookup,
    FillEvidenceLookupOutcome,
    FillLookupContext,
)
from btcquant.execution.state_store import StateStore


OBSERVED = "2026-08-30T12:00:00Z"
START = 1_700_000_000_000
END = START + 10_000
OID = "9001"


def context(**changes) -> FillLookupContext:
    values = {
        "local_order_id": 1,
        "intent_id": "intent-fill-reader",
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": "SELL",
        "expected_client_order_id": "0x" + "a" * 32,
        "expected_external_order_id": OID,
        "start_time_ms": START,
        "end_time_ms": END,
        "engine": "trend",
    }
    values.update(changes)
    return FillLookupContext(**values)


_RAW_UNSET = object()


def trade(
    *,
    oid: int | str = 9001,
    tid: int = 10001,
    amount: float = 0.25,
    price: float = 100_000.0,
    timestamp: int = START + 1_000,
    fee: str | None = "-1.25",
    fee_asset: str | None = "USDC",
    side: str = "sell",
    raw_side: str = "A",
    symbol: str = "BTC/USDC:USDC",
    raw_coin: str = "BTC",
    raw_oid: int | str | None | object = _RAW_UNSET,
    raw_tid: int | None | object = _RAW_UNSET,
    raw_amount: float | str | None | object = _RAW_UNSET,
    raw_price: float | str | None | object = _RAW_UNSET,
    raw_timestamp: int | float | str | None | object = _RAW_UNSET,
    raw_fee: float | str | None | object = _RAW_UNSET,
    raw_fee_asset: str | None | object = _RAW_UNSET,
    client_order_id: str | None = None,
    raw_cloid: str | None = None,
) -> dict:
    info = {
        "coin": raw_coin,
        "px": raw_price if raw_price is not _RAW_UNSET else str(price),
        "sz": raw_amount if raw_amount is not _RAW_UNSET else str(amount),
        "side": raw_side,
        "time": raw_timestamp if raw_timestamp is not _RAW_UNSET else timestamp,
        "fee": raw_fee if raw_fee is not _RAW_UNSET else fee,
        "feeToken": raw_fee_asset if raw_fee_asset is not _RAW_UNSET else fee_asset,
        "oid": raw_oid if raw_oid is not _RAW_UNSET else oid,
        "tid": raw_tid if raw_tid is not _RAW_UNSET else tid,
    }
    if raw_cloid is not None:
        info["cloid"] = raw_cloid
    value = {
        "id": str(tid),
        "order": str(oid) if oid is not None else None,
        "symbol": symbol,
        "side": side,
        "amount": amount,
        "price": price,
        "timestamp": timestamp,
        "fee": {"cost": fee, "currency": fee_asset},
        "info": info,
    }
    if client_order_id is not None:
        value["clientOrderId"] = client_order_id
    return value


class FakeExchange:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else [trade()]
        self.error = error
        self.calls: list[tuple] = []

    def fetch_my_trades(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.response


def read(response=None, error=None, **context_changes):
    exchange = FakeExchange(response, error)
    result = CcxtExternalFillEvidenceReader(exchange).lookup_fills(
        context(**context_changes), observed_at=OBSERVED
    )
    return exchange, result


def _order(store: StateStore, intent_id: str = "intent-fill-reader") -> int:
    return store.begin_order("trend", "slot", intent_id, "MARKET", "SELL", 1.0, "exit")


def _lookup_events(store: StateStore) -> list[dict]:
    return [
        event
        for event in store.read_events("trend")
        if event["aggregate_type"] == "external_fill_lookup"
    ]


def _lookup_for_store(store: StateStore, response=None, error=None):
    orders = store.read_orders("trend")
    order_id = orders[0]["id"] if orders else _order(store)
    exchange = FakeExchange(response, error)
    lookup = CcxtExternalFillEvidenceReader(exchange).lookup_fills(
        context(local_order_id=order_id), observed_at=OBSERVED
    )
    return order_id, exchange, lookup


def test_context_requires_bounded_integer_window_and_external_oid():
    with pytest.raises(ValueError):
        context(start_time_ms=True)
    with pytest.raises(ValueError):
        context(start_time_ms=-1)
    with pytest.raises(ValueError):
        context(start_time_ms=END, end_time_ms=START)
    with pytest.raises(ValueError):
        context(expected_external_order_id="")


def test_valid_one_fill_uses_one_non_aggregated_bounded_call():
    exchange, result = read()

    assert result.outcome == FillEvidenceLookupOutcome.FOUND
    assert result.response_count == 1
    assert result.response_limit == 2000
    assert result.response_limit_reached is False
    assert result.retention_limit == 10_000
    assert result.absence_authoritative is False
    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.external_order_id == OID
    assert fill.venue_fill_id is None
    assert result.venue_fill_id_candidates == ("10001",)
    assert fill.client_order_id is None
    assert fill.side == "SELL"
    assert fill.quantity == pytest.approx(0.25)
    assert fill.price == pytest.approx(100_000.0)
    assert fill.fee == pytest.approx(-1.25)
    assert fill.fee_asset == "USDC"
    assert exchange.calls == [
        (
            None,
            START,
            2000,
            {"until": END, "aggregateByTime": False},
        )
    ]


def test_valid_multiple_fills_same_oid_remain_individual():
    first = trade(tid=10001, amount=0.25, price=100_000.0, timestamp=START + 1_000)
    second = trade(
        tid=10002,
        amount=0.15,
        price=100_100.0,
        timestamp=START + 2_000,
    )
    _, result = read([first, second])

    assert result.outcome == FillEvidenceLookupOutcome.FOUND
    assert result.matched_count == 2
    assert [fill.external_order_id for fill in result.fills] == [OID, OID]
    assert [candidate for candidate in result.venue_fill_id_candidates] == ["10001", "10002"]
    assert len({fill.fill_key for fill in result.fills}) == 2
    assert [fill.quantity for fill in result.fills] == [0.25, 0.15]
    assert [fill.price for fill in result.fills] == [100_000.0, 100_100.0]
    assert [fill.venue_event_at for fill in result.fills][0] != [
        fill.venue_event_at for fill in result.fills
    ][1]


def test_unrelated_oid_is_ignored_and_is_not_an_error():
    _, result = read([trade(oid=8000, raw_oid=8000)])

    assert result.outcome == FillEvidenceLookupOutcome.NO_MATCH
    assert result.fills == ()
    assert result.absence_authoritative is False


def test_no_oid_row_is_incomplete_and_not_persistable_as_trusted_fill():
    row = trade(oid=None, raw_oid=None)
    _, result = read([row])

    assert result.outcome == FillEvidenceLookupOutcome.INCOMPLETE_RESPONSE
    assert result.fills == ()
    assert result.absence_authoritative is False


def test_unified_and_raw_oid_conflict_is_not_repaired():
    _, result = read([trade(oid=9001, raw_oid=9002)])

    assert result.outcome == FillEvidenceLookupOutcome.CONFLICTING_RESPONSE
    assert result.fills == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "ETH/USDC:USDC"},
        {"side": "buy", "raw_side": "B"},
        {"amount": 0.20, "raw_amount": "0.25"},
        {"price": 100_001.0, "raw_price": "100000"},
        {"timestamp": START + 2_000, "raw_timestamp": START + 1_000},
        {"fee": "-1.20", "raw_fee": "-1.25"},
        {"fee_asset": "HYPE", "raw_fee_asset": "USDC"},
    ],
)
def test_target_unified_raw_or_context_contradiction_fails_closed(changes):
    _, result = read([trade(**changes)])

    assert result.outcome == FillEvidenceLookupOutcome.CONFLICTING_RESPONSE
    assert result.fills == ()


def test_raw_side_a_maps_to_sell_and_raw_side_b_maps_to_buy():
    _, sell = read([trade(side=None, raw_side="A")], side="SELL")
    _, buy = read([trade(side=None, raw_side="B")], side="BUY")

    assert sell.outcome == FillEvidenceLookupOutcome.FOUND
    assert sell.fills[0].side == "SELL"
    assert buy.outcome == FillEvidenceLookupOutcome.FOUND
    assert buy.fills[0].side == "BUY"


@pytest.mark.parametrize("fee", ["-1.25", "0", "2.5", None])
def test_signed_zero_positive_and_missing_fees_are_preserved(fee):
    raw_fee_asset = "USDC" if fee is not None else None
    _, result = read(
        [trade(fee=fee, raw_fee=fee, fee_asset=raw_fee_asset, raw_fee_asset=raw_fee_asset)]
    )

    assert result.outcome == FillEvidenceLookupOutcome.FOUND
    assert result.fills[0].fee == (None if fee is None else pytest.approx(float(fee)))


def test_fee_token_without_fee_is_incomplete():
    _, result = read([trade(fee=None, raw_fee=None, fee_asset="USDC", raw_fee_asset="USDC")])

    assert result.outcome == FillEvidenceLookupOutcome.INCOMPLETE_RESPONSE
    assert result.fills == ()


def test_expected_cloid_is_not_fabricated_and_tid_is_only_candidate():
    _, result = read([trade()])

    assert result.outcome == FillEvidenceLookupOutcome.FOUND
    assert result.fills[0].client_order_id is None
    assert result.fills[0].venue_fill_id is None
    assert result.venue_fill_id_candidates == ("10001",)


def test_identical_redelivery_has_deterministic_hash_and_fallback_fill_key():
    first = trade()
    reordered = {key: first[key] for key in reversed(tuple(first))}
    reordered["info"] = {key: first["info"][key] for key in reversed(tuple(first["info"]))}
    _, first_result = read([first])
    _, second_result = read([reordered])

    assert first_result.outcome == FillEvidenceLookupOutcome.FOUND
    assert second_result.outcome == FillEvidenceLookupOutcome.FOUND
    assert first_result.fills[0].raw_payload_hash == second_result.fills[0].raw_payload_hash
    assert first_result.fills[0].fill_key == second_result.fills[0].fill_key


def test_account_wide_mixed_symbols_are_counted_before_filtering():
    response = [
        trade(
            oid=10_000 + index,
            raw_oid=10_000 + index,
            tid=index,
            symbol="ETH/USDC:USDC",
            raw_coin="ETH",
        )
        for index in range(1_000)
    ] + [
        trade(
            oid=11_000 + index,
            raw_oid=11_000 + index,
            tid=1_000 + index,
            symbol="SOL/USDC:USDC",
            raw_coin="SOL",
        )
        for index in range(1_000)
    ]
    exchange, result = read(response)

    assert exchange.calls[0][0] is None
    assert result.response_count == 2_000
    assert result.response_limit_reached is True
    assert result.outcome == FillEvidenceLookupOutcome.INCOMPLETE_RESPONSE
    assert result.absence_authoritative is False


def test_response_at_limit_without_target_is_incomplete_not_zero_fill():
    response = [trade(oid=10000 + index, raw_oid=10000 + index, tid=index) for index in range(2000)]
    _, result = read(response)

    assert result.outcome == FillEvidenceLookupOutcome.INCOMPLETE_RESPONSE
    assert result.response_count == 2000
    assert result.response_limit_reached is True
    assert result.absence_authoritative is False
    assert not hasattr(result, "zero_fill_proven")


def test_response_at_limit_with_target_allows_positive_found_but_marks_truncation():
    response = [trade()] + [
        trade(oid=10000 + index, raw_oid=10000 + index, tid=index) for index in range(1999)
    ]
    _, result = read(response)

    assert result.outcome == FillEvidenceLookupOutcome.FOUND
    assert result.matched_count == 1
    assert result.response_limit_reached is True
    assert result.absence_authoritative is False


def test_transport_failure_is_explicit_and_has_no_hidden_retry():
    exchange, result = read(error=ccxt.RequestTimeout("timeout"))

    assert result.outcome == FillEvidenceLookupOutcome.TRANSPORT_FAILURE
    assert result.retryable is True
    assert len(exchange.calls) == 1


def test_unsupported_reader_has_no_fallback_calls():
    class NoTrades:
        pass

    exchange = NoTrades()
    result = CcxtExternalFillEvidenceReader(exchange).lookup_fills(context())

    assert result.outcome == FillEvidenceLookupOutcome.UNSUPPORTED
    assert result.fills == ()


def test_malformed_top_level_response_is_invalid():
    _, result = read({"trade": trade()})

    assert result.outcome == FillEvidenceLookupOutcome.INVALID_RESPONSE
    assert result.fills == ()


def test_timestamp_outside_explicit_window_is_invalid():
    _, result = read([trade(timestamp=END + 1, raw_timestamp=END + 1)])

    assert result.outcome == FillEvidenceLookupOutcome.INVALID_RESPONSE
    assert result.fills == ()


def test_persist_found_fills_and_event_are_atomic_and_safe(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    _, _, lookup = _lookup_for_store(store)
    result = ExternalFillEvidencePersistence.persist(store, lookup)

    assert result.attempt_recorded is True
    assert result.fill_created_flags == (True,)
    assert len(store.get_external_fills(order_id)) == 1
    event = _lookup_events(store)[0]
    assert event["event_type"] == "external_fill_lookup_found"
    payload = json.loads(event["payload"])
    assert payload["expected_external_order_id"] == OID
    assert payload["start_time_ms"] == START
    assert payload["end_time_ms"] == END
    assert payload["matched_count"] == 1
    assert payload["absence_authoritative"] is False
    assert payload["matched_fills"][0]["reported_trade_id_candidate"] == "10001"
    assert payload["matched_fills"][0]["raw_payload_hash"] == result.fills[0].raw_payload_hash


def test_atomic_failure_after_first_fill_rolls_back_all_fills_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = StateStore(tmp_path / "state.db")
    _order(store)
    response = [
        trade(tid=10001),
        trade(tid=10002, amount=0.15, price=100_100.0, timestamp=START + 2_000),
    ]
    _, _, lookup = _lookup_for_store(store, response)
    assert lookup.outcome == FillEvidenceLookupOutcome.FOUND
    assert lookup.matched_count == 2
    assert len(lookup.fills) == 2

    def fail_event(self, connection, *args, **kwargs):
        raise RuntimeError("injected fill journal failure")

    monkeypatch.setattr(StateStore, "_insert_event", fail_event)
    with pytest.raises(RuntimeError, match="injected fill journal failure"):
        ExternalFillEvidencePersistence.persist(store, lookup)

    assert store.get_external_fills(1) == []
    assert _lookup_events(store) == []


class SimulatedPowerLoss(BaseException):
    pass


def test_atomic_failure_from_base_exception_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = StateStore(tmp_path / "state.db")
    _order(store)
    _, _, lookup = _lookup_for_store(store)

    def power_loss(self, connection, *args, **kwargs):
        raise SimulatedPowerLoss("power loss")

    monkeypatch.setattr(StateStore, "_insert_event", power_loss)
    with pytest.raises(SimulatedPowerLoss, match="power loss"):
        ExternalFillEvidencePersistence.persist(store, lookup)

    assert store.get_external_fills(1) == []
    assert _lookup_events(store) == []


def test_repeated_found_deduplicates_fills_but_keeps_two_attempt_events(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    _order(store)
    _, _, lookup = _lookup_for_store(store)

    first = ExternalFillEvidencePersistence.persist(store, lookup)
    second = ExternalFillEvidencePersistence.persist(store, lookup)

    assert first.fill_created_flags == (True,)
    assert second.fill_created_flags == (False,)
    assert len(store.get_external_fills(1)) == 1
    assert len(_lookup_events(store)) == 2


def test_preexisting_duplicate_plus_new_fill_rolls_back_only_current_changes_on_event_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = StateStore(tmp_path / "state.db")
    _order(store)
    first_response = [trade(tid=10001)]
    _, _, first_lookup = _lookup_for_store(store, first_response)
    first_result = ExternalFillEvidencePersistence.persist(store, first_lookup)
    assert first_lookup.outcome == FillEvidenceLookupOutcome.FOUND
    assert first_lookup.matched_count == 1
    assert len(first_lookup.fills) == 1
    second_response = [
        trade(tid=10001),
        trade(tid=10002, amount=0.15, price=100_100.0, timestamp=START + 2_000),
    ]
    _, _, second_lookup = _lookup_for_store(store, second_response)
    assert second_lookup.outcome == FillEvidenceLookupOutcome.FOUND
    assert second_lookup.matched_count == 2
    assert len(second_lookup.fills) == 2
    assert second_lookup.fills[0].fill_key == first_result.fills[0].fill_key
    assert second_lookup.fills[1].fill_key != first_result.fills[0].fill_key

    def fail_event(self, connection, *args, **kwargs):
        raise RuntimeError("event failure")

    monkeypatch.setattr(StateStore, "_insert_event", fail_event)
    with pytest.raises(RuntimeError, match="event failure"):
        ExternalFillEvidencePersistence.persist(store, second_lookup)

    assert len(store.get_external_fills(1)) == 1
    assert len(_lookup_events(store)) == 1


def test_mid_batch_conflict_rolls_back_new_fill_and_preserves_history(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    _order(store)
    _, _, first_lookup = _lookup_for_store(store, [trade(tid=10001)])
    first_result = ExternalFillEvidencePersistence.persist(store, first_lookup)
    existing = first_result.fills[0]

    _, _, batch_lookup = _lookup_for_store(
        store,
        [
            trade(tid=10002, amount=0.15, price=100_100.0, timestamp=START + 2_000),
            trade(tid=10001),
        ],
    )
    assert batch_lookup.outcome == FillEvidenceLookupOutcome.FOUND
    assert batch_lookup.matched_count == 2
    assert len(batch_lookup.fills) == 2
    new_fill = batch_lookup.fills[0]
    conflicting = replace(batch_lookup.fills[1], quantity=0.20, fill_key=existing.fill_key)

    with pytest.raises(ExternalFillConflict):
        store.persist_external_fill_lookup_evidence(
            fills=(new_fill, conflicting),
            engine=batch_lookup.context.engine,
            aggregate_id=batch_lookup.context.intent_id,
            payload={"outcome": "FOUND", "test": "mid-batch-conflict"},
            event_type="external_fill_lookup_found",
        )

    assert store.get_external_fills(1) == [existing]
    assert len(_lookup_events(store)) == 1


@pytest.mark.parametrize(
    ("response", "error", "event_type"),
    [
        ([trade(oid=8000, raw_oid=8000)], None, "external_fill_lookup_no_match"),
        (None, ccxt.RequestTimeout("timeout"), "external_fill_lookup_transport_failure"),
        ({"bad": "shape"}, None, "external_fill_lookup_invalid"),
    ],
)
def test_non_found_outcomes_write_one_event_and_zero_fills(
    tmp_path: Path, response, error, event_type
):
    store = StateStore(tmp_path / "state.db")
    _order(store)
    _, _, lookup = _lookup_for_store(store, response, error)
    result = ExternalFillEvidencePersistence.persist(store, lookup)

    assert result.fills == ()
    assert result.fill_created_flags == ()
    assert store.get_external_fills(1) == []
    events = _lookup_events(store)
    assert len(events) == 1
    assert events[0]["event_type"] == event_type


def test_fill_conflict_rolls_back_current_invocation_and_preserves_history(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    _order(store)
    _, _, lookup = _lookup_for_store(store)
    first = ExternalFillEvidencePersistence.persist(store, lookup)
    existing = first.fills[0]
    conflicting = replace(existing, quantity=0.20, fill_key=existing.fill_key)
    conflict_lookup = FillEvidenceLookup(
        context=lookup.context,
        outcome=FillEvidenceLookupOutcome.FOUND,
        fills=(conflicting,),
        venue_fill_id_candidates=("10001",),
        response_count=1,
    )

    with pytest.raises(ExternalFillConflict):
        ExternalFillEvidencePersistence.persist(store, conflict_lookup)

    assert store.get_external_fills(1) == [existing]
    assert len(_lookup_events(store)) == 1


def test_fill_persistence_does_not_mutate_financial_tables(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    order_id = _order(store)
    orders_before = store.read_orders("trend")
    _, _, lookup = _lookup_for_store(store)
    ExternalFillEvidencePersistence.persist(store, lookup)

    assert store.read_orders("trend") == orders_before
    assert store.get_external_fills(order_id)[0].fee == pytest.approx(-1.25)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            == "10"
        )
