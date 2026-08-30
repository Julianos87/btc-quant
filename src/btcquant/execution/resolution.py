"""Pure, deterministic assessment of immutable external-order evidence.

This module is deliberately below acquisition and persistence.  It accepts
already-normalized facts, performs no I/O, and returns an assessment only.  It
does not authorize retries or apply financial state.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from .external_evidence import ExternalEvidenceSource, ExternalFill, ExternalOrderObservation
from .order_state import ExternalOrderState


class OrderLookupOutcome(StrEnum):
    """Closed outcomes produced by the order acquisition boundary."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CONFLICTING_RESPONSE = "CONFLICTING_RESPONSE"
    INCOMPLETE_RESPONSE = "INCOMPLETE_RESPONSE"


class FillLookupOutcome(StrEnum):
    """Closed outcomes produced by the individual-fill boundary."""

    FOUND = "FOUND"
    NO_MATCH = "NO_MATCH"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CONFLICTING_RESPONSE = "CONFLICTING_RESPONSE"
    INCOMPLETE_RESPONSE = "INCOMPLETE_RESPONSE"


class ResolutionOutcome(StrEnum):
    """Closed v1 decision-kernel outcomes."""

    UNRESOLVED = "UNRESOLVED"
    EXTERNAL_ACTIVE = "EXTERNAL_ACTIVE"
    EFFECT_PROVEN_INCOMPLETE = "EFFECT_PROVEN_INCOMPLETE"
    TERMINAL_EFFECT_PROVEN = "TERMINAL_EFFECT_PROVEN"
    ZERO_EFFECT_PROVEN = "ZERO_EFFECT_PROVEN"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    BINDING_INCOMPLETE = "BINDING_INCOMPLETE"
    BINDING_CONFLICT = "BINDING_CONFLICT"


class ResolutionReasonCode(StrEnum):
    """Closed reason vocabulary for machine-readable assessments."""

    ACTIVE_ORDER_OBSERVED = "ACTIVE_ORDER_OBSERVED"
    BINDING_CONFLICT = "BINDING_CONFLICT"
    BINDING_INCOMPLETE = "BINDING_INCOMPLETE"
    FILL_COMPLETENESS_UNPROVEN = "FILL_COMPLETENESS_UNPROVEN"
    FILL_LOOKUP_INCOMPLETE = "FILL_LOOKUP_INCOMPLETE"
    FILL_LOOKUP_NO_MATCH = "FILL_LOOKUP_NO_MATCH"
    FILL_LOOKUP_UNAVAILABLE = "FILL_LOOKUP_UNAVAILABLE"
    FILL_IDENTITY_AMBIGUITY = "FILL_IDENTITY_AMBIGUITY"
    FILL_POSITIVE_EVIDENCE = "FILL_POSITIVE_EVIDENCE"
    FILL_QUANTITY_CONFLICT = "FILL_QUANTITY_CONFLICT"
    NO_ORDER_EVIDENCE = "NO_ORDER_EVIDENCE"
    ORDER_FILL_TIMING_UNPROVEN = "ORDER_FILL_TIMING_UNPROVEN"
    ORDER_LOOKUP_INCOMPLETE = "ORDER_LOOKUP_INCOMPLETE"
    ORDER_LOOKUP_NOT_FOUND = "ORDER_LOOKUP_NOT_FOUND"
    ORDER_LOOKUP_UNAVAILABLE = "ORDER_LOOKUP_UNAVAILABLE"
    ORDER_OBSERVATION_IDENTITY_CONFLICT = "ORDER_OBSERVATION_IDENTITY_CONFLICT"
    ORDER_QUANTITY_CONFLICT = "ORDER_QUANTITY_CONFLICT"
    STATUS_CONFLICT = "STATUS_CONFLICT"
    TERMINAL_ORDER_OBSERVED = "TERMINAL_ORDER_OBSERVED"
    TID_CANDIDATE_NOT_IDENTITY = "TID_CANDIDATE_NOT_IDENTITY"
    ZERO_EFFECT_UNPROVEN = "ZERO_EFFECT_UNPROVEN"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _finite(value: object, field_name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be finite numeric")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite numeric")
    if positive and normalized <= 0:
        raise ValueError(f"{field_name} must be strictly positive")
    if not positive and normalized < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return normalized


def _finite_optional(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be finite numeric")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite numeric")
    return normalized


def _same_quantity(left: float, right: float) -> bool:
    tolerance = max(1e-9, max(abs(left), abs(right)) * 1e-9)
    return abs(left - right) <= tolerance


def _canonical_acquisition_timestamp(value: object, field_name: str) -> str:
    text = _required_text(value, field_name)
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError(f"{field_name} must be ISO 8601 with an explicit timezone") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include an explicit timezone")
    return parsed.astimezone(UTC).isoformat()


def _timestamp_key(value: str | None) -> tuple[int, str]:
    if value is None:
        return (0, "")
    parsed = datetime.fromisoformat(value).astimezone(UTC)
    return (1, parsed.isoformat())


@dataclass(frozen=True)
class ExpectedOrderBinding:
    """Local facts against which every external fact must be checked."""

    local_order_id: int
    intent_id: str
    venue: str
    account_scope: str
    engine: str
    instrument: str
    side: str
    requested_qty: float
    expected_client_order_id: str
    expected_external_order_id: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        for field_name in (
            "intent_id",
            "venue",
            "account_scope",
            "engine",
            "instrument",
            "expected_client_order_id",
        ):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        object.__setattr__(self, "side", _required_text(self.side, "side").upper())
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        object.__setattr__(
            self,
            "requested_qty",
            _finite(self.requested_qty, "requested_qty", positive=True),
        )
        object.__setattr__(
            self,
            "expected_external_order_id",
            _optional_text(self.expected_external_order_id, "expected_external_order_id"),
        )

    @property
    def quantity_tolerance(self) -> float:
        return max(1e-9, self.requested_qty * 1e-9)


@dataclass(frozen=True)
class OrderLookupEvidence:
    """Pure representation of one already-acquired order lookup response."""

    local_order_id: int
    intent_id: str
    venue: str
    account_scope: str
    instrument: str
    side: str
    engine: str
    expected_client_order_id: str
    returned_client_order_id: str | None
    external_order_id: str | None
    ccxt_status: str | None
    venue_status: str | None
    normalized_state: ExternalOrderState
    requested_qty: float | None
    filled_qty: float | None
    remaining_qty: float | None
    requested_qty_explicit: bool
    filled_qty_explicit: bool
    remaining_qty_explicit: bool
    source_kind: ExternalEvidenceSource | str
    venue_event_at: str | None
    observed_at: str
    raw_payload_hash: str
    correlation_complete: bool
    quantities_complete: bool
    contradictory: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        for field_name in (
            "intent_id",
            "venue",
            "account_scope",
            "instrument",
            "engine",
            "expected_client_order_id",
            "raw_payload_hash",
        ):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        object.__setattr__(
            self,
            "observed_at",
            _canonical_acquisition_timestamp(self.observed_at, "observed_at"),
        )
        object.__setattr__(self, "side", _required_text(self.side, "side").upper())
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        for field_name in (
            "returned_client_order_id",
            "external_order_id",
            "ccxt_status",
            "venue_status",
        ):
            object.__setattr__(
                self, field_name, _optional_text(getattr(self, field_name), field_name)
            )
        try:
            object.__setattr__(self, "normalized_state", ExternalOrderState(self.normalized_state))
            object.__setattr__(self, "source_kind", ExternalEvidenceSource(self.source_kind))
        except ValueError as error:
            raise ValueError("invalid normalized state or source kind") from error
        for field_name in ("requested_qty", "filled_qty", "remaining_qty"):
            object.__setattr__(
                self, field_name, _finite_optional(getattr(self, field_name), field_name)
            )
        for field_name in (
            "requested_qty_explicit",
            "filled_qty_explicit",
            "remaining_qty_explicit",
            "correlation_complete",
            "quantities_complete",
            "contradictory",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be boolean")
        for value, explicit, field_name in (
            (self.requested_qty, self.requested_qty_explicit, "requested_qty"),
            (self.filled_qty, self.filled_qty_explicit, "filled_qty"),
            (self.remaining_qty, self.remaining_qty_explicit, "remaining_qty"),
        ):
            if explicit and value is None:
                raise ValueError(f"{field_name}_explicit cannot be true without a value")


@dataclass(frozen=True)
class OrderLookupFact:
    """One typed result from order acquisition; no reader is called here."""

    binding: ExpectedOrderBinding
    outcome: OrderLookupOutcome | str
    evidence: OrderLookupEvidence | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ExpectedOrderBinding):
            raise ValueError("binding must be ExpectedOrderBinding")
        try:
            outcome = OrderLookupOutcome(self.outcome)
        except ValueError as error:
            raise ValueError("invalid order lookup outcome") from error
        object.__setattr__(self, "outcome", outcome)
        if self.evidence is not None and not isinstance(self.evidence, OrderLookupEvidence):
            raise ValueError("evidence must be OrderLookupEvidence or None")
        if outcome == OrderLookupOutcome.FOUND and self.evidence is None:
            raise ValueError("FOUND order lookup requires evidence")
        if (
            outcome
            in {
                OrderLookupOutcome.NOT_FOUND,
                OrderLookupOutcome.TRANSPORT_FAILURE,
                OrderLookupOutcome.UNSUPPORTED,
            }
            and self.evidence is not None
        ):
            raise ValueError(f"{outcome.value} order lookup cannot carry evidence")
        object.__setattr__(self, "reason", _optional_text(self.reason, "reason"))


@dataclass(frozen=True)
class FillLookupFact:
    """One typed result from bounded individual-fill acquisition."""

    binding: ExpectedOrderBinding
    outcome: FillLookupOutcome | str
    fills: tuple[ExternalFill, ...] = ()
    response_count: int = 0
    response_limit: int = 2000
    response_limit_reached: bool = False
    retention_limit: int = 10_000
    absence_authoritative: bool = False
    venue_fill_id_candidates: tuple[str | None, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ExpectedOrderBinding):
            raise ValueError("binding must be ExpectedOrderBinding")
        try:
            object.__setattr__(self, "outcome", FillLookupOutcome(self.outcome))
        except ValueError as error:
            raise ValueError("invalid fill lookup outcome") from error
        fills = tuple(self.fills)
        candidates = tuple(self.venue_fill_id_candidates)
        object.__setattr__(self, "fills", fills)
        if any(not isinstance(fill, ExternalFill) for fill in self.fills):
            raise ValueError("fills must contain ExternalFill values")
        if len(fills) != len(candidates):
            raise ValueError("venue_fill_id_candidates must align one-to-one with fills")
        object.__setattr__(
            self,
            "venue_fill_id_candidates",
            tuple(_optional_text(value, "venue_fill_id_candidate") for value in candidates),
        )
        if (
            isinstance(self.response_count, bool)
            or not isinstance(self.response_count, int)
            or self.response_count < 0
        ):
            raise ValueError("response_count must be a non-negative integer")
        if self.response_limit != 2_000:
            raise ValueError("response_limit must remain fixed at 2000")
        if self.retention_limit != 10_000:
            raise ValueError("retention_limit must remain fixed at 10000")
        for field_name in ("response_limit_reached", "absence_authoritative"):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be boolean")
        if self.absence_authoritative:
            raise ValueError("absence_authoritative must remain false")
        if self.response_limit_reached != (self.response_count >= self.response_limit):
            raise ValueError("response_limit_reached must reflect response_count")
        if self.outcome == FillLookupOutcome.FOUND and not fills:
            raise ValueError("FOUND fill lookup requires at least one fill")
        if self.outcome == FillLookupOutcome.FOUND and self.response_count < len(fills):
            raise ValueError("response_count must cover every returned fill")
        if self.outcome != FillLookupOutcome.FOUND and fills:
            raise ValueError("only FOUND fill lookups may carry fills")
        object.__setattr__(self, "reason", _optional_text(self.reason, "reason"))


@dataclass(frozen=True)
class ResolutionEvidenceBundle:
    """Immutable input to the decision kernel."""

    binding: ExpectedOrderBinding
    order_observations: tuple[ExternalOrderObservation, ...] = ()
    fills: tuple[ExternalFill, ...] = ()
    order_lookups: tuple[OrderLookupFact, ...] = ()
    fill_lookups: tuple[FillLookupFact, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ExpectedOrderBinding):
            raise ValueError("binding must be ExpectedOrderBinding")
        for field_name, expected_type in (
            ("order_observations", ExternalOrderObservation),
            ("fills", ExternalFill),
            ("order_lookups", OrderLookupFact),
            ("fill_lookups", FillLookupFact),
        ):
            values = tuple(getattr(self, field_name))
            if any(not isinstance(value, expected_type) for value in values):
                raise ValueError(f"{field_name} contains an invalid value")
            object.__setattr__(self, field_name, values)


@dataclass(frozen=True)
class ResolutionAssessment:
    """Pure result of assessing one immutable evidence bundle."""

    outcome: ResolutionOutcome
    proven_filled_lower_bound: float
    requested_qty: float
    external_order_active: bool
    terminal_state_observed: bool
    terminal_states_observed: tuple[ExternalOrderState, ...]
    fill_completeness_proven: bool
    zero_effect_proven: bool
    binding_complete: bool
    conflicts: tuple[ResolutionReasonCode, ...] = ()
    reasons: tuple[ResolutionReasonCode, ...] = ()
    deduplicated_fill_keys: tuple[str, ...] = ()
    tid_identity_proven: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", ResolutionOutcome(self.outcome))
        object.__setattr__(
            self,
            "proven_filled_lower_bound",
            _finite(self.proven_filled_lower_bound, "proven_filled_lower_bound"),
        )
        object.__setattr__(
            self, "requested_qty", _finite(self.requested_qty, "requested_qty", positive=True)
        )
        for field_name in (
            "external_order_active",
            "terminal_state_observed",
            "fill_completeness_proven",
            "zero_effect_proven",
            "binding_complete",
            "tid_identity_proven",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be boolean")
        object.__setattr__(
            self,
            "terminal_states_observed",
            tuple(ExternalOrderState(state) for state in self.terminal_states_observed),
        )
        object.__setattr__(
            self, "conflicts", tuple(ResolutionReasonCode(code) for code in self.conflicts)
        )
        object.__setattr__(
            self, "reasons", tuple(ResolutionReasonCode(code) for code in self.reasons)
        )
        object.__setattr__(self, "deduplicated_fill_keys", tuple(self.deduplicated_fill_keys))


@dataclass
class _FillCorrelationFacts:
    """Delivery-independent optional correlation facts for one fill key."""

    known_client_order_ids: set[str] = field(default_factory=set)
    known_external_order_ids: set[str] = field(default_factory=set)
    known_venue_event_ats: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _AssessmentState:
    binding_conflicts: set[ResolutionReasonCode] = field(default_factory=set)
    binding_incomplete: set[ResolutionReasonCode] = field(default_factory=set)
    evidence_conflicts: set[ResolutionReasonCode] = field(default_factory=set)
    reasons: set[ResolutionReasonCode] = field(default_factory=set)
    fills: dict[str, ExternalFill] = field(default_factory=dict)
    fill_correlations: dict[str, _FillCorrelationFacts] = field(default_factory=dict)
    fill_tid_candidates: dict[str, set[str]] = field(default_factory=dict)
    known_external_order_ids: set[str] = field(default_factory=set)
    context_external_order_ids: set[str] = field(default_factory=set)
    observations_by_key: dict[str, ExternalOrderObservation] = field(default_factory=dict)
    order_records: list[tuple[str, ExternalOrderState, float | None, float | None, str]] = field(
        default_factory=list
    )
    active_states: set[ExternalOrderState] = field(default_factory=set)
    terminal_states: set[ExternalOrderState] = field(default_factory=set)


def _binding_mismatch(
    expected: ExpectedOrderBinding, actual: ExpectedOrderBinding
) -> tuple[bool, bool]:
    conflict = any(
        left != right
        for left, right in (
            (expected.local_order_id, actual.local_order_id),
            (expected.intent_id, actual.intent_id),
            (expected.venue, actual.venue),
            (expected.account_scope, actual.account_scope),
            (expected.engine, actual.engine),
            (expected.instrument, actual.instrument),
            (expected.side, actual.side),
            (expected.expected_client_order_id, actual.expected_client_order_id),
        )
    )
    conflict = conflict or not _same_quantity(expected.requested_qty, actual.requested_qty)
    if (
        expected.expected_external_order_id is not None
        and actual.expected_external_order_id is not None
    ):
        conflict = (
            conflict or expected.expected_external_order_id != actual.expected_external_order_id
        )
    incomplete = (
        expected.expected_external_order_id is not None
        and actual.expected_external_order_id is None
    )
    return conflict, incomplete


def _record_external_order_id(
    state: _AssessmentState,
    external_order_id: str | None,
    *,
    context: bool,
) -> None:
    if external_order_id is None:
        return
    state.known_external_order_ids.add(external_order_id)
    if context:
        state.context_external_order_ids.add(external_order_id)


def _record_fill(
    state: _AssessmentState,
    expected: ExpectedOrderBinding,
    fill: ExternalFill,
) -> bool:
    _record_external_order_id(state, fill.external_order_id, context=False)
    strict_pairs = (
        (fill.local_order_id, expected.local_order_id),
        (fill.intent_id, expected.intent_id),
        (fill.venue, expected.venue),
        (fill.account_scope, expected.account_scope),
        (fill.instrument, expected.instrument),
        (fill.side, expected.side),
    )
    if any(left != right for left, right in strict_pairs):
        state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
        return False
    if expected.expected_external_order_id is not None:
        if fill.external_order_id is None:
            state.binding_incomplete.add(ResolutionReasonCode.BINDING_INCOMPLETE)
        elif fill.external_order_id != expected.expected_external_order_id:
            state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
    if (
        fill.client_order_id is not None
        and fill.client_order_id != expected.expected_client_order_id
    ):
        state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
    assert fill.fill_key is not None
    current = state.fills.get(fill.fill_key)
    if current is None:
        state.fills[fill.fill_key] = fill
    elif not current.is_semantically_compatible_with(fill):
        state.evidence_conflicts.add(ResolutionReasonCode.FILL_QUANTITY_CONFLICT)
        return False

    correlations = state.fill_correlations.setdefault(fill.fill_key, _FillCorrelationFacts())
    if fill.client_order_id is not None:
        correlations.known_client_order_ids.add(fill.client_order_id)
    if fill.external_order_id is not None:
        correlations.known_external_order_ids.add(fill.external_order_id)
    if fill.venue_event_at is not None:
        correlations.known_venue_event_ats.add(fill.venue_event_at)
    return True


def _record_observation(
    state: _AssessmentState,
    expected: ExpectedOrderBinding,
    observation: ExternalOrderObservation,
) -> None:
    _record_external_order_id(state, observation.external_order_id, context=True)
    strict_pairs = (
        (observation.local_order_id, expected.local_order_id),
        (observation.intent_id, expected.intent_id),
        (observation.venue, expected.venue),
        (observation.account_scope, expected.account_scope),
        (observation.instrument, expected.instrument),
        (observation.side, expected.side),
    )
    if any(left != right for left, right in strict_pairs):
        state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
        return
    if not _same_quantity(observation.requested_qty, expected.requested_qty):
        state.evidence_conflicts.add(ResolutionReasonCode.ORDER_QUANTITY_CONFLICT)
    if observation.client_order_id is None:
        state.binding_incomplete.add(ResolutionReasonCode.BINDING_INCOMPLETE)
    elif observation.client_order_id != expected.expected_client_order_id:
        state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
    if expected.expected_external_order_id is not None:
        if observation.external_order_id is None:
            state.binding_incomplete.add(ResolutionReasonCode.BINDING_INCOMPLETE)
        elif observation.external_order_id != expected.expected_external_order_id:
            state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
    assert observation.observation_key is not None
    existing = state.observations_by_key.get(observation.observation_key)
    if existing is not None:
        if existing.semantic_content() != observation.semantic_content():
            state.evidence_conflicts.add(ResolutionReasonCode.ORDER_OBSERVATION_IDENTITY_CONFLICT)
        return
    state.observations_by_key[observation.observation_key] = observation
    state.order_records.append(
        (
            observation.observed_at,
            ExternalOrderState(observation.normalized_external_status),
            observation.cumulative_filled_qty,
            observation.remaining_qty,
            observation.observation_key,
        )
    )


def _record_order_evidence(
    state: _AssessmentState,
    expected: ExpectedOrderBinding,
    evidence: OrderLookupEvidence,
) -> None:
    _record_external_order_id(state, evidence.external_order_id, context=True)
    strict_pairs = (
        (evidence.local_order_id, expected.local_order_id),
        (evidence.intent_id, expected.intent_id),
        (evidence.venue, expected.venue),
        (evidence.account_scope, expected.account_scope),
        (evidence.instrument, expected.instrument),
        (evidence.side, expected.side),
        (evidence.engine, expected.engine),
    )
    if any(left != right for left, right in strict_pairs):
        state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
        return
    if not evidence.correlation_complete:
        state.binding_incomplete.add(ResolutionReasonCode.BINDING_INCOMPLETE)
    if not evidence.quantities_complete:
        state.binding_incomplete.add(ResolutionReasonCode.BINDING_INCOMPLETE)
    if evidence.returned_client_order_id is None:
        state.binding_incomplete.add(ResolutionReasonCode.BINDING_INCOMPLETE)
    elif evidence.returned_client_order_id != expected.expected_client_order_id:
        state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
    if expected.expected_external_order_id is not None:
        if evidence.external_order_id is None:
            state.binding_incomplete.add(ResolutionReasonCode.BINDING_INCOMPLETE)
        elif evidence.external_order_id != expected.expected_external_order_id:
            state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
    if evidence.contradictory:
        state.evidence_conflicts.add(ResolutionReasonCode.ORDER_QUANTITY_CONFLICT)
    if (evidence.requested_qty is not None and evidence.requested_qty <= 0) or any(
        value is not None and value < 0 for value in (evidence.filled_qty, evidence.remaining_qty)
    ):
        state.evidence_conflicts.add(ResolutionReasonCode.ORDER_QUANTITY_CONFLICT)
    if (
        evidence.requested_qty is not None
        and evidence.filled_qty is not None
        and evidence.filled_qty
        > evidence.requested_qty + max(1e-9, abs(evidence.requested_qty) * 1e-9)
    ):
        state.evidence_conflicts.add(ResolutionReasonCode.ORDER_QUANTITY_CONFLICT)
    if evidence.requested_qty is not None and not _same_quantity(
        evidence.requested_qty, expected.requested_qty
    ):
        state.evidence_conflicts.add(ResolutionReasonCode.ORDER_QUANTITY_CONFLICT)
    if (
        evidence.requested_qty is not None
        and evidence.filled_qty is not None
        and evidence.remaining_qty is not None
        and not _same_quantity(evidence.requested_qty, evidence.filled_qty + evidence.remaining_qty)
    ):
        state.evidence_conflicts.add(ResolutionReasonCode.ORDER_QUANTITY_CONFLICT)
    key = f"lookup:{evidence.observed_at}:{evidence.raw_payload_hash}"
    state.order_records.append(
        (
            evidence.observed_at,
            evidence.normalized_state,
            evidence.filled_qty,
            evidence.remaining_qty,
            key,
        )
    )


def _record_order_lookup(
    state: _AssessmentState,
    expected: ExpectedOrderBinding,
    lookup: OrderLookupFact,
) -> None:
    _record_external_order_id(state, lookup.binding.expected_external_order_id, context=True)
    conflict, incomplete = _binding_mismatch(expected, lookup.binding)
    if conflict:
        state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
    if incomplete:
        state.binding_incomplete.add(ResolutionReasonCode.BINDING_INCOMPLETE)
    if lookup.evidence is not None:
        _record_order_evidence(state, expected, lookup.evidence)
    if lookup.outcome == OrderLookupOutcome.NOT_FOUND:
        state.reasons.add(ResolutionReasonCode.ORDER_LOOKUP_NOT_FOUND)
    elif lookup.outcome in {
        OrderLookupOutcome.TRANSPORT_FAILURE,
        OrderLookupOutcome.UNSUPPORTED,
    }:
        state.reasons.add(ResolutionReasonCode.ORDER_LOOKUP_UNAVAILABLE)
    elif lookup.outcome == OrderLookupOutcome.INCOMPLETE_RESPONSE:
        state.reasons.add(ResolutionReasonCode.ORDER_LOOKUP_INCOMPLETE)
    elif lookup.outcome in {
        OrderLookupOutcome.INVALID_RESPONSE,
        OrderLookupOutcome.CONFLICTING_RESPONSE,
    }:
        state.evidence_conflicts.add(ResolutionReasonCode.STATUS_CONFLICT)


def _record_fill_lookup(
    state: _AssessmentState,
    expected: ExpectedOrderBinding,
    lookup: FillLookupFact,
) -> None:
    _record_external_order_id(state, lookup.binding.expected_external_order_id, context=True)
    conflict, incomplete = _binding_mismatch(expected, lookup.binding)
    if conflict:
        state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
    if incomplete:
        state.binding_incomplete.add(ResolutionReasonCode.BINDING_INCOMPLETE)
    for fill, candidate in sorted(
        zip(lookup.fills, lookup.venue_fill_id_candidates, strict=True),
        key=lambda item: item[0].fill_key or "",
    ):
        if _record_fill(state, expected, fill) and candidate is not None:
            assert fill.fill_key is not None
            state.fill_tid_candidates.setdefault(fill.fill_key, set()).add(candidate)
    if any(candidate is not None for candidate in lookup.venue_fill_id_candidates):
        state.reasons.add(ResolutionReasonCode.TID_CANDIDATE_NOT_IDENTITY)
    if lookup.response_limit_reached:
        state.reasons.add(ResolutionReasonCode.FILL_LOOKUP_INCOMPLETE)
    if lookup.outcome == FillLookupOutcome.NO_MATCH:
        state.reasons.add(ResolutionReasonCode.FILL_LOOKUP_NO_MATCH)
    elif lookup.outcome in {
        FillLookupOutcome.TRANSPORT_FAILURE,
        FillLookupOutcome.UNSUPPORTED,
    }:
        state.reasons.add(ResolutionReasonCode.FILL_LOOKUP_UNAVAILABLE)
    elif lookup.outcome == FillLookupOutcome.INCOMPLETE_RESPONSE:
        state.reasons.add(ResolutionReasonCode.FILL_LOOKUP_INCOMPLETE)
    elif lookup.outcome in {
        FillLookupOutcome.INVALID_RESPONSE,
        FillLookupOutcome.CONFLICTING_RESPONSE,
    }:
        state.evidence_conflicts.add(ResolutionReasonCode.STATUS_CONFLICT)
    if lookup.response_count >= lookup.response_limit:
        state.reasons.add(ResolutionReasonCode.FILL_LOOKUP_INCOMPLETE)


def _canonical_observations(
    observations: Iterable[ExternalOrderObservation],
) -> tuple[ExternalOrderObservation, ...]:
    return tuple(
        sorted(
            observations,
            key=lambda item: (
                _timestamp_key(item.observed_at),
                ExternalOrderState(item.normalized_external_status).value,
                item.cumulative_filled_qty if item.cumulative_filled_qty is not None else -1.0,
                item.remaining_qty if item.remaining_qty is not None else -1.0,
                item.observation_key or "",
            ),
        )
    )


def _conservative_fill_lower_bound(state: _AssessmentState) -> float:
    """Return a lower bound valid even if same-candidate TIDs alias fills.

    TID remains only a candidate. Distinct fill keys sharing a candidate can
    be redeliveries of one venue fill, so each connected ambiguity component
    contributes at most the smallest observed quantity.
    """

    candidate_groups: dict[str, set[str]] = {}
    for fill_key, candidates in state.fill_tid_candidates.items():
        for candidate in candidates:
            candidate_groups.setdefault(candidate, set()).add(fill_key)
    ambiguous_groups = [group for group in candidate_groups.values() if len(group) > 1]
    if not ambiguous_groups:
        return sum(fill.quantity for fill in state.fills.values())

    state.evidence_conflicts.add(ResolutionReasonCode.FILL_IDENTITY_AMBIGUITY)
    state.reasons.add(ResolutionReasonCode.FILL_IDENTITY_AMBIGUITY)
    adjacency: dict[str, set[str]] = {}
    for group in ambiguous_groups:
        for fill_key in group:
            adjacency.setdefault(fill_key, set()).update(group - {fill_key})
    ambiguous_keys = set(adjacency)
    lower_bound = sum(
        fill.quantity for fill_key, fill in state.fills.items() if fill_key not in ambiguous_keys
    )
    while adjacency:
        start = min(adjacency)
        component: set[str] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(adjacency.get(current, ()))
        for fill_key in component:
            adjacency.pop(fill_key, None)
        lower_bound += min(state.fills[fill_key].quantity for fill_key in component)
    return lower_bound


def _canonical_fills(fills: Iterable[ExternalFill]) -> tuple[ExternalFill, ...]:
    return tuple(sorted(fills, key=lambda item: item.fill_key or ""))


def _ordered_codes(values: Iterable[ResolutionReasonCode]) -> tuple[ResolutionReasonCode, ...]:
    return tuple(sorted(set(values), key=lambda item: item.value))


def _assess_temporal_consistency(state: _AssessmentState) -> None:
    """Audit acquisition history without inferring venue status chronology.

    Facts with an identical ``observed_at`` form one simultaneous acquisition
    group. They do not establish an order amongst themselves. Only a strictly
    later group is checked against remembered cumulative quantity and terminal
    status from earlier groups.
    """

    records = sorted(
        state.order_records,
        key=lambda item: (
            _timestamp_key(item[0]),
            item[1].value,
            item[2] if item[2] is not None else -1.0,
            item[3] if item[3] is not None else -1.0,
            item[4],
        ),
    )
    maximum_cumulative: float | None = None
    terminal_seen_earlier = False
    index = 0
    while index < len(records):
        timestamp = records[index][0]
        timestamp_key = _timestamp_key(timestamp)
        group: list[tuple[str, ExternalOrderState, float | None, float | None, str]] = []
        while index < len(records) and _timestamp_key(records[index][0]) == timestamp_key:
            group.append(records[index])
            index += 1

        cumulative_values = [record[2] for record in group if record[2] is not None]
        if maximum_cumulative is not None:
            for cumulative in cumulative_values:
                assert cumulative is not None
                if cumulative < maximum_cumulative and not _same_quantity(
                    cumulative, maximum_cumulative
                ):
                    state.evidence_conflicts.add(ResolutionReasonCode.ORDER_QUANTITY_CONFLICT)
        if terminal_seen_earlier and any(
            record[1] in {ExternalOrderState.OPEN, ExternalOrderState.PARTIAL_OPEN}
            for record in group
        ):
            state.evidence_conflicts.add(ResolutionReasonCode.STATUS_CONFLICT)

        if cumulative_values:
            group_maximum = max(cumulative_values)
            maximum_cumulative = (
                group_maximum
                if maximum_cumulative is None
                else max(maximum_cumulative, group_maximum)
            )
        if any(record[1].is_terminal for record in group):
            terminal_seen_earlier = True
    if len(state.terminal_states) > 1:
        state.evidence_conflicts.add(ResolutionReasonCode.STATUS_CONFLICT)


def assess_resolution(bundle: ResolutionEvidenceBundle) -> ResolutionAssessment:
    """Return a deterministic, fail-closed assessment of ``bundle``.

    ``venue_event_at`` is intentionally never used to order statuses: on the
    current Hyperliquid/CCXT path it is the order timestamp, not a status
    transition timestamp.  ``observed_at`` orders local acquisition facts only.
    """

    if not isinstance(bundle, ResolutionEvidenceBundle):
        raise TypeError("bundle must be ResolutionEvidenceBundle")
    expected = bundle.binding
    state = _AssessmentState()
    _record_external_order_id(state, expected.expected_external_order_id, context=True)

    observations = _canonical_observations(bundle.order_observations)
    fills = _canonical_fills(bundle.fills)
    for observation in observations:
        _record_observation(state, expected, observation)
    for fill in fills:
        _record_fill(state, expected, fill)
    for order_lookup in sorted(
        bundle.order_lookups,
        key=lambda item: (
            item.binding.local_order_id,
            item.binding.intent_id,
            OrderLookupOutcome(item.outcome).value,
            item.evidence.observed_at if item.evidence is not None else "",
            item.evidence.raw_payload_hash if item.evidence is not None else "",
        ),
    ):
        _record_order_lookup(state, expected, order_lookup)
    for fill_lookup in sorted(
        bundle.fill_lookups,
        key=lambda item: (
            item.binding.local_order_id,
            item.binding.intent_id,
            FillLookupOutcome(item.outcome).value,
            item.response_count if item.response_count is not None else -1,
            tuple(fill.fill_key or "" for fill in item.fills),
        ),
    ):
        _record_fill_lookup(state, expected, fill_lookup)

    if len(state.known_external_order_ids) > 1:
        state.binding_conflicts.add(ResolutionReasonCode.BINDING_CONFLICT)
    for correlations in state.fill_correlations.values():
        if expected.expected_client_order_id not in correlations.known_client_order_ids and not (
            correlations.known_external_order_ids & state.context_external_order_ids
        ):
            state.binding_incomplete.add(ResolutionReasonCode.BINDING_INCOMPLETE)

    for record in state.order_records:
        if record[1].is_terminal:
            state.terminal_states.add(record[1])
            state.reasons.add(ResolutionReasonCode.TERMINAL_ORDER_OBSERVED)
        elif record[1] in {ExternalOrderState.OPEN, ExternalOrderState.PARTIAL_OPEN}:
            state.active_states.add(record[1])
            state.reasons.add(ResolutionReasonCode.ACTIVE_ORDER_OBSERVED)
    _assess_temporal_consistency(state)

    lower_bound = 0.0
    if state.fills:
        state.reasons.add(ResolutionReasonCode.FILL_POSITIVE_EVIDENCE)
        state.reasons.add(ResolutionReasonCode.FILL_COMPLETENESS_UNPROVEN)
        lower_bound = _conservative_fill_lower_bound(state)
        if lower_bound > expected.requested_qty + expected.quantity_tolerance:
            state.evidence_conflicts.add(ResolutionReasonCode.FILL_QUANTITY_CONFLICT)
    state.reasons.add(ResolutionReasonCode.ZERO_EFFECT_UNPROVEN)

    if state.binding_conflicts:
        outcome = ResolutionOutcome.BINDING_CONFLICT
    elif state.binding_incomplete:
        outcome = ResolutionOutcome.BINDING_INCOMPLETE
    elif state.evidence_conflicts:
        outcome = ResolutionOutcome.EVIDENCE_CONFLICT
    elif state.fills:
        outcome = ResolutionOutcome.EFFECT_PROVEN_INCOMPLETE
    elif state.active_states and not state.terminal_states:
        outcome = ResolutionOutcome.EXTERNAL_ACTIVE
    else:
        outcome = ResolutionOutcome.UNRESOLVED
        if not state.order_records and not bundle.order_lookups:
            state.reasons.add(ResolutionReasonCode.NO_ORDER_EVIDENCE)

    if state.fills and state.order_records:
        state.reasons.add(ResolutionReasonCode.ORDER_FILL_TIMING_UNPROVEN)
    terminal_states = tuple(sorted(state.terminal_states, key=lambda item: item.value))
    return ResolutionAssessment(
        outcome=outcome,
        proven_filled_lower_bound=(
            0.0 if state.binding_conflicts or state.binding_incomplete else lower_bound
        ),
        requested_qty=expected.requested_qty,
        external_order_active=(
            bool(state.active_states)
            and not state.terminal_states
            and not state.binding_conflicts
            and not state.binding_incomplete
            and not state.evidence_conflicts
        ),
        terminal_state_observed=bool(state.terminal_states),
        terminal_states_observed=terminal_states,
        fill_completeness_proven=False,
        zero_effect_proven=False,
        binding_complete=not state.binding_conflicts and not state.binding_incomplete,
        conflicts=_ordered_codes(state.binding_conflicts | state.evidence_conflicts),
        reasons=_ordered_codes(state.reasons | state.binding_conflicts | state.binding_incomplete),
        deduplicated_fill_keys=tuple(sorted(state.fills)),
        tid_identity_proven=False,
    )


__all__ = [
    "ExpectedOrderBinding",
    "FillLookupFact",
    "FillLookupOutcome",
    "OrderLookupEvidence",
    "OrderLookupFact",
    "OrderLookupOutcome",
    "ResolutionAssessment",
    "ResolutionEvidenceBundle",
    "ResolutionOutcome",
    "ResolutionReasonCode",
    "assess_resolution",
]
