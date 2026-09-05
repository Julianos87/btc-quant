"""Durable orchestration for the bounded external IOC settlement path.

The coordinator is deliberately a thin layer between acquisition and the
already-qualified persistence/application contracts.  It does not contain a
new decision engine, does not finalize orders, and does not authorize a
retry.  Every acquired fact is persisted before the durable resolution
projection is assessed and before the dormant settlement writer is called.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .external_evidence_reader import ExternalEvidencePersistence
from .external_fill_evidence_reader import ExternalFillEvidencePersistence
from .external_settlement_acquisition import (
    ExternalSettlementAcquisitionContext,
    ExternalSettlementAcquisitionResult,
    ExternalSettlementEvidenceAcquirer,
)
from .external_submission_commitment import ExternalSubmissionOutcome
from .financial_order_settlement import (
    FinancialSettlementCommitResult,
    FinancialSettlementError,
)
from .resolution_projection import (
    PersistedResolutionAssessment,
    ProjectionStatus,
    assess_persisted_resolution,
)
from .state_store import StateStore


class ExternalSettlementReconciliationStatus(StrEnum):
    """Closed status vocabulary for one external reconciliation pass."""

    NOT_READY = "NOT_READY"
    EVIDENCE_PERSISTENCE_BLOCKED = "EVIDENCE_PERSISTENCE_BLOCKED"
    APPLICATION_BLOCKED = "APPLICATION_BLOCKED"
    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"


@dataclass(frozen=True)
class ExternalSettlementReconciliationResult:
    """Immutable report of one bounded external reconciliation invocation.

    ``finalized`` is intentionally always false in this layer.  Finalization
    is a separate CAS contract and must not be smuggled into settlement
    application.
    """

    local_order_id: int
    status: ExternalSettlementReconciliationStatus | str
    acquisition_performed: bool
    evidence_persisted: bool
    resolution: PersistedResolutionAssessment
    settlement_key: str | None
    settlement_complete: bool
    financial_application_key: str | None
    applied: bool
    already_applied: bool
    finalized: bool
    manual_reconciliation_required: bool
    blocking_reason: str | None = None
    application: FinancialSettlementCommitResult | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise ValueError("local_order_id must be a positive integer")
        object.__setattr__(self, "status", ExternalSettlementReconciliationStatus(self.status))
        if not isinstance(self.resolution, PersistedResolutionAssessment):
            raise TypeError("resolution must be a PersistedResolutionAssessment")
        for field_name in (
            "acquisition_performed",
            "evidence_persisted",
            "settlement_complete",
            "applied",
            "already_applied",
            "finalized",
            "manual_reconciliation_required",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be boolean")
        if self.applied and self.already_applied:
            raise ValueError("applied and already_applied are mutually exclusive")
        if self.finalized:
            raise ValueError("external settlement coordinator cannot finalize orders")
        for field_name in ("settlement_key", "financial_application_key", "blocking_reason"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} must be None or a non-empty string")
        if self.application is not None and not isinstance(
            self.application, FinancialSettlementCommitResult
        ):
            raise TypeError("application must be a FinancialSettlementCommitResult")


class ExternalSettlementCoordinator:
    """Coordinate acquisition, durable evidence, assessment and application.

    This class is intentionally not wired into ``runner`` or ``recovery``.
    It is an injectable external path for a later, separately reviewed
    runtime integration.  The acquirer is the only component allowed to
    perform network reads; the coordinator itself never calls a broker.
    """

    def __init__(self, store: StateStore) -> None:
        if not isinstance(store, StateStore):
            raise TypeError("store must be a StateStore")
        self._store = store

    @staticmethod
    def _commitment_is_durable(
        context: ExternalSettlementAcquisitionContext,
        responses: object,
    ) -> bool:
        commitment = context.submission_commitment
        if commitment is None or not isinstance(responses, list):
            return False
        return any(
            response.outcome == ExternalSubmissionOutcome.FILLED_COMMITMENT
            and response.commitment == commitment
            for response in responses
        )

    @staticmethod
    def _settlement_matches_context(
        result: ExternalSettlementAcquisitionResult,
    ) -> bool:
        settlement = result.settlement
        context = result.context
        if settlement is None or not settlement.completeness.is_complete:
            return False
        fixed_fields = (
            (settlement.local_order_id, context.local_order_id),
            (settlement.intent_id, context.intent_id),
            (settlement.venue, context.venue),
            (settlement.environment, context.environment),
            (settlement.account_scope, context.account_scope),
            (settlement.instrument, context.instrument),
            (settlement.side, context.side),
            (settlement.client_order_id, context.client_order_id),
            (settlement.external_order_id, context.external_order_id),
        )
        return all(left == right for left, right in fixed_fields[:-1]) and (
            context.external_order_id is None or fixed_fields[-1][0] == fixed_fields[-1][1]
        )

    def _report(
        self,
        *,
        acquisition: ExternalSettlementAcquisitionResult,
        status: ExternalSettlementReconciliationStatus,
        evidence_persisted: bool,
        resolution: PersistedResolutionAssessment,
        blocking_reason: str | None,
        application: FinancialSettlementCommitResult | None = None,
    ) -> ExternalSettlementReconciliationResult:
        settlement = acquisition.settlement
        settlement_key = settlement.settlement_key if settlement is not None else None
        settlement_complete = bool(settlement is not None and settlement.completeness.is_complete)
        application_key = application.application.application_key if application else None
        return ExternalSettlementReconciliationResult(
            local_order_id=acquisition.context.local_order_id,
            status=status,
            acquisition_performed=acquisition.acquisition_performed,
            evidence_persisted=evidence_persisted,
            resolution=resolution,
            settlement_key=settlement_key,
            settlement_complete=settlement_complete,
            financial_application_key=application_key,
            applied=bool(application and application.applied),
            already_applied=bool(application and application.already_applied),
            finalized=False,
            manual_reconciliation_required=status
            not in {
                ExternalSettlementReconciliationStatus.APPLIED,
                ExternalSettlementReconciliationStatus.ALREADY_APPLIED,
            },
            blocking_reason=blocking_reason,
            application=application,
        )

    def reconcile(
        self,
        context: ExternalSettlementAcquisitionContext,
        acquirer: ExternalSettlementEvidenceAcquirer,
        *,
        observed_at: str | None = None,
    ) -> ExternalSettlementReconciliationResult:
        """Run exactly one finite external settlement pass.

        The method persists every returned order/fill lookup, then persists a
        complete settlement if one was built.  It only calls the existing
        atomic settlement writer after the durable submission commitment and
        the durable projection are both valid.  It never changes order
        terminality and never authorizes another submission.
        """

        if not isinstance(context, ExternalSettlementAcquisitionContext):
            raise TypeError("context must be an ExternalSettlementAcquisitionContext")
        acquire = getattr(acquirer, "acquire", None)
        if not callable(acquire):
            raise TypeError("acquirer must provide acquire(context, observed_at=...)")
        acquisition = acquire(context, observed_at=observed_at)
        if not isinstance(acquisition, ExternalSettlementAcquisitionResult):
            raise TypeError("acquirer returned an invalid acquisition result")

        evidence_persisted = False
        try:
            ExternalEvidencePersistence.persist(self._store, acquisition.order_lookup)
            for lookup in acquisition.fill_lookups:
                ExternalFillEvidencePersistence.persist(self._store, lookup)
            if acquisition.settlement is not None:
                self._store.persist_external_order_settlement(
                    acquisition.settlement,
                    engine=context.engine,
                    observed_at=observed_at,
                )
            evidence_persisted = True
        except (FinancialSettlementError, TypeError, ValueError, RuntimeError) as error:
            resolution = assess_persisted_resolution(self._store, context.local_order_id)
            return self._report(
                acquisition=acquisition,
                status=ExternalSettlementReconciliationStatus.EVIDENCE_PERSISTENCE_BLOCKED,
                evidence_persisted=False,
                resolution=resolution,
                blocking_reason=str(error),
            )

        resolution = assess_persisted_resolution(self._store, context.local_order_id)
        if resolution.projection.status != ProjectionStatus.READY:
            return self._report(
                acquisition=acquisition,
                status=ExternalSettlementReconciliationStatus.NOT_READY,
                evidence_persisted=evidence_persisted,
                resolution=resolution,
                blocking_reason=acquisition.blocking_reason or "RESOLUTION_PROJECTION_NOT_READY",
            )
        assert resolution.assessment is not None
        if not resolution.assessment.binding_complete or resolution.assessment.conflicts:
            return self._report(
                acquisition=acquisition,
                status=ExternalSettlementReconciliationStatus.NOT_READY,
                evidence_persisted=evidence_persisted,
                resolution=resolution,
                blocking_reason="RESOLUTION_EVIDENCE_CONFLICT",
            )
        if not self._settlement_matches_context(acquisition):
            return self._report(
                acquisition=acquisition,
                status=ExternalSettlementReconciliationStatus.NOT_READY,
                evidence_persisted=evidence_persisted,
                resolution=resolution,
                blocking_reason=acquisition.blocking_reason or "SETTLEMENT_COMPLETENESS_NOT_PROVEN",
            )

        try:
            responses = self._store.read_external_submission_responses(
                context.intent_id, engine=context.engine
            )
        except (TypeError, ValueError, RuntimeError) as error:
            return self._report(
                acquisition=acquisition,
                status=ExternalSettlementReconciliationStatus.APPLICATION_BLOCKED,
                evidence_persisted=evidence_persisted,
                resolution=resolution,
                blocking_reason=str(error),
            )
        if not self._commitment_is_durable(context, responses):
            return self._report(
                acquisition=acquisition,
                status=ExternalSettlementReconciliationStatus.APPLICATION_BLOCKED,
                evidence_persisted=evidence_persisted,
                resolution=resolution,
                blocking_reason="SUBMISSION_FILL_COMMITMENT_NOT_DURABLE",
            )

        settlement = acquisition.settlement
        assert settlement is not None
        try:
            application = self._store.apply_external_settlement_atomically(
                local_order_id=context.local_order_id,
                settlement_key=settlement.settlement_key,
            )
        except (FinancialSettlementError, TypeError, ValueError, RuntimeError) as error:
            return self._report(
                acquisition=acquisition,
                status=ExternalSettlementReconciliationStatus.APPLICATION_BLOCKED,
                evidence_persisted=evidence_persisted,
                resolution=resolution,
                blocking_reason=str(error),
            )
        status = (
            ExternalSettlementReconciliationStatus.ALREADY_APPLIED
            if application.already_applied
            else ExternalSettlementReconciliationStatus.APPLIED
        )
        return self._report(
            acquisition=acquisition,
            status=status,
            evidence_persisted=evidence_persisted,
            resolution=resolution,
            blocking_reason=None,
            application=application,
        )


__all__ = [
    "ExternalSettlementCoordinator",
    "ExternalSettlementReconciliationResult",
    "ExternalSettlementReconciliationStatus",
]
