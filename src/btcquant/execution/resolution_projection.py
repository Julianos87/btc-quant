"""Read-only projection of durable evidence into the frozen resolution kernel."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .external_evidence import ExternalEvidenceSource, ExternalFill, ExternalOrderObservation
from .order_state import ExternalOrderState
from .resolution import (
    ExpectedOrderBinding,
    FillLookupFact,
    FillLookupOutcome,
    OrderLookupEvidence,
    OrderLookupFact,
    OrderLookupOutcome,
    ResolutionAssessment,
    ResolutionEvidenceBundle,
    assess_resolution,
)
from .state_store import PersistedLookupEvent, ResolutionSnapshot, StateStore


class ProjectionStatus(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    INVALID_PERSISTED_EVIDENCE = "INVALID_PERSISTED_EVIDENCE"


class ProjectionReasonCode(StrEnum):
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    UNSUPPORTED_ORDER_TYPE = "UNSUPPORTED_ORDER_TYPE"
    NO_LOOKUP_CONTEXT = "NO_LOOKUP_CONTEXT"
    BINDING_CONTEXT_INCOMPLETE = "BINDING_CONTEXT_INCOMPLETE"
    BINDING_CONTEXT_CONFLICT = "BINDING_CONTEXT_CONFLICT"
    MALFORMED_LOOKUP_EVENT = "MALFORMED_LOOKUP_EVENT"
    EVENT_OUTCOME_MISMATCH = "EVENT_OUTCOME_MISMATCH"
    ORDER_PERSISTENCE_LINK_MISSING = "ORDER_PERSISTENCE_LINK_MISSING"
    ORDER_PERSISTENCE_LINK_AMBIGUOUS = "ORDER_PERSISTENCE_LINK_AMBIGUOUS"
    FILL_PERSISTENCE_LINK_MISSING = "FILL_PERSISTENCE_LINK_MISSING"
    FILL_PERSISTENCE_LINK_AMBIGUOUS = "FILL_PERSISTENCE_LINK_AMBIGUOUS"


@dataclass(frozen=True)
class PersistedResolutionProjection:
    status: ProjectionStatus
    reasons: tuple[ProjectionReasonCode, ...] = ()
    bundle: ResolutionEvidenceBundle | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ProjectionStatus(self.status))
        object.__setattr__(
            self, "reasons", tuple(sorted({ProjectionReasonCode(code) for code in self.reasons}))
        )
        if self.status == ProjectionStatus.READY and self.bundle is None:
            raise ValueError("READY projection requires a ResolutionEvidenceBundle")
        if self.status != ProjectionStatus.READY and self.bundle is not None:
            raise ValueError("non-ready projection cannot carry a bundle")


@dataclass(frozen=True)
class PersistedResolutionAssessment:
    projection: PersistedResolutionProjection
    assessment: ResolutionAssessment | None

    def __post_init__(self) -> None:
        if self.projection.status == ProjectionStatus.READY:
            if self.assessment is None:
                raise ValueError("ready projection requires an assessment")
        elif self.assessment is not None:
            raise ValueError("non-ready projection cannot carry an assessment")


_ORDER_EVENT_OUTCOMES = {
    "external_order_lookup_found": OrderLookupOutcome.FOUND,
    "external_order_lookup_not_found": OrderLookupOutcome.NOT_FOUND,
    "external_order_lookup_transport_failure": OrderLookupOutcome.TRANSPORT_FAILURE,
    "external_order_lookup_unsupported": OrderLookupOutcome.UNSUPPORTED,
    "external_order_lookup_invalid": OrderLookupOutcome.INVALID_RESPONSE,
    "external_order_lookup_conflict": OrderLookupOutcome.CONFLICTING_RESPONSE,
    "external_order_lookup_incomplete": OrderLookupOutcome.INCOMPLETE_RESPONSE,
}
_FILL_EVENT_OUTCOMES = {
    "external_fill_lookup_found": FillLookupOutcome.FOUND,
    "external_fill_lookup_no_match": FillLookupOutcome.NO_MATCH,
    "external_fill_lookup_transport_failure": FillLookupOutcome.TRANSPORT_FAILURE,
    "external_fill_lookup_unsupported": FillLookupOutcome.UNSUPPORTED,
    "external_fill_lookup_invalid": FillLookupOutcome.INVALID_RESPONSE,
    "external_fill_lookup_conflict": FillLookupOutcome.CONFLICTING_RESPONSE,
    "external_fill_lookup_incomplete": FillLookupOutcome.INCOMPLETE_RESPONSE,
}
_ORDER_EVIDENCE_FIELDS = frozenset(
    {
        "returned_client_order_id",
        "external_order_id",
        "ccxt_status",
        "venue_status",
        "normalized_state",
        "requested_qty",
        "filled_qty",
        "remaining_qty",
        "requested_qty_explicit",
        "filled_qty_explicit",
        "remaining_qty_explicit",
        "source_kind",
        "venue_event_at",
        "observed_at",
        "raw_payload_hash",
        "correlation_complete",
        "quantities_complete",
        "contradictory",
    }
)
_FILL_CONTEXT_FIELDS = frozenset(
    {
        "expected_external_order_id",
        "expected_client_order_id",
        "start_time_ms",
        "end_time_ms",
        "response_count",
        "matched_count",
        "response_limit",
        "response_limit_reached",
        "retention_limit",
        "absence_authoritative",
        "matched_fills",
    }
)


def _invalid(*reasons: ProjectionReasonCode) -> PersistedResolutionProjection:
    return PersistedResolutionProjection(ProjectionStatus.INVALID_PERSISTED_EVIDENCE, reasons)


def _not_ready(*reasons: ProjectionReasonCode) -> PersistedResolutionProjection:
    return PersistedResolutionProjection(ProjectionStatus.NOT_READY, reasons)


def _text(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _required_text(value: object, field: str) -> str:
    result = _text(value, field)
    assert result is not None
    return result


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _number(value: object, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be finite numeric")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{field} must be finite{' and positive' if positive else ''}")
    return result


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _payload(event: PersistedLookupEvent) -> dict[str, Any]:
    try:
        value = json.loads(event.payload)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("event payload is invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("event payload must be an object")
    return value


def _same_float(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return abs(left - right) <= max(1e-9, max(abs(left), abs(right)) * 1e-9)


def _validate_local_event(
    event: PersistedLookupEvent,
    payload: dict[str, Any],
    order: dict[str, Any],
) -> tuple[str, str, str, str | None]:
    order_id = _integer(payload.get("local_order_id"), "local_order_id", minimum=1)
    if order_id != int(order["id"]):
        raise ValueError("event local_order_id differs from order")
    if _text(payload.get("intent_id"), "intent_id") != str(order["intent_id"]):
        raise ValueError("event intent_id differs from order")
    if _text(payload.get("engine"), "engine") != str(order["engine"]):
        raise ValueError("event engine differs from order")
    if event.engine != str(order["engine"]) or event.aggregate_id != str(order["intent_id"]):
        raise ValueError("event durable binding differs from order")
    side = _text(payload.get("side"), "side")
    assert side is not None
    if side.upper() != str(order["side"]).upper():
        raise ValueError("event side differs from order")
    venue = _text(payload.get("venue"), "venue")
    account_scope = _text(payload.get("account_scope"), "account_scope")
    instrument = _text(payload.get("instrument"), "instrument")
    assert venue is not None and account_scope is not None and instrument is not None
    expected_cloid = _text(
        payload.get("expected_client_order_id"), "expected_client_order_id", nullable=True
    )
    return venue, account_scope, instrument, expected_cloid


def _binding(
    order: dict[str, Any],
    *,
    venue: str,
    account_scope: str,
    instrument: str,
    expected_client_order_id: str,
    expected_external_order_id: str | None = None,
) -> ExpectedOrderBinding:
    return ExpectedOrderBinding(
        local_order_id=int(order["id"]),
        intent_id=str(order["intent_id"]),
        venue=venue,
        account_scope=account_scope,
        engine=str(order["engine"]),
        instrument=instrument,
        side=str(order["side"]),
        requested_qty=_number(order["requested_qty"], "requested_qty", positive=True),
        expected_client_order_id=expected_client_order_id,
        expected_external_order_id=expected_external_order_id,
    )


def _order_evidence(payload: dict[str, Any]) -> OrderLookupEvidence | None:
    present = _ORDER_EVIDENCE_FIELDS & payload.keys()
    if not present:
        return None
    if present != _ORDER_EVIDENCE_FIELDS:
        raise ValueError("order evidence is partial")
    return OrderLookupEvidence(
        local_order_id=_integer(payload["local_order_id"], "local_order_id", minimum=1),
        intent_id=_required_text(payload["intent_id"], "intent_id"),
        venue=_required_text(payload["venue"], "venue"),
        account_scope=_required_text(payload["account_scope"], "account_scope"),
        instrument=_required_text(payload["instrument"], "instrument"),
        side=_required_text(payload["side"], "side"),
        engine=_required_text(payload["engine"], "engine"),
        expected_client_order_id=_required_text(
            payload["expected_client_order_id"], "expected_client_order_id"
        ),
        returned_client_order_id=_text(
            payload["returned_client_order_id"], "returned_client_order_id", nullable=True
        ),
        external_order_id=_text(payload["external_order_id"], "external_order_id", nullable=True),
        ccxt_status=_text(payload["ccxt_status"], "ccxt_status", nullable=True),
        venue_status=_text(payload["venue_status"], "venue_status", nullable=True),
        normalized_state=ExternalOrderState(payload["normalized_state"]),
        requested_qty=(
            None
            if payload["requested_qty"] is None
            else _number(payload["requested_qty"], "requested_qty")
        ),
        filled_qty=(
            None if payload["filled_qty"] is None else _number(payload["filled_qty"], "filled_qty")
        ),
        remaining_qty=(
            None
            if payload["remaining_qty"] is None
            else _number(payload["remaining_qty"], "remaining_qty")
        ),
        requested_qty_explicit=_boolean(
            payload["requested_qty_explicit"], "requested_qty_explicit"
        ),
        filled_qty_explicit=_boolean(payload["filled_qty_explicit"], "filled_qty_explicit"),
        remaining_qty_explicit=_boolean(
            payload["remaining_qty_explicit"], "remaining_qty_explicit"
        ),
        source_kind=ExternalEvidenceSource(payload["source_kind"]),
        venue_event_at=_text(payload["venue_event_at"], "venue_event_at", nullable=True),
        observed_at=_required_text(payload["observed_at"], "observed_at"),
        raw_payload_hash=_required_text(payload["raw_payload_hash"], "raw_payload_hash"),
        correlation_complete=_boolean(payload["correlation_complete"], "correlation_complete"),
        quantities_complete=_boolean(payload["quantities_complete"], "quantities_complete"),
        contradictory=_boolean(payload["contradictory"], "contradictory"),
    )


def _matches_order_observation(
    evidence: OrderLookupEvidence, observation: ExternalOrderObservation
) -> bool:
    return (
        observation.local_order_id == evidence.local_order_id
        and observation.intent_id == evidence.intent_id
        and observation.venue == evidence.venue
        and observation.account_scope == evidence.account_scope
        and observation.instrument == evidence.instrument
        and observation.side == evidence.side
        and observation.source_kind == evidence.source_kind
        and observation.normalized_external_status == evidence.normalized_state
        and observation.raw_payload_hash == evidence.raw_payload_hash
        and observation.observed_at == evidence.observed_at
        and observation.client_order_id == evidence.returned_client_order_id
        and observation.external_order_id == evidence.external_order_id
        and _same_float(observation.requested_qty, evidence.requested_qty)
        and _same_float(observation.cumulative_filled_qty, evidence.filled_qty)
        and _same_float(observation.remaining_qty, evidence.remaining_qty)
    )


def _project_order_event(
    event: PersistedLookupEvent,
    payload: dict[str, Any],
    order: dict[str, Any],
    binding: ExpectedOrderBinding,
    observations: tuple[ExternalOrderObservation, ...],
) -> OrderLookupFact:
    outcome = _ORDER_EVENT_OUTCOMES[event.event_type]
    if payload.get("outcome") != outcome.value:
        raise RuntimeError(ProjectionReasonCode.EVENT_OUTCOME_MISMATCH)
    evidence = _order_evidence(payload)
    fact = OrderLookupFact(
        binding, outcome, evidence, _text(payload.get("reason"), "reason", nullable=True)
    )
    if outcome == OrderLookupOutcome.FOUND:
        assert evidence is not None
        matches = [obs for obs in observations if _matches_order_observation(evidence, obs)]
        if not matches:
            raise RuntimeError(ProjectionReasonCode.ORDER_PERSISTENCE_LINK_MISSING)
        if len(matches) != 1:
            raise RuntimeError(ProjectionReasonCode.ORDER_PERSISTENCE_LINK_AMBIGUOUS)
    return fact


def _fill_from_hash(
    raw_payload_hash: str,
    fills: tuple[ExternalFill, ...],
    binding: ExpectedOrderBinding,
) -> ExternalFill:
    matches = [
        fill
        for fill in fills
        if fill.raw_payload_hash == raw_payload_hash
        and fill.local_order_id == binding.local_order_id
        and fill.intent_id == binding.intent_id
        and fill.venue == binding.venue
        and fill.account_scope == binding.account_scope
        and fill.instrument == binding.instrument
        and fill.side == binding.side
    ]
    if not matches:
        raise RuntimeError(ProjectionReasonCode.FILL_PERSISTENCE_LINK_MISSING)
    if len(matches) != 1:
        raise RuntimeError(ProjectionReasonCode.FILL_PERSISTENCE_LINK_AMBIGUOUS)
    return matches[0]


def _project_fill_event(
    event: PersistedLookupEvent,
    payload: dict[str, Any],
    order: dict[str, Any],
    global_binding: ExpectedOrderBinding,
    fills: tuple[ExternalFill, ...],
) -> FillLookupFact:
    outcome = _FILL_EVENT_OUTCOMES[event.event_type]
    if payload.get("outcome") != outcome.value:
        raise RuntimeError(ProjectionReasonCode.EVENT_OUTCOME_MISMATCH)
    if not _FILL_CONTEXT_FIELDS <= payload.keys():
        raise ValueError("fill lookup context is partial")
    expected_oid = _text(payload["expected_external_order_id"], "expected_external_order_id")
    event_cloid = _text(
        payload["expected_client_order_id"], "expected_client_order_id", nullable=True
    )
    if event_cloid is not None and event_cloid != global_binding.expected_client_order_id:
        raise RuntimeError(ProjectionReasonCode.BINDING_CONTEXT_CONFLICT)
    fact_binding = _binding(
        order,
        venue=global_binding.venue,
        account_scope=global_binding.account_scope,
        instrument=global_binding.instrument,
        expected_client_order_id=event_cloid or global_binding.expected_client_order_id,
        expected_external_order_id=expected_oid,
    )
    start = _integer(payload["start_time_ms"], "start_time_ms")
    end = _integer(payload["end_time_ms"], "end_time_ms")
    if end < start:
        raise ValueError("fill lookup window is invalid")
    response_count = _integer(payload["response_count"], "response_count")
    matched_count = _integer(payload["matched_count"], "matched_count")
    matched = payload["matched_fills"]
    if not isinstance(matched, list) or matched_count != len(matched):
        raise ValueError("matched fills are inconsistent")
    if response_count < matched_count:
        raise ValueError("response_count is smaller than matched_count")
    if outcome == FillLookupOutcome.FOUND:
        if matched_count <= 0:
            raise ValueError("FOUND fill lookup requires matched fills")
    elif matched_count != 0 or matched:
        raise ValueError("non-FOUND fill lookup cannot carry matched fills")
    resolved: list[ExternalFill] = []
    candidates: list[str | None] = []
    for item in matched:
        if not isinstance(item, dict) or set(item) != {
            "raw_payload_hash",
            "reported_trade_id_candidate",
        }:
            raise ValueError("matched fill entry is malformed")
        raw_hash = _text(item["raw_payload_hash"], "raw_payload_hash")
        candidate = _text(
            item["reported_trade_id_candidate"], "reported_trade_id_candidate", nullable=True
        )
        assert raw_hash is not None
        resolved.append(_fill_from_hash(raw_hash, fills, fact_binding))
        candidates.append(candidate)
    return FillLookupFact(
        binding=fact_binding,
        outcome=outcome,
        fills=tuple(resolved),
        response_count=response_count,
        response_limit=_integer(payload["response_limit"], "response_limit"),
        response_limit_reached=_boolean(
            payload["response_limit_reached"], "response_limit_reached"
        ),
        retention_limit=_integer(payload["retention_limit"], "retention_limit"),
        absence_authoritative=_boolean(payload["absence_authoritative"], "absence_authoritative"),
        venue_fill_id_candidates=tuple(candidates),
        reason=_text(payload.get("reason"), "reason", nullable=True),
    )


def project_resolution_snapshot(snapshot: ResolutionSnapshot) -> PersistedResolutionProjection:
    """Project one coherent durable snapshot without performing I/O."""

    if snapshot.order is None:
        return _not_ready(ProjectionReasonCode.ORDER_NOT_FOUND)
    order = dict(snapshot.order)
    if str(order.get("order_type", "")).upper() != "MARKET":
        return _not_ready(ProjectionReasonCode.UNSUPPORTED_ORDER_TYPE)
    if not snapshot.lookup_events:
        return _not_ready(ProjectionReasonCode.NO_LOOKUP_CONTEXT)

    parsed: list[tuple[PersistedLookupEvent, dict[str, Any], str, str, str, str | None]] = []
    try:
        for event in snapshot.lookup_events:
            if event.event_type not in _ORDER_EVENT_OUTCOMES | _FILL_EVENT_OUTCOMES:
                raise ValueError("unknown lookup event type")
            payload = _payload(event)
            venue, account_scope, instrument, cloid = _validate_local_event(event, payload, order)
            parsed.append((event, payload, venue, account_scope, instrument, cloid))
    except ValueError:
        return _invalid(ProjectionReasonCode.MALFORMED_LOOKUP_EVENT)

    venues = {entry[2] for entry in parsed}
    account_scopes = {entry[3] for entry in parsed}
    instruments = {entry[4] for entry in parsed}
    if len(venues) != 1 or len(account_scopes) != 1 or len(instruments) != 1:
        return _invalid(ProjectionReasonCode.BINDING_CONTEXT_CONFLICT)
    cloids = {entry[5] for entry in parsed if entry[5] is not None}
    if not cloids:
        return _not_ready(ProjectionReasonCode.BINDING_CONTEXT_INCOMPLETE)
    if len(cloids) != 1:
        return _invalid(ProjectionReasonCode.BINDING_CONTEXT_CONFLICT)
    venue = next(iter(venues))
    account_scope = next(iter(account_scopes))
    instrument = next(iter(instruments))
    expected_cloid = next(iter(cloids))
    assert isinstance(venue, str) and isinstance(account_scope, str) and isinstance(instrument, str)
    assert isinstance(expected_cloid, str)
    try:
        global_binding = _binding(
            order,
            venue=venue,
            account_scope=account_scope,
            instrument=instrument,
            expected_client_order_id=expected_cloid,
        )
    except ValueError:
        return _invalid(ProjectionReasonCode.MALFORMED_LOOKUP_EVENT)

    order_facts: list[OrderLookupFact] = []
    fill_facts: list[FillLookupFact] = []
    try:
        for event, payload, *_ in parsed:
            if event.event_type in _ORDER_EVENT_OUTCOMES:
                order_facts.append(
                    _project_order_event(
                        event, payload, order, global_binding, snapshot.order_observations
                    )
                )
            else:
                fill_facts.append(
                    _project_fill_event(event, payload, order, global_binding, snapshot.fills)
                )
    except RuntimeError as error:
        reason = error.args[0] if error.args else ProjectionReasonCode.MALFORMED_LOOKUP_EVENT
        if isinstance(reason, ProjectionReasonCode):
            return _invalid(reason)
        return _invalid(ProjectionReasonCode.MALFORMED_LOOKUP_EVENT)
    except (TypeError, ValueError):
        return _invalid(ProjectionReasonCode.MALFORMED_LOOKUP_EVENT)

    return PersistedResolutionProjection(
        ProjectionStatus.READY,
        bundle=ResolutionEvidenceBundle(
            binding=global_binding,
            order_observations=snapshot.order_observations,
            fills=snapshot.fills,
            order_lookups=tuple(order_facts),
            fill_lookups=tuple(fill_facts),
        ),
    )


def assess_persisted_resolution(
    store: StateStore, local_order_id: int
) -> PersistedResolutionAssessment:
    """Read durable evidence once, project it, then invoke only the pure kernel."""

    projection = project_resolution_snapshot(store.read_resolution_snapshot(local_order_id))
    assessment = assess_resolution(projection.bundle) if projection.bundle is not None else None
    return PersistedResolutionAssessment(projection, assessment)
