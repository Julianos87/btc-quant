"""Contrat A.3.3.1: lecture externe sans résolution ni effet de trading."""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import ccxt
import pytest

from btcquant.execution.external_evidence_reader import (
    CcxtExternalEvidenceReader,
    EvidenceLookupOutcome,
    ExternalEvidencePersistence,
    ExternalEvidenceReader,
    OrderLookupContext,
)
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore


CLOID = "0x" + "a" * 32
OBSERVED = "2026-08-29T12:00:00Z"


def context(
    *,
    side: str = "BUY",
    local_order_id: int = 1,
    intent_id: str = "intent-reader",
) -> OrderLookupContext:
    return OrderLookupContext(
        local_order_id=local_order_id,
        intent_id=intent_id,
        venue="hyperliquid",
        account_scope="main",
        instrument="BTC/USDC:USDC",
        side=side,
        expected_client_order_id=CLOID,
        engine="trend",
    )


def order(**changes):
    value = {
        "id": "oid-1",
        "clientOrderId": CLOID,
        "status": "open",
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "timestamp": 1788004800000,
    }
    value.update(changes)
    return value


class FakeExchange:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def fetch_order(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.response


def read(response=None, error=None):
    exchange = FakeExchange(response if response is not None else order(), error)
    result = CcxtExternalEvidenceReader(exchange).lookup_order(context(), observed_at=OBSERVED)
    return exchange, result


def test_interface_is_read_only_and_does_not_depend_on_state_store():
    assert "lookup_order" in dir(ExternalEvidenceReader)
    methods = " ".join(dir(CcxtExternalEvidenceReader))
    assert "submit" not in methods
    assert "cancel" not in methods
    assert "store" not in inspect.signature(CcxtExternalEvidenceReader.__init__).parameters


def test_exact_cloid_is_used_and_one_call_is_made():
    exchange, result = read()
    assert result.outcome == EvidenceLookupOutcome.FOUND
    assert len(exchange.calls) == 1
    assert exchange.calls[0] == (CLOID, "BTC/USDC:USDC", {"clientOrderId": CLOID})


def test_matching_returned_cloid_is_correlated():
    _, result = read()
    assert result.evidence is not None
    assert result.evidence.returned_client_order_id == CLOID
    assert result.evidence.correlation_complete is True


def test_missing_returned_cloid_is_incomplete_and_not_fabricated():
    _, result = read({"status": "open", "amount": 1.0, "filled": 0.0, "remaining": 1.0})
    assert result.outcome == EvidenceLookupOutcome.INCOMPLETE_RESPONSE
    assert result.evidence is not None
    assert result.evidence.returned_client_order_id is None
    assert result.evidence.correlation_complete is False


def test_mismatched_returned_cloid_is_conflicting():
    _, result = read(order(clientOrderId="0x" + "b" * 32))
    assert result.outcome == EvidenceLookupOutcome.CONFLICTING_RESPONSE


def test_order_not_found_is_explicit():
    _, result = read(error=ccxt.OrderNotFound("gone"))
    assert result.outcome == EvidenceLookupOutcome.NOT_FOUND
    assert result.evidence is None


def test_timeout_is_transport_failure_without_retry():
    exchange, result = read(error=ccxt.RequestTimeout("timeout"))
    assert result.outcome == EvidenceLookupOutcome.TRANSPORT_FAILURE
    assert result.retryable is True
    assert len(exchange.calls) == 1


def test_unsupported_reader_is_explicit():
    class NoFetch:
        pass

    result = CcxtExternalEvidenceReader(NoFetch()).lookup_order(context())
    assert result.outcome == EvidenceLookupOutcome.UNSUPPORTED


def test_unknown_ccxt_status_is_preserved_and_normalized_unknown():
    _, result = read(order(status="futureCcxtStatus"))
    assert result.outcome == EvidenceLookupOutcome.FOUND
    assert result.evidence is not None
    assert result.evidence.ccxt_status == "futureCcxtStatus"
    assert result.evidence.venue_status is None
    assert result.evidence.normalized_state == ExternalOrderState.UNKNOWN


def test_canceled_ccxt_status_is_preserved_without_venue_status():
    _, result = read(order(status="canceled", filled=0.0, remaining=0.0))
    assert result.outcome == EvidenceLookupOutcome.FOUND
    assert result.evidence is not None
    assert result.evidence.ccxt_status == "canceled"
    assert result.evidence.venue_status is None
    assert result.evidence.normalized_state == ExternalOrderState.CANCELED


def test_margin_canceled_preserves_ccxt_and_venue_status_separately():
    _, result = read(order(status="canceled", info={"status": "marginCanceled"}))
    assert result.outcome == EvidenceLookupOutcome.FOUND
    assert result.evidence is not None
    assert result.evidence.ccxt_status == "canceled"
    assert result.evidence.venue_status == "marginCanceled"
    assert result.evidence.normalized_state == ExternalOrderState.CANCELED


def test_unknown_venue_status_is_preserved_without_interpretation():
    _, result = read(order(status="open", info={"status": "futureVenueStatus"}))
    assert result.outcome == EvidenceLookupOutcome.FOUND
    assert result.evidence is not None
    assert result.evidence.ccxt_status == "open"
    assert result.evidence.venue_status == "futureVenueStatus"
    assert result.evidence.normalized_state == ExternalOrderState.OPEN


def test_missing_venue_status_is_not_fabricated():
    _, result = read(order(status="open"))
    assert result.outcome == EvidenceLookupOutcome.FOUND
    assert result.evidence is not None
    assert result.evidence.ccxt_status == "open"
    assert result.evidence.venue_status is None


def test_missing_ccxt_status_keeps_venue_status_and_is_unknown():
    _, result = read(order(status=None, info={"status": "open"}))
    assert result.outcome == EvidenceLookupOutcome.INCOMPLETE_RESPONSE
    assert result.evidence is not None
    assert result.evidence.ccxt_status is None
    assert result.evidence.venue_status == "open"
    assert result.evidence.normalized_state == ExternalOrderState.UNKNOWN


@pytest.mark.parametrize("ccxt_status", ["open", "new", "pending"])
def test_active_explicit_zero_remaining_is_unknown_and_preserved(ccxt_status):
    _, result = read(order(status=ccxt_status, filled=0.0, remaining=0.0))
    assert result.evidence is not None
    assert result.evidence.normalized_state == ExternalOrderState.UNKNOWN
    assert result.evidence.remaining_qty == 0.0
    assert result.evidence.remaining_qty_explicit is True
    assert result.evidence.contradictory is True


def test_explicit_quantity_flags_are_preserved():
    _, result = read(order(amount=2.0, filled=0.25, remaining=1.75))
    assert result.evidence is not None
    assert result.evidence.requested_qty == 2.0
    assert result.evidence.requested_qty_explicit is True
    assert result.evidence.filled_qty_explicit is True
    assert result.evidence.remaining_qty_explicit is True


def test_absent_requested_quantity_is_not_fabricated():
    _, result = read(order(amount=None))
    assert result.outcome == EvidenceLookupOutcome.INCOMPLETE_RESPONSE
    assert result.evidence is not None
    assert result.evidence.requested_qty is None
    assert result.evidence.requested_qty_explicit is False


def test_explicit_zero_filled_and_remaining_stay_explicit():
    _, result = read(order(filled=0.0, remaining=1.0))
    assert result.evidence is not None
    assert result.evidence.filled_qty == 0.0
    assert result.evidence.filled_qty_explicit is True
    assert result.evidence.remaining_qty == 1.0
    assert result.evidence.remaining_qty_explicit is True


def test_absent_remaining_is_derived_but_marked_non_explicit():
    _, result = read(order(amount=1.0, filled=0.25, remaining=None))
    assert result.outcome == EvidenceLookupOutcome.FOUND
    assert result.evidence is not None
    assert result.evidence.remaining_qty == pytest.approx(0.75)
    assert result.evidence.remaining_qty_explicit is False


def test_contradictory_explicit_quantities_fail_closed_without_repair():
    _, result = read(order(amount=1.0, filled=0.25, remaining=0.40))
    assert result.outcome == EvidenceLookupOutcome.INVALID_RESPONSE
    assert result.evidence is not None
    assert result.evidence.contradictory is True
    assert result.evidence.remaining_qty == pytest.approx(0.40)
    assert result.evidence.normalized_state == ExternalOrderState.UNKNOWN


def test_raw_payload_hash_is_deterministic_for_mapping_order():
    first = order()
    second = {key: first[key] for key in reversed(tuple(first))}
    _, first_result = read(first)
    _, second_result = read(second)
    assert first_result.evidence is not None
    assert second_result.evidence is not None
    assert first_result.evidence.raw_payload_hash == second_result.evidence.raw_payload_hash


def test_venue_status_changes_the_evidence_hash():
    _, first_result = read(order(status="canceled", info={"status": "marginCanceled"}))
    _, second_result = read(order(status="canceled", info={"status": "canceled"}))
    assert first_result.evidence is not None
    assert second_result.evidence is not None
    assert first_result.evidence.raw_payload_hash != second_result.evidence.raw_payload_hash


def test_reader_has_no_state_store_write_path():
    reader = CcxtExternalEvidenceReader(FakeExchange(order()))
    assert not hasattr(reader, "store")
    assert not any(name.startswith("append") for name in dir(reader))


def _valid_lookup(order_id: int, intent_id: str):
    exchange = FakeExchange(order())
    return CcxtExternalEvidenceReader(exchange).lookup_order(
        context(local_order_id=order_id, intent_id=intent_id), observed_at=OBSERVED
    )


def test_valid_found_persists_one_normalized_observation_and_attempt(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    order_id = store.begin_order("trend", "slot", "intent-reader", "MARKET", "BUY", 1.0, "entry")
    result = CcxtExternalEvidenceReader(
        FakeExchange(order(info={"status": "marginCanceled"}))
    ).lookup_order(context(local_order_id=order_id), observed_at=OBSERVED)
    persisted = ExternalEvidencePersistence.persist(store, result)
    assert persisted.observation_created is True
    assert len(store.get_external_order_observations(order_id)) == 1
    event = store.read_events("trend")[-1]
    assert event["event_type"] == "external_order_lookup_found"
    payload = json.loads(event["payload"])
    assert payload["ccxt_status"] == "open"
    assert payload["venue_status"] == "marginCanceled"


def test_repeated_equivalent_found_is_idempotent_for_observation(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    order_id = store.begin_order("trend", "slot", "intent-reader", "MARKET", "BUY", 1.0, "entry")
    result = _valid_lookup(order_id, "intent-reader")
    first = ExternalEvidencePersistence.persist(store, result)
    second = ExternalEvidencePersistence.persist(store, result)
    assert first.observation_created is True
    assert second.observation_created is False
    assert len(store.get_external_order_observations(order_id)) == 1


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ccxt.OrderNotFound("gone"), EvidenceLookupOutcome.NOT_FOUND),
        (ccxt.RequestTimeout("timeout"), EvidenceLookupOutcome.TRANSPORT_FAILURE),
    ],
)
def test_non_found_outcomes_record_attempt_only(tmp_path: Path, error, outcome):
    store = StateStore(tmp_path / "state.db")
    order_id = store.begin_order("trend", "slot", "intent-reader", "MARKET", "BUY", 1.0, "entry")
    lookup = CcxtExternalEvidenceReader(FakeExchange(error=error)).lookup_order(
        context(local_order_id=order_id), observed_at=OBSERVED
    )
    assert lookup.outcome == outcome
    persisted = ExternalEvidencePersistence.persist(store, lookup)
    assert persisted.observation is None
    assert len(store.get_external_order_observations(order_id)) == 0
    assert (
        store.read_events("trend")[-1]["event_type"]
        == f"external_order_lookup_{outcome.value.lower()}"
    )


def test_cloid_mismatch_records_attempt_only(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    order_id = store.begin_order("trend", "slot", "intent-reader", "MARKET", "BUY", 1.0, "entry")
    lookup = CcxtExternalEvidenceReader(
        FakeExchange(order(clientOrderId="0x" + "b" * 32, info={"status": "marginCanceled"}))
    ).lookup_order(context(local_order_id=order_id), observed_at=OBSERVED)
    ExternalEvidencePersistence.persist(store, lookup)
    assert len(store.get_external_order_observations(order_id)) == 0
    event = store.read_events("trend")[-1]
    assert event["event_type"] == "external_order_lookup_conflict"
    payload = json.loads(event["payload"])
    assert payload["ccxt_status"] == "open"
    assert payload["venue_status"] == "marginCanceled"


def test_incomplete_response_is_not_a_trusted_observation(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    order_id = store.begin_order("trend", "slot", "intent-reader", "MARKET", "BUY", 1.0, "entry")
    lookup = CcxtExternalEvidenceReader(FakeExchange(order(clientOrderId=None))).lookup_order(
        context(local_order_id=order_id), observed_at=OBSERVED
    )
    ExternalEvidencePersistence.persist(store, lookup)
    assert len(store.get_external_order_observations(order_id)) == 0


def test_persistence_does_not_mutate_orders_positions_or_fills(tmp_path: Path):
    store = StateStore(tmp_path / "state.db")
    order_id = store.begin_order("trend", "slot", "intent-reader", "MARKET", "BUY", 1.0, "entry")
    before = store.read_orders("trend")
    ExternalEvidencePersistence.persist(store, _valid_lookup(order_id, "intent-reader"))
    assert store.read_orders("trend") == before
    assert store.get_external_fills(order_id) == []
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM positions WHERE engine = 'trend'").fetchone()[
                0
            ]
            == 0
        )
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
