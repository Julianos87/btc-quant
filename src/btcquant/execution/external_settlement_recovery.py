"""Startup recovery for the bounded external settlement path.

This module is intentionally injectable and not wired into the runner.  It
reuses the qualified acquisition/coordinator/finalization boundaries and has
no submission or cancellation capability.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .external_settlement_acquisition import (
    ExternalSettlementAcquisitionContext,
    ExternalSettlementEvidenceAcquirer,
)
from .external_settlement_coordinator import (
    ExternalSettlementCoordinator,
    ExternalSettlementReconciliationStatus,
)
from .external_settlement_finalization import ExternalSettlementFinalizer
from .external_submission_commitment import (
    AuthoritativeSubmissionFillCommitment,
    ExternalSubmissionResponse,
    ExternalSubmissionOutcome,
)
from .financial_order_settlement import FinancialSettlementError
from .order_state import LocalOrderState
from .state_store import StateStore


ContextFactory = Callable[
    [Mapping[str, object], AuthoritativeSubmissionFillCommitment],
    ExternalSettlementAcquisitionContext,
]


@dataclass(frozen=True)
class ExternalSettlementStartupRecoveryReport:
    """Immutable report of one startup scan; no field authorizes a retry."""

    inspected_order_ids: tuple[int, ...]
    finalized_order_ids: tuple[int, ...]
    manual_order_ids: tuple[int, ...]
    blocking_reasons: tuple[tuple[int, str], ...]

    def __post_init__(self) -> None:
        for name in ("inspected_order_ids", "finalized_order_ids", "manual_order_ids"):
            values = tuple(getattr(self, name))
            if tuple(sorted(set(values))) != values or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in values
            ):
                raise ValueError(f"{name} must be sorted, unique positive integers")
            object.__setattr__(self, name, values)
        reasons = tuple(self.blocking_reasons)
        if tuple(sorted(reasons)) != reasons or any(
            not isinstance(order_id, int)
            or order_id <= 0
            or not isinstance(reason, str)
            or not reason.strip()
            for order_id, reason in reasons
        ):
            raise ValueError("blocking_reasons must be sorted and non-empty")
        object.__setattr__(self, "blocking_reasons", reasons)
        if set(self.finalized_order_ids) & set(self.manual_order_ids):
            raise ValueError("an order cannot be finalized and manual simultaneously")

    @property
    def can_start(self) -> bool:
        """False whenever startup recovery found an unresolved external order."""

        return not self.manual_order_ids and not self.blocking_reasons


class ExternalSettlementStartupRecovery:
    """Recover external orders without ever submitting a replacement order."""

    def __init__(
        self,
        store: StateStore,
        *,
        coordinator: ExternalSettlementCoordinator | None = None,
        finalizer: ExternalSettlementFinalizer | None = None,
    ) -> None:
        if not isinstance(store, StateStore):
            raise TypeError("store must be a StateStore")
        self._store = store
        self._coordinator = coordinator or ExternalSettlementCoordinator(store)
        self._finalizer = finalizer or ExternalSettlementFinalizer(store)

    @staticmethod
    def _commitment(
        responses: Sequence[ExternalSubmissionResponse],
    ) -> AuthoritativeSubmissionFillCommitment | None:
        commitments = [
            response.commitment
            for response in responses
            if getattr(response, "outcome", None) == ExternalSubmissionOutcome.FILLED_COMMITMENT
            and isinstance(
                getattr(response, "commitment", None), AuthoritativeSubmissionFillCommitment
            )
        ]
        if not commitments:
            return None
        first = commitments[0]
        return first if all(item == first for item in commitments[1:]) else None

    def recover(
        self,
        engine: str,
        *,
        context_factory: ContextFactory,
        acquirer: ExternalSettlementEvidenceAcquirer,
        observed_at: str | None = None,
    ) -> ExternalSettlementStartupRecoveryReport:
        """Scan modern external orders and recover only durable settlements.

        ``context_factory`` supplies durable window policy from the caller;
        it cannot submit an order.  Missing plans/commitments, acquisition
        ambiguity and finalization failures remain manual reconciliation.
        """

        if not isinstance(engine, str) or not engine.strip():
            raise ValueError("engine must be non-empty")
        if not callable(context_factory):
            raise TypeError("context_factory must be callable")
        if not callable(getattr(acquirer, "acquire", None)):
            raise TypeError("acquirer must provide acquire")

        inspected: list[int] = []
        finalized: list[int] = []
        manual: list[int] = []
        reasons: list[tuple[int, str]] = []
        for order in self._store.unresolved_orders(engine):
            if order.get("order_type") != "MARKET":
                continue
            order_id = int(order["id"])
            if order.get("local_state") == LocalOrderState.INTENT_CREATED.value:
                # This is the existing local proof that broker submission could
                # not have started; the generic local recovery owns it.
                continue
            inspected.append(order_id)
            try:
                plan = self._store.get_financial_application_plan(order_id)
                if plan is None:
                    raise FinancialSettlementError("LEGACY_APPLICATION_CONTEXT_INCOMPLETE")
                responses = self._store.read_external_submission_responses(
                    str(order["intent_id"]), engine=engine
                )
                commitment = self._commitment(responses)
                if commitment is None:
                    raise FinancialSettlementError(
                        "MANUAL_RECONCILIATION_REQUIRED_MISSING_SUBMISSION_COMMITMENT"
                    )
                context = context_factory(order, commitment)
                if context.local_order_id != order_id or context.intent_id != str(
                    order["intent_id"]
                ):
                    raise FinancialSettlementError("STARTUP_RECOVERY_CONTEXT_BINDING_CONFLICT")
                reconciliation = self._coordinator.reconcile(
                    context, acquirer, observed_at=observed_at
                )
                if (
                    reconciliation.status
                    not in {
                        ExternalSettlementReconciliationStatus.APPLIED,
                        ExternalSettlementReconciliationStatus.ALREADY_APPLIED,
                    }
                    or reconciliation.settlement_key is None
                ):
                    raise FinancialSettlementError(
                        reconciliation.blocking_reason or "EXTERNAL_SETTLEMENT_NOT_READY"
                    )
                self._finalizer.finalize(
                    order_id,
                    settlement_key=reconciliation.settlement_key,
                )
                finalized.append(order_id)
            except (FinancialSettlementError, TypeError, ValueError, RuntimeError) as error:
                manual.append(order_id)
                reasons.append((order_id, str(error) or type(error).__name__))
        return ExternalSettlementStartupRecoveryReport(
            tuple(sorted(inspected)),
            tuple(sorted(finalized)),
            tuple(sorted(manual)),
            tuple(sorted(reasons)),
        )


__all__ = ["ExternalSettlementStartupRecovery", "ExternalSettlementStartupRecoveryReport"]
