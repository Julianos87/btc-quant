"""Bounded, read-only acquisition of complete external IOC settlement evidence.

This module is the acquisition boundary for the future external coordinator.
It performs no persistence and no financial application: callers must persist
the returned lookup facts before projecting or assessing them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from .external_evidence import ExternalEvidenceSource, ExternalFill
from .external_evidence_reader import (
    EvidenceLookupOutcome,
    ExternalEvidenceReader,
    OrderEvidenceLookup,
    OrderLookupContext,
)
from .external_fill_evidence_reader import (
    ExternalFillEvidenceReader,
    FillEvidenceLookup,
    FillEvidenceLookupOutcome,
    FillLookupContext,
)
from .external_submission_commitment import AuthoritativeSubmissionFillCommitment
from .financial_order_settlement import (
    ExternalOrderSettlement,
    ExternalSettlementFillRow,
    FinancialSettlementError,
    SETTLEMENT_COMPLETENESS_COMMITMENT_VERSION,
    SettlementCompletenessProof,
)
from .order_state import ExternalOrderState


FILL_ACQUISITION_STABILITY_LOOKUPS = 2
_SUPPORTED_FEE_ASSET = "USDC"


class ExternalSettlementAcquisitionError(ValueError):
    """Invalid acquisition context or an unsafe acquisition result."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalSettlementAcquisitionError(f"{field} must be non-empty")
    return value.strip()


def _timestamp(value: object, field: str) -> str:
    candidate = _text(value, field)
    if candidate[-1:] in {"Z", "z"}:
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ExternalSettlementAcquisitionError(
            f"{field} must be ISO 8601 with an explicit timezone"
        ) from error
    if parsed.tzinfo is None:
        raise ExternalSettlementAcquisitionError(f"{field} must contain an explicit timezone")
    return parsed.astimezone(UTC).isoformat()


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ExternalSettlementAcquisitionError(f"{field} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ExternalSettlementAcquisitionError(f"{field} must be numeric") from error
    if not number.is_finite() or (positive and number <= 0):
        raise ExternalSettlementAcquisitionError(f"{field} has an invalid value")
    return number


def _millis(timestamp: str) -> int:
    return int(datetime.fromisoformat(timestamp).timestamp() * 1000)


def _binding_matches(
    *,
    context: ExternalSettlementAcquisitionContext,
    commitment: AuthoritativeSubmissionFillCommitment,
) -> bool:
    return all(
        left == right
        for left, right in (
            (commitment.local_order_id, context.local_order_id),
            (commitment.intent_id, context.intent_id),
            (commitment.venue, context.venue),
            (commitment.environment, context.environment),
            (commitment.account_scope, context.account_scope),
            (commitment.instrument, context.instrument),
            (commitment.side, context.side),
            (commitment.client_order_id, context.client_order_id),
        )
    )


@dataclass(frozen=True)
class SettlementRetentionWitness:
    """Independent bounded-history witness required by the settlement proof."""

    response_count: int
    oldest_event_at: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.response_count, bool)
            or not isinstance(self.response_count, int)
            or self.response_count <= 0
        ):
            raise ExternalSettlementAcquisitionError("retention response_count must be positive")
        object.__setattr__(
            self, "oldest_event_at", _timestamp(self.oldest_event_at, "oldest_event_at")
        )


@dataclass(frozen=True)
class ExternalSettlementAcquisitionContext:
    """Durable order/plan binding supplied by the orchestration layer."""

    local_order_id: int
    intent_id: str
    venue: str
    environment: str
    account_scope: str
    instrument: str
    side: str
    engine: str
    client_order_id: str
    requested_qty: float
    planned_effect_at: str
    window_start: str
    window_end: str
    submission_commitment: AuthoritativeSubmissionFillCommitment | None
    external_order_id: str | None = None
    retention_witness: SettlementRetentionWitness | None = None
    stability_lookups: int = FILL_ACQUISITION_STABILITY_LOOKUPS

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise ExternalSettlementAcquisitionError("local_order_id must be positive")
        for field in (
            "intent_id",
            "venue",
            "environment",
            "account_scope",
            "instrument",
            "engine",
            "client_order_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        side = _text(self.side, "side").upper()
        if side not in {"BUY", "SELL"}:
            raise ExternalSettlementAcquisitionError("side must be BUY or SELL")
        object.__setattr__(self, "side", side)
        requested_qty = _decimal(self.requested_qty, "requested_qty", positive=True)
        object.__setattr__(self, "requested_qty", float(requested_qty))
        object.__setattr__(
            self, "planned_effect_at", _timestamp(self.planned_effect_at, "planned_effect_at")
        )
        start = _timestamp(self.window_start, "window_start")
        end = _timestamp(self.window_end, "window_end")
        if end < start:
            raise ExternalSettlementAcquisitionError("window_end precedes window_start")
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        if self.external_order_id is not None:
            object.__setattr__(
                self, "external_order_id", _text(self.external_order_id, "external_order_id")
            )
        if self.submission_commitment is not None:
            if not isinstance(self.submission_commitment, AuthoritativeSubmissionFillCommitment):
                raise ExternalSettlementAcquisitionError("invalid submission commitment")
            if not _binding_matches(context=self, commitment=self.submission_commitment):
                raise ExternalSettlementAcquisitionError("submission commitment binding conflict")
            if (
                self.external_order_id is not None
                and self.external_order_id != self.submission_commitment.external_order_id
            ):
                raise ExternalSettlementAcquisitionError("external order id binding conflict")
        if (
            isinstance(self.stability_lookups, bool)
            or not isinstance(self.stability_lookups, int)
            or self.stability_lookups not in {1, 2}
        ):
            raise ExternalSettlementAcquisitionError("stability_lookups must be 1 or 2")


@dataclass(frozen=True)
class ExternalSettlementAcquisitionResult:
    """Read-only result handed to persistence/projection orchestration."""

    context: ExternalSettlementAcquisitionContext
    order_lookup: OrderEvidenceLookup
    fill_lookups: tuple[FillEvidenceLookup, ...] = ()
    settlement: ExternalOrderSettlement | None = None
    acquisition_performed: bool = True
    blocking_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, ExternalSettlementAcquisitionContext):
            raise TypeError("context must be ExternalSettlementAcquisitionContext")
        if not isinstance(self.order_lookup, OrderEvidenceLookup):
            raise TypeError("order_lookup must be OrderEvidenceLookup")
        object.__setattr__(self, "fill_lookups", tuple(self.fill_lookups))
        if not isinstance(self.acquisition_performed, bool):
            raise TypeError("acquisition_performed must be bool")
        if self.blocking_reason is not None:
            object.__setattr__(
                self, "blocking_reason", _text(self.blocking_reason, "blocking_reason")
            )


class ExternalSettlementEvidenceAcquirer(Protocol):
    """Bounded read-only acquisition interface; no writer or broker methods."""

    def acquire(
        self,
        context: ExternalSettlementAcquisitionContext,
        *,
        observed_at: str | None = None,
    ) -> ExternalSettlementAcquisitionResult: ...


def _fill_signature(fill: ExternalFill) -> tuple[object, ...]:
    return (
        fill.raw_payload_hash,
        fill.external_order_id,
        fill.account_scope,
        fill.instrument,
        fill.side,
        fill.client_order_id,
        fill.quantity,
        fill.price,
        fill.fee,
        fill.fee_asset,
        fill.venue_event_at,
    )


def _rows_from_lookup(
    lookup: FillEvidenceLookup,
    context: ExternalSettlementAcquisitionContext,
    external_order_id: str,
) -> tuple[ExternalSettlementFillRow, ...]:
    if lookup.outcome != FillEvidenceLookupOutcome.FOUND:
        raise ExternalSettlementAcquisitionError("fill lookup is not FOUND")
    if lookup.response_limit_reached:
        raise ExternalSettlementAcquisitionError("SETTLEMENT_RESPONSE_LIMIT_REACHED")
    rows: list[ExternalSettlementFillRow] = []
    for fill, candidate in zip(lookup.fills, lookup.venue_fill_id_candidates, strict=True):
        if fill.source_kind != ExternalEvidenceSource.FILL_LOOKUP:
            raise ExternalSettlementAcquisitionError("fill provenance is not FILL_LOOKUP")
        if (
            fill.local_order_id != context.local_order_id
            or fill.intent_id != context.intent_id
            or fill.venue != context.venue
            or fill.account_scope != context.account_scope
            or fill.instrument != context.instrument
            or fill.side != context.side
            or fill.external_order_id != external_order_id
            or (
                fill.client_order_id is not None and fill.client_order_id != context.client_order_id
            )
            or fill.fee is None
            or fill.fee_asset != _SUPPORTED_FEE_ASSET
        ):
            raise ExternalSettlementAcquisitionError("fill binding or fee contract conflict")
        rows.append(
            ExternalSettlementFillRow(
                external_order_id=fill.external_order_id,
                account_scope=fill.account_scope,
                instrument=fill.instrument,
                side=fill.side,
                quantity=fill.quantity,
                price=fill.price,
                fee=fill.fee,
                fee_asset=fill.fee_asset,
                venue_event_at=fill.venue_event_at or fill.observed_at,
                raw_payload_hash=fill.raw_payload_hash,
                client_order_id=fill.client_order_id,
                reported_trade_id_candidate=candidate,
            )
        )
    if not rows:
        raise ExternalSettlementAcquisitionError("FOUND fill lookup has no fills")
    return tuple(rows)


class CcxtExternalSettlementAcquirer:
    """One order lookup plus one bounded fill lookup and optional stability read."""

    def __init__(
        self,
        order_reader: ExternalEvidenceReader,
        fill_reader: ExternalFillEvidenceReader,
    ) -> None:
        self.order_reader = order_reader
        self.fill_reader = fill_reader

    def acquire(
        self,
        context: ExternalSettlementAcquisitionContext,
        *,
        observed_at: str | None = None,
    ) -> ExternalSettlementAcquisitionResult:
        order_context = OrderLookupContext(
            local_order_id=context.local_order_id,
            intent_id=context.intent_id,
            venue=context.venue,
            account_scope=context.account_scope,
            instrument=context.instrument,
            side=context.side,
            expected_client_order_id=context.client_order_id,
            engine=context.engine,
            requested_qty=context.requested_qty,
        )
        order_lookup = self.order_reader.lookup_order(order_context, observed_at=observed_at)
        if order_lookup.outcome != EvidenceLookupOutcome.FOUND or order_lookup.evidence is None:
            return ExternalSettlementAcquisitionResult(
                context, order_lookup, blocking_reason=f"ORDER_LOOKUP_{order_lookup.outcome.value}"
            )
        evidence = order_lookup.evidence
        if (
            evidence.source_kind != ExternalEvidenceSource.ORDER_LOOKUP
            or not evidence.correlation_complete
            or evidence.external_order_id is None
            or evidence.requested_qty is None
            or evidence.status_event_at is None
        ):
            return ExternalSettlementAcquisitionResult(
                context, order_lookup, blocking_reason="ORDER_EVIDENCE_INCOMPLETE"
            )
        if _decimal(evidence.requested_qty, "order.requested_qty") != _decimal(
            context.requested_qty, "requested_qty"
        ):
            return ExternalSettlementAcquisitionResult(
                context, order_lookup, blocking_reason="ORDER_REQUESTED_QTY_CONFLICT"
            )
        if (
            context.external_order_id is not None
            and evidence.external_order_id != context.external_order_id
        ):
            return ExternalSettlementAcquisitionResult(
                context, order_lookup, blocking_reason="EXTERNAL_ORDER_ID_CONFLICT"
            )
        if context.submission_commitment is None:
            return ExternalSettlementAcquisitionResult(
                context, order_lookup, blocking_reason="SUBMISSION_FILL_COMMITMENT_MISSING"
            )
        if evidence.external_order_id != context.submission_commitment.external_order_id:
            return ExternalSettlementAcquisitionResult(
                context, order_lookup, blocking_reason="SUBMISSION_COMMITMENT_OID_CONFLICT"
            )
        if not ExternalOrderState(evidence.normalized_state).is_terminal:
            return ExternalSettlementAcquisitionResult(
                context, order_lookup, blocking_reason="ORDER_NOT_TERMINAL"
            )

        fill_lookups: list[FillEvidenceLookup] = []
        for _ in range(context.stability_lookups):
            fill_context = FillLookupContext(
                local_order_id=context.local_order_id,
                intent_id=context.intent_id,
                venue=context.venue,
                account_scope=context.account_scope,
                instrument=context.instrument,
                side=context.side,
                expected_external_order_id=evidence.external_order_id,
                expected_client_order_id=context.client_order_id,
                start_time_ms=_millis(context.window_start),
                end_time_ms=_millis(context.window_end),
                engine=context.engine,
            )
            lookup = self.fill_reader.lookup_fills(fill_context, observed_at=observed_at)
            fill_lookups.append(lookup)
            if lookup.outcome != FillEvidenceLookupOutcome.FOUND:
                return ExternalSettlementAcquisitionResult(
                    context,
                    order_lookup,
                    tuple(fill_lookups),
                    blocking_reason=f"FILL_LOOKUP_{lookup.outcome.value}",
                )
            if lookup.response_limit_reached:
                return ExternalSettlementAcquisitionResult(
                    context,
                    order_lookup,
                    tuple(fill_lookups),
                    blocking_reason="SETTLEMENT_RESPONSE_LIMIT_REACHED",
                )
            if not lookup.fills:
                return ExternalSettlementAcquisitionResult(
                    context,
                    order_lookup,
                    tuple(fill_lookups),
                    blocking_reason="FILL_LOOKUP_EMPTY_FOUND",
                )

        if len(fill_lookups) == 2:
            first_signatures = {_fill_signature(fill) for fill in fill_lookups[0].fills}
            second_signatures = {_fill_signature(fill) for fill in fill_lookups[1].fills}
            if not first_signatures.issubset(second_signatures):
                return ExternalSettlementAcquisitionResult(
                    context,
                    order_lookup,
                    tuple(fill_lookups),
                    blocking_reason="FILL_SNAPSHOT_CONFLICT",
                )
        selected_lookup = fill_lookups[-1]
        try:
            rows = _rows_from_lookup(selected_lookup, context, evidence.external_order_id)
            witness = context.retention_witness
            if witness is None:
                return ExternalSettlementAcquisitionResult(
                    context,
                    order_lookup,
                    tuple(fill_lookups),
                    blocking_reason="RETENTION_COVERAGE_WITNESS_MISSING",
                )
            proof = SettlementCompletenessProof(
                local_order_id=context.local_order_id,
                intent_id=context.intent_id,
                venue=context.venue,
                environment=context.environment,
                account_scope=context.account_scope,
                instrument=context.instrument,
                side=context.side,
                client_order_id=context.client_order_id,
                external_order_id=evidence.external_order_id,
                terminal_status=evidence.normalized_state,
                terminal_status_event_at=evidence.status_event_at,
                window_start=context.window_start,
                window_end=context.window_end,
                response_count=selected_lookup.response_count,
                aggregate_by_time=False,
                malformed_entry_count=0,
                retention_response_count=witness.response_count,
                retention_oldest_event_at=witness.oldest_event_at,
                completeness_version=SETTLEMENT_COMPLETENESS_COMMITMENT_VERSION,
                submission_commitment=context.submission_commitment,
            )
            settlement = ExternalOrderSettlement(
                local_order_id=context.local_order_id,
                intent_id=context.intent_id,
                venue=context.venue,
                environment=context.environment,
                account_scope=context.account_scope,
                instrument=context.instrument,
                side=context.side,
                client_order_id=context.client_order_id,
                external_order_id=evidence.external_order_id,
                terminal_status=evidence.normalized_state,
                terminal_status_event_at=evidence.status_event_at,
                completeness=proof,
                fills=rows,
            )
        except (ExternalSettlementAcquisitionError, FinancialSettlementError) as error:
            return ExternalSettlementAcquisitionResult(
                context,
                order_lookup,
                tuple(fill_lookups),
                blocking_reason=str(error),
            )
        return ExternalSettlementAcquisitionResult(
            context, order_lookup, tuple(fill_lookups), settlement=settlement
        )
