"""Authoritative, local-only immutable evidence for a PAPER execution.

This module deliberately models the synchronous result returned by
``PaperBroker``.  It is not a venue adapter and must never be used to assign
an identity to an external exchange fill.  A successful PAPER execution has
no effect outside the process until this evidence is committed durably.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from .broker import BrokerOrderResult
from .external_evidence import ExternalEvidenceSource, ExternalFill, ExternalOrderObservation


PAPER_EVIDENCE_VERSION = "paper-local-execution-v1"
PAPER_VENUE = "paper-local"
PAPER_FEE_ASSET = "USDC"
PAPER_EXECUTION_EVIDENCE_EVENT_TYPE = "PAPER_EXECUTION_EVIDENCE_PERSISTED"
PAPER_EXECUTION_EVIDENCE_AGGREGATE_TYPE = "paper_execution_evidence"


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class PaperExecutionEvidenceContext:
    """Local binding for one PAPER broker result.

    ``account_scope`` is a local namespace only.  It is intentionally not a
    wallet, sub-account, or exchange account identity.
    """

    local_order_id: int
    intent_id: str
    engine: str
    instrument: str
    side: str
    account_scope: str = "paper-local"

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        for field in ("intent_id", "engine", "instrument", "account_scope"):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        side = _required_text(self.side, "side").upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        object.__setattr__(self, "side", side)


@dataclass(frozen=True)
class PaperExecutionEvidence:
    """One immutable, local PAPER submission result.

    The optional fill has a deterministic *local* ``venue_fill_id`` only to
    satisfy the immutable-fill contract.  The identifier's namespace makes no
    assertion about a venue-native identity.
    """

    context: PaperExecutionEvidenceContext
    observation: ExternalOrderObservation
    fill: ExternalFill | None
    raw_payload_hash: str
    reported_remaining_qty: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, PaperExecutionEvidenceContext):
            raise ValueError("context must be PaperExecutionEvidenceContext")
        if not isinstance(self.observation, ExternalOrderObservation):
            raise ValueError("observation must be ExternalOrderObservation")
        if self.fill is not None and not isinstance(self.fill, ExternalFill):
            raise ValueError("fill must be ExternalFill or None")
        if self.reported_remaining_qty is not None and (
            isinstance(self.reported_remaining_qty, bool)
            or not math.isfinite(self.reported_remaining_qty)
            or self.reported_remaining_qty < 0.0
        ):
            raise ValueError("reported_remaining_qty must be finite and non-negative")
        if self.observation.source_kind != ExternalEvidenceSource.SUBMISSION_RESPONSE:
            raise ValueError("paper observation must be a SUBMISSION_RESPONSE")
        if self.fill is not None:
            if self.fill.source_kind != ExternalEvidenceSource.SUBMISSION_RESPONSE:
                raise ValueError("paper fill must be a SUBMISSION_RESPONSE")
            if self.fill.venue_fill_id is None:
                raise ValueError("paper fill requires a durable local fill identity")
            if not self.fill.venue_fill_id.startswith("paper-local-fill-v1-"):
                raise ValueError("paper fill identity must remain in the local PAPER namespace")
        for evidence in (self.observation, self.fill):
            if evidence is None:
                continue
            if (
                evidence.local_order_id != self.context.local_order_id
                or evidence.intent_id != self.context.intent_id
                or evidence.venue != PAPER_VENUE
                or evidence.account_scope != self.context.account_scope
                or evidence.instrument != self.context.instrument
                or evidence.side != self.context.side
                or evidence.client_order_id != self.context.intent_id
            ):
                raise ValueError("paper evidence does not match its local binding")
            if evidence.raw_payload_hash != self.raw_payload_hash:
                raise ValueError("paper evidence must retain the submission payload hash")
            if evidence.external_order_id is not None:
                raise ValueError("paper evidence cannot claim an external order identity")
            if evidence.venue_event_at is not None:
                raise ValueError("paper evidence cannot claim a venue event time")


@dataclass(frozen=True)
class PaperExecutionEvidencePersistenceResult:
    """Durable result of one idempotent PAPER-evidence persistence attempt."""

    observation: ExternalOrderObservation
    observation_created: bool
    fill: ExternalFill | None
    fill_created: bool


def build_paper_execution_evidence(
    context: PaperExecutionEvidenceContext,
    result: BrokerOrderResult,
    *,
    observed_at: str,
) -> PaperExecutionEvidence:
    """Convert a synchronous PAPER result to immutable local evidence.

    The identifier derives only from durable submission identity and the
    local-evidence namespace; ``observed_at`` is deliberately excluded from
    identity and payload hashing so ingestion time cannot alter a replay.
    """

    if not isinstance(context, PaperExecutionEvidenceContext):
        raise TypeError("context must be PaperExecutionEvidenceContext")
    if not isinstance(result, BrokerOrderResult):
        raise TypeError("result must be BrokerOrderResult")

    fill = result.fill
    payload = {
        "contract": PAPER_EVIDENCE_VERSION,
        "local_order_id": context.local_order_id,
        "intent_id": context.intent_id,
        "engine": context.engine,
        "venue": PAPER_VENUE,
        "account_scope": context.account_scope,
        "instrument": context.instrument,
        "side": context.side,
        "status": result.status.value,
        "requested_qty": result.requested_qty,
        "remaining_qty": result.remaining_qty,
        "price": fill.price,
        "filled_qty": fill.qty,
        "fee": fill.fee,
    }
    raw_payload_hash = _sha256(payload)
    observation_remaining: float | None = result.remaining_qty
    if abs(fill.qty + result.remaining_qty - result.requested_qty) > max(
        1e-9, result.requested_qty * 1e-9
    ):
        observation_remaining = None
    observation = ExternalOrderObservation(
        local_order_id=context.local_order_id,
        intent_id=context.intent_id,
        venue=PAPER_VENUE,
        account_scope=context.account_scope,
        instrument=context.instrument,
        side=context.side,
        source_kind=ExternalEvidenceSource.SUBMISSION_RESPONSE,
        normalized_external_status=result.status,
        requested_qty=result.requested_qty,
        cumulative_filled_qty=fill.qty,
        remaining_qty=observation_remaining,
        client_order_id=context.intent_id,
        observed_at=observed_at,
        raw_payload_hash=raw_payload_hash,
    )
    paper_fill: ExternalFill | None = None
    if fill.qty > 0:
        fill_identity = _sha256(
            {
                "contract": PAPER_EVIDENCE_VERSION,
                "kind": "individual-fill",
                "local_order_id": context.local_order_id,
                "intent_id": context.intent_id,
                "side": context.side,
                "engine": context.engine,
                "account_scope": context.account_scope,
                "instrument": context.instrument,
            }
        )
        paper_fill = ExternalFill(
            local_order_id=context.local_order_id,
            intent_id=context.intent_id,
            venue=PAPER_VENUE,
            account_scope=context.account_scope,
            instrument=context.instrument,
            side=context.side,
            source_kind=ExternalEvidenceSource.SUBMISSION_RESPONSE,
            client_order_id=context.intent_id,
            venue_fill_id=f"paper-local-fill-v1-{fill_identity}",
            quantity=fill.qty,
            price=fill.price,
            fee=fill.fee,
            fee_asset=PAPER_FEE_ASSET,
            observed_at=observed_at,
            raw_payload_hash=raw_payload_hash,
        )
    return PaperExecutionEvidence(
        context=context,
        observation=observation,
        fill=paper_fill,
        raw_payload_hash=raw_payload_hash,
        reported_remaining_qty=result.remaining_qty,
    )


__all__ = [
    "PAPER_EVIDENCE_VERSION",
    "PAPER_EXECUTION_EVIDENCE_AGGREGATE_TYPE",
    "PAPER_EXECUTION_EVIDENCE_EVENT_TYPE",
    "PAPER_FEE_ASSET",
    "PAPER_VENUE",
    "PaperExecutionEvidence",
    "PaperExecutionEvidenceContext",
    "PaperExecutionEvidencePersistenceResult",
    "build_paper_execution_evidence",
]
