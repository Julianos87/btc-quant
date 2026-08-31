"""A.3.3.2D durable-evidence projection tests."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from btcquant.execution.external_evidence import (
    ExternalEvidenceSource,
    ExternalFill,
    ExternalOrderObservation,
)
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.resolution import ResolutionOutcome
from btcquant.execution.resolution_projection import (
    ProjectionReasonCode,
    ProjectionStatus,
    assess_persisted_resolution,
    project_resolution_snapshot,
)
from btcquant.execution.state_store import StateStore

CLOID = "0x" + "a" * 32
HASH = "a" * 64
OBSERVED = "2026-08-30T12:00:00Z"
VENUE_AT = "2026-08-30T11:59:00Z"
START = 1_700_000_000_000
END = START + 10_000


def order_context(
    order_id: int, *, intent_id: str = "intent-1", **changes: object
) -> dict[str, object]:
    value: dict[str, object] = {
        "local_order_id": order_id,
        "intent_id": intent_id,
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": "SELL",
        "engine": "trend",
        "expected_client_order_id": CLOID,
    }
    value.update(changes)
    return value


def new_store(tmp_path: Path, *, order_type: str = "MARKET") -> tuple[StateStore, int]:
    store = StateStore(tmp_path / "state.db")
    order_id = store.begin_order("trend", "slot", "intent-1", order_type, "SELL", 1.0, "exit")
    return store, order_id


def observation(order_id: int, **changes: object) -> ExternalOrderObservation:
    value: dict[str, object] = {
        "local_order_id": order_id,
        "intent_id": "intent-1",
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": "SELL",
        "source_kind": ExternalEvidenceSource.ORDER_LOOKUP,
        "normalized_external_status": ExternalOrderState.OPEN,
        "requested_qty": 1.0,
        "cumulative_filled_qty": 0.0,
        "remaining_qty": 1.0,
        "client_order_id": CLOID,
        "external_order_id": "oid-A",
        "venue_event_at": VENUE_AT,
        "observed_at": OBSERVED,
        "raw_payload_hash": HASH,
    }
    value.update(changes)
    return ExternalOrderObservation(**cast(Any, value))


def fill(
    order_id: int, *, key: str = "1", raw_hash: str = "b" * 64, **changes: object
) -> ExternalFill:
    value: dict[str, object] = {
        "local_order_id": order_id,
        "intent_id": "intent-1",
        "venue": "hyperliquid",
        "account_scope": "main",
        "instrument": "BTC/USDC:USDC",
        "side": "SELL",
        "source_kind": ExternalEvidenceSource.FILL_LOOKUP,
        "client_order_id": CLOID,
        "external_order_id": "oid-A",
        "quantity": 0.25,
        "price": 100_000.0,
        "fee": -0.01,
        "fee_asset": "USDC",
        "venue_event_at": VENUE_AT,
        "observed_at": OBSERVED,
        "raw_payload_hash": raw_hash,
        "fill_key": "fill-" + key.zfill(64),
    }
    value.update(changes)
    return ExternalFill(**cast(Any, value))


def order_payload(order_id: int, outcome: str, **changes: object) -> dict[str, object]:
    value = order_context(order_id)
    value.update({"outcome": outcome, "reason": None, "retryable": False})
    value.update(changes)
    return value


def found_order_payload(order_id: int, **changes: object) -> dict[str, object]:
    value = order_payload(order_id, "FOUND")
    value.update(
        {
            "returned_client_order_id": CLOID,
            "external_order_id": "oid-A",
            "ccxt_status": "open",
            "venue_status": "open",
            "normalized_state": "OPEN",
            "requested_qty": 1.0,
            "filled_qty": 0.0,
            "remaining_qty": 1.0,
            "requested_qty_explicit": True,
            "filled_qty_explicit": True,
            "remaining_qty_explicit": True,
            "source_kind": "ORDER_LOOKUP",
            "venue_event_at": VENUE_AT,
            "observed_at": OBSERVED,
            "raw_payload_hash": HASH,
            "correlation_complete": True,
            "quantities_complete": True,
            "contradictory": False,
        }
    )
    value.update(changes)
    return value


def fill_payload(
    order_id: int,
    outcome: str,
    *,
    matched: list[dict[str, object]] | None = None,
    **changes: object,
) -> dict[str, object]:
    value = order_context(order_id)
    matched = [] if matched is None else matched
    value.update(
        {
            "outcome": outcome,
            "reason": None,
            "retryable": False,
            "expected_external_order_id": "oid-A",
            "start_time_ms": START,
            "end_time_ms": END,
            "response_count": len(matched),
            "matched_count": len(matched),
            "response_limit": 2000,
            "response_limit_reached": False,
            "retention_limit": 10_000,
            "absence_authoritative": False,
            "matched_fills": matched,
        }
    )
    value.update(changes)
    return value


def append_order(store: StateStore, payload: dict[str, object], event_type: str) -> None:
    store.append_external_order_lookup_attempt(
        engine="trend", aggregate_id="intent-1", payload=payload, event_type=event_type
    )


def append_fill(store: StateStore, payload: dict[str, object], event_type: str) -> None:
    store.persist_external_fill_lookup_evidence(
        fills=(), engine="trend", aggregate_id="intent-1", payload=payload, event_type=event_type
    )


def test_not_found_and_no_match_project_ready_unresolved(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    append_order(store, order_payload(order_id, "NOT_FOUND"), "external_order_lookup_not_found")
    append_fill(store, fill_payload(order_id, "NO_MATCH"), "external_fill_lookup_no_match")

    result = assess_persisted_resolution(store, order_id)

    assert result.projection.status == ProjectionStatus.READY
    assert result.assessment is not None
    assert result.assessment.outcome == ResolutionOutcome.UNRESOLVED


def test_found_order_requires_exact_persisted_observation_link(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    store.append_external_order_observation(observation(order_id))
    append_order(store, found_order_payload(order_id), "external_order_lookup_found")

    projection = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert projection.status == ProjectionStatus.READY
    assert projection.bundle is not None
    assert projection.bundle.order_lookups[0].outcome == "FOUND"


@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        ((), ProjectionReasonCode.ORDER_PERSISTENCE_LINK_MISSING),
        (
            (observation(1), observation(1, observation_key="obs-" + "c" * 64)),
            ProjectionReasonCode.ORDER_PERSISTENCE_LINK_AMBIGUOUS,
        ),
    ],
)
def test_found_order_link_missing_or_ambiguous_is_invalid(
    tmp_path: Path, stored: tuple[ExternalOrderObservation, ...], reason: ProjectionReasonCode
):
    store, order_id = new_store(tmp_path)
    for item in stored:
        store.append_external_order_observation(
            ExternalOrderObservation(**{**item.__dict__, "local_order_id": order_id})
        )
    append_order(store, found_order_payload(order_id), "external_order_lookup_found")

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (reason,)


def test_found_fill_links_unique_persisted_fill_and_preserves_tid_candidate(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    stored, _ = store.append_external_fill(fill(order_id))
    append_fill(
        store,
        fill_payload(
            order_id,
            "FOUND",
            matched=[
                {"raw_payload_hash": stored.raw_payload_hash, "reported_trade_id_candidate": None}
            ],
        ),
        "external_fill_lookup_found",
    )

    projection = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert projection.status == ProjectionStatus.READY
    assert projection.bundle is not None
    fact = projection.bundle.fill_lookups[0]
    assert fact.fills == (stored,)
    assert fact.venue_fill_id_candidates == (None,)
    assert stored.venue_fill_id is None


@pytest.mark.parametrize(
    ("stored_fills", "reason"),
    [
        ((), ProjectionReasonCode.FILL_PERSISTENCE_LINK_MISSING),
        (
            (
                fill(1, key="1", raw_hash="d" * 64),
                fill(1, key="2", raw_hash="d" * 64, price=100_001.0),
            ),
            ProjectionReasonCode.FILL_PERSISTENCE_LINK_AMBIGUOUS,
        ),
    ],
)
def test_found_fill_link_missing_or_ambiguous_is_invalid(
    tmp_path: Path, stored_fills: tuple[ExternalFill, ...], reason: ProjectionReasonCode
):
    store, order_id = new_store(tmp_path)
    for item in stored_fills:
        store.append_external_fill(ExternalFill(**{**item.__dict__, "local_order_id": order_id}))
    raw_hash = stored_fills[0].raw_payload_hash if stored_fills else "d" * 64
    append_fill(
        store,
        fill_payload(
            order_id,
            "FOUND",
            matched=[{"raw_payload_hash": raw_hash, "reported_trade_id_candidate": "100"}],
        ),
        "external_fill_lookup_found",
    )

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (reason,)


def test_same_fill_redelivered_across_events_stays_ready_and_kernel_deduplicates(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    stored, _ = store.append_external_fill(fill(order_id))
    payload = fill_payload(
        order_id,
        "FOUND",
        matched=[
            {"raw_payload_hash": stored.raw_payload_hash, "reported_trade_id_candidate": "100"}
        ],
    )
    append_fill(store, payload, "external_fill_lookup_found")
    append_fill(store, payload, "external_fill_lookup_found")

    result = assess_persisted_resolution(store, order_id)

    assert result.projection.status == ProjectionStatus.READY
    assert result.assessment is not None
    assert result.assessment.proven_filled_lower_bound == pytest.approx(0.25)


def test_same_tid_candidate_different_fill_keys_remains_kernel_conservative(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    one, _ = store.append_external_fill(fill(order_id, key="1", raw_hash="b" * 64))
    two, _ = store.append_external_fill(fill(order_id, key="2", raw_hash="c" * 64, price=100_001.0))
    append_fill(
        store,
        fill_payload(
            order_id,
            "FOUND",
            matched=[
                {"raw_payload_hash": one.raw_payload_hash, "reported_trade_id_candidate": "100"},
                {"raw_payload_hash": two.raw_payload_hash, "reported_trade_id_candidate": "100"},
            ],
        ),
        "external_fill_lookup_found",
    )

    result = assess_persisted_resolution(store, order_id)

    assert result.projection.status == ProjectionStatus.READY
    assert result.assessment is not None
    assert result.assessment.proven_filled_lower_bound == pytest.approx(0.25)


@pytest.mark.parametrize(
    "field",
    ["local_order_id", "intent_id", "engine"],
)
def test_local_binding_payload_mismatch_is_invalid(tmp_path: Path, field: str):
    store, order_id = new_store(tmp_path)
    payload = order_payload(order_id, "NOT_FOUND")
    payload[field] = order_id + 1 if field == "local_order_id" else "other"
    append_order(store, payload, "external_order_lookup_not_found")

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (ProjectionReasonCode.MALFORMED_LOOKUP_EVENT,)


@pytest.mark.parametrize(
    "payload_change",
    [
        {"venue": "other"},
        {"account_scope": "other"},
        {"instrument": "ETH/USDC:USDC"},
        {"expected_client_order_id": "0x" + "b" * 32},
    ],
)
def test_conflicting_global_context_is_invalid(tmp_path: Path, payload_change: dict[str, object]):
    store, order_id = new_store(tmp_path)
    append_order(store, order_payload(order_id, "NOT_FOUND"), "external_order_lookup_not_found")
    payload = fill_payload(order_id, "NO_MATCH")
    payload.update(payload_change)
    append_fill(store, payload, "external_fill_lookup_no_match")

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (ProjectionReasonCode.BINDING_CONTEXT_CONFLICT,)


def test_order_lookup_missing_cloid_is_invalid_and_fill_none_can_reuse_global(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    no_cloid = order_payload(order_id, "NOT_FOUND", expected_client_order_id=None)
    append_order(store, no_cloid, "external_order_lookup_not_found")
    projection = project_resolution_snapshot(store.read_resolution_snapshot(order_id))
    assert projection.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert projection.reasons == (ProjectionReasonCode.EVENT_PROVENANCE_MISMATCH,)

    store, order_id = new_store(tmp_path / "with-global")
    append_order(store, order_payload(order_id, "NOT_FOUND"), "external_order_lookup_not_found")
    append_fill(
        store,
        fill_payload(order_id, "NO_MATCH", expected_client_order_id=None),
        "external_fill_lookup_no_match",
    )
    projection = project_resolution_snapshot(store.read_resolution_snapshot(order_id))
    assert projection.status == ProjectionStatus.READY
    assert projection.bundle is not None
    assert projection.bundle.fill_lookups[0].binding.expected_client_order_id == CLOID


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("external_order_lookup_found", order_payload(1, "NOT_FOUND")),
        ("external_order_lookup_not_found", "not-json"),
    ],
)
def test_mismatched_or_malformed_event_is_invalid(tmp_path: Path, event_type: str, payload: object):
    store, order_id = new_store(tmp_path)
    if isinstance(payload, dict):
        payload["local_order_id"] = order_id
        append_order(store, payload, event_type)
    snapshot = store.read_resolution_snapshot(order_id)
    if not isinstance(payload, dict):
        append_order(store, order_payload(order_id, "NOT_FOUND"), event_type)
        snapshot = store.read_resolution_snapshot(order_id)
        snapshot = replace(
            snapshot,
            lookup_events=(replace(snapshot.lookup_events[0], payload=str(payload)),),
        )
    result = project_resolution_snapshot(snapshot)
    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons[0] in {
        ProjectionReasonCode.EVENT_OUTCOME_MISMATCH,
        ProjectionReasonCode.MALFORMED_LOOKUP_EVENT,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"matched_count": 2},
        {"response_count": 0},
    ],
)
def test_fill_matched_count_and_response_count_are_validated(
    tmp_path: Path, change: dict[str, object]
):
    store, order_id = new_store(tmp_path)
    stored, _ = store.append_external_fill(fill(order_id))
    payload = fill_payload(
        order_id,
        "FOUND",
        matched=[
            {"raw_payload_hash": stored.raw_payload_hash, "reported_trade_id_candidate": None}
        ],
        **change,
    )
    append_fill(store, payload, "external_fill_lookup_found")
    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))
    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (ProjectionReasonCode.MALFORMED_LOOKUP_EVENT,)


def test_snapshot_filters_only_matching_lookup_events(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    append_order(store, order_payload(order_id, "NOT_FOUND"), "external_order_lookup_not_found")
    with store._transaction() as connection:
        store._insert_event(
            connection, "trend", "checkpoint", {"state": {}}, "checkpoint", "intent-1"
        )
        store._insert_event(
            connection,
            "other-engine",
            "external_order_lookup_not_found",
            order_payload(order_id, "NOT_FOUND", engine="other-engine"),
            "external_order_lookup",
            "intent-1",
        )
        store._insert_event(
            connection,
            "trend",
            "external_order_lookup_not_found",
            order_payload(order_id, "NOT_FOUND", intent_id="other-intent"),
            "external_order_lookup",
            "other-intent",
        )
    snapshot = store.read_resolution_snapshot(order_id)
    assert [event.event_type for event in snapshot.lookup_events] == [
        "external_order_lookup_not_found"
    ]


def test_stop_order_is_not_silently_projected(tmp_path: Path):
    store, order_id = new_store(tmp_path, order_type="STOP")
    append_order(store, order_payload(order_id, "NOT_FOUND"), "external_order_lookup_not_found")
    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))
    assert result.status == ProjectionStatus.NOT_READY
    assert result.reasons == (ProjectionReasonCode.UNSUPPORTED_ORDER_TYPE,)


def test_legacy_order_fields_do_not_change_assessment(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    append_order(store, order_payload(order_id, "NOT_FOUND"), "external_order_lookup_not_found")
    append_fill(store, fill_payload(order_id, "NO_MATCH"), "external_fill_lookup_no_match")
    before = assess_persisted_resolution(store, order_id)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE orders
            SET filled_qty = 0.75, remaining_qty = 0.25, external_state = 'FILLED',
                price = 99.0, fee = 123.0, broker_order_id = 'legacy-oid'
            WHERE id = ?
            """,
            (order_id,),
        )
    after = assess_persisted_resolution(store, order_id)
    assert before.assessment == after.assessment


def test_read_only_store_produces_identical_assessment(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    append_order(store, order_payload(order_id, "NOT_FOUND"), "external_order_lookup_not_found")
    expected = assess_persisted_resolution(store, order_id)
    readonly = StateStore(store.path, read_only=True)
    assert assess_persisted_resolution(readonly, order_id) == expected


def test_single_read_snapshot_excludes_concurrent_committed_event(monkeypatch, tmp_path: Path):
    store, order_id = new_store(tmp_path)
    append_order(store, order_payload(order_id, "NOT_FOUND"), "external_order_lookup_not_found")
    writer = StateStore(store.path)
    original = store._external_order_observation_from_row
    triggered = False

    def inject(row):
        nonlocal triggered
        if not triggered:
            triggered = True
            append_fill(writer, fill_payload(order_id, "NO_MATCH"), "external_fill_lookup_no_match")
        return original(row)

    # Force the writer after the snapshot's first SELECT by supplying one observation.
    store.append_external_order_observation(observation(order_id))
    monkeypatch.setattr(store, "_external_order_observation_from_row", inject)
    first = store.read_resolution_snapshot(order_id)
    second = store.read_resolution_snapshot(order_id)
    assert [event.event_type for event in first.lookup_events] == [
        "external_order_lookup_not_found"
    ]
    assert [event.event_type for event in second.lookup_events] == [
        "external_order_lookup_not_found",
        "external_fill_lookup_no_match",
    ]


def test_bundle_keeps_all_immutable_evidence_without_generic_event_reader(
    monkeypatch, tmp_path: Path
):
    store, order_id = new_store(tmp_path)
    store.append_external_order_observation(observation(order_id))
    stored_fill, _ = store.append_external_fill(fill(order_id))
    append_order(store, order_payload(order_id, "NOT_FOUND"), "external_order_lookup_not_found")

    def forbidden(*args, **kwargs):
        raise AssertionError("projection must not call read_events")

    monkeypatch.setattr(store, "read_events", forbidden)
    result = assess_persisted_resolution(store, order_id)

    assert result.projection.status == ProjectionStatus.READY
    assert result.projection.bundle is not None
    assert len(result.projection.bundle.order_observations) == 1
    assert (
        result.projection.bundle.order_observations[0].observation_key
        == observation(order_id).observation_key
    )
    assert result.projection.bundle.fills == (stored_fill,)


def test_multiple_fill_lookup_oids_remain_for_kernel_conflict_detection(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    append_order(store, order_payload(order_id, "NOT_FOUND"), "external_order_lookup_not_found")
    append_fill(store, fill_payload(order_id, "NO_MATCH"), "external_fill_lookup_no_match")
    payload = fill_payload(order_id, "NO_MATCH", expected_external_order_id="oid-B")
    append_fill(store, payload, "external_fill_lookup_no_match")

    result = assess_persisted_resolution(store, order_id)

    assert result.projection.status == ProjectionStatus.READY
    assert result.projection.bundle is not None
    assert result.projection.bundle.binding.expected_external_order_id is None
    assert result.assessment is not None
    assert result.assessment.outcome == ResolutionOutcome.BINDING_CONFLICT


def test_partial_order_evidence_is_invalid_instead_of_defaulted(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    payload = order_payload(order_id, "INCOMPLETE_RESPONSE", normalized_state="UNKNOWN")
    append_order(store, payload, "external_order_lookup_incomplete")

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (ProjectionReasonCode.MALFORMED_LOOKUP_EVENT,)


@pytest.mark.parametrize(
    ("event_type", "aggregate_type", "payload"),
    [
        (
            "external_order_lookup_not_found",
            "external_fill_lookup",
            lambda order_id: order_payload(order_id, "NOT_FOUND"),
        ),
        (
            "external_fill_lookup_no_match",
            "external_order_lookup",
            lambda order_id: fill_payload(order_id, "NO_MATCH"),
        ),
    ],
)
def test_lookup_event_family_must_match_aggregate_type(
    tmp_path: Path, event_type: str, aggregate_type: str, payload
):
    store, order_id = new_store(tmp_path)
    event_payload = payload(order_id)
    with store._transaction() as connection:
        store._insert_event(
            connection, "trend", event_type, event_payload, aggregate_type, "intent-1"
        )

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (ProjectionReasonCode.EVENT_PROVENANCE_MISMATCH,)


def test_order_lookup_cannot_borrow_cloid_from_fill_lookup(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    append_order(
        store,
        order_payload(order_id, "NOT_FOUND", expected_client_order_id=None),
        "external_order_lookup_not_found",
    )
    append_fill(store, fill_payload(order_id, "NO_MATCH"), "external_fill_lookup_no_match")

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (ProjectionReasonCode.EVENT_PROVENANCE_MISMATCH,)


def test_found_order_requires_matching_venue_event_at(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    store.append_external_order_observation(
        observation(order_id, venue_event_at="2026-08-30T11:58:00Z")
    )
    append_order(store, found_order_payload(order_id), "external_order_lookup_found")

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (ProjectionReasonCode.ORDER_PERSISTENCE_LINK_MISSING,)


def test_found_order_requires_order_lookup_source_provenance(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    store.append_external_order_observation(
        observation(order_id, source_kind=ExternalEvidenceSource.PRIVATE_EVENT)
    )
    append_order(
        store,
        found_order_payload(order_id, source_kind="PRIVATE_EVENT"),
        "external_order_lookup_found",
    )

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (ProjectionReasonCode.EVENT_PROVENANCE_MISMATCH,)


def test_found_fill_requires_event_expected_oid(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    stored, _ = store.append_external_fill(fill(order_id, external_order_id="oid-B"))
    append_fill(
        store,
        fill_payload(
            order_id,
            "FOUND",
            matched=[
                {"raw_payload_hash": stored.raw_payload_hash, "reported_trade_id_candidate": None}
            ],
        ),
        "external_fill_lookup_found",
    )

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (ProjectionReasonCode.FILL_PERSISTENCE_LINK_MISSING,)


def test_found_fill_requires_fill_lookup_source_provenance(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    stored, _ = store.append_external_fill(
        fill(order_id, source_kind=ExternalEvidenceSource.PRIVATE_EVENT)
    )
    append_fill(
        store,
        fill_payload(
            order_id,
            "FOUND",
            matched=[
                {"raw_payload_hash": stored.raw_payload_hash, "reported_trade_id_candidate": None}
            ],
        ),
        "external_fill_lookup_found",
    )

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
    assert result.reasons == (ProjectionReasonCode.FILL_PERSISTENCE_LINK_MISSING,)


def test_private_event_stays_in_bundle_without_satisfying_fill_lookup_link(tmp_path: Path):
    store, order_id = new_store(tmp_path)
    store.append_external_fill(
        fill(
            order_id,
            key="1",
            raw_hash="c" * 64,
            source_kind=ExternalEvidenceSource.PRIVATE_EVENT,
        )
    )
    linked, _ = store.append_external_fill(fill(order_id, key="2", raw_hash="d" * 64))
    append_fill(
        store,
        fill_payload(
            order_id,
            "FOUND",
            matched=[
                {"raw_payload_hash": linked.raw_payload_hash, "reported_trade_id_candidate": None}
            ],
        ),
        "external_fill_lookup_found",
    )

    result = project_resolution_snapshot(store.read_resolution_snapshot(order_id))

    assert result.status == ProjectionStatus.READY
    assert result.bundle is not None
    assert len(result.bundle.fills) == 2
    assert result.bundle.fill_lookups[0].fills == (linked,)
