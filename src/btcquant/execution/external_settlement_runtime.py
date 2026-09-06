"""Runtime adapter for the qualified external IOC settlement path.

This module is the only runner-facing bridge to the already qualified
acquisition, persistence, settlement application and finalization contracts.
It is constructed only for the explicitly qualified Hyperliquid testnet
profile.  It never submits or cancels an order and it never authorizes a
retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from collections.abc import Mapping, Sequence
from typing import Any

from .broker import Broker
from .ccxt_broker import CcxtBroker
from .external_evidence_reader import CcxtExternalEvidenceReader
from .external_fill_evidence_reader import CcxtExternalFillEvidenceReader
from .external_settlement_acquisition import (
    CcxtExternalSettlementAcquirer,
    ExternalSettlementAcquisitionContext,
    ExternalSettlementEvidenceAcquirer,
)
from .external_settlement_coordinator import (
    ExternalSettlementCoordinator,
    ExternalSettlementReconciliationResult,
    ExternalSettlementReconciliationStatus,
)
from .external_settlement_finalization import (
    ExternalSettlementFinalizationResult,
    ExternalSettlementFinalizer,
    ExternalZeroEffectFinalizationResult,
)
from .external_settlement_recovery import (
    ExternalSettlementStartupRecovery,
    ExternalSettlementStartupRecoveryReport,
)
from .external_submission_commitment import (
    AuthoritativeSubmissionFillCommitment,
    ExternalSubmissionOutcome,
    ExternalSubmissionResponse,
)
from .errors import ReconciliationRequired
from .state_store import StateStore


WINDOW_SAFETY_MARGIN = timedelta(minutes=5)
WINDOW_END_SAFETY_MARGIN = timedelta(seconds=30)


@dataclass(frozen=True)
class ExternalSettlementRuntimeResult:
    """Immutable result after settlement application and finalization."""

    reconciliation: ExternalSettlementReconciliationResult
    finalization: ExternalSettlementFinalizationResult

    @property
    def local_order_id(self) -> int:
        return self.reconciliation.local_order_id


@dataclass(frozen=True)
class ExternalZeroEffectRuntimeResult:
    """Runtime result for a durable deterministic IOC no-effect outcome."""

    finalization: ExternalZeroEffectFinalizationResult

    @property
    def local_order_id(self) -> int:
        return self.finalization.local_order_id


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReconciliationRequired(f"{field} absent du contexte durable")
    candidate = value.strip()
    if candidate[-1:] in {"Z", "z"}:
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ReconciliationRequired(f"{field} invalide") from error
    if parsed.tzinfo is None:
        raise ReconciliationRequired(f"{field} sans fuseau explicite")
    return parsed.astimezone(UTC)


def _commitment(
    responses: Sequence[ExternalSubmissionResponse],
) -> AuthoritativeSubmissionFillCommitment | None:
    commitments = [
        response.commitment
        for response in responses
        if getattr(response, "outcome", None) == ExternalSubmissionOutcome.FILLED_COMMITMENT
        and isinstance(getattr(response, "commitment", None), AuthoritativeSubmissionFillCommitment)
    ]
    if not commitments:
        return None
    first = commitments[0]
    return first if all(item == first for item in commitments[1:]) else None


class ExternalSettlementRuntime:
    """Bind the external coordinator to one qualified testnet broker.

    The adapter owns only orchestration.  All network reads are delegated to
    the injected read-only acquirer, all writes go through the qualified
    coordinator/finalizer, and no broker submission method is reachable from
    this class.
    """

    def __init__(
        self,
        store: StateStore,
        broker: Broker,
        clock: Any,
        *,
        acquirer: ExternalSettlementEvidenceAcquirer | None = None,
        coordinator: ExternalSettlementCoordinator | None = None,
        finalizer: ExternalSettlementFinalizer | None = None,
    ) -> None:
        if not isinstance(store, StateStore):
            raise TypeError("store must be a StateStore")
        if not isinstance(broker, Broker):
            raise TypeError("broker must be a Broker")
        if not callable(getattr(clock, "utc_now", None)):
            raise TypeError("clock must provide utc_now()")
        self.store = store
        self.broker = broker
        self.clock = clock
        self.coordinator = coordinator or ExternalSettlementCoordinator(store)
        self.finalizer = finalizer or ExternalSettlementFinalizer(store)
        if acquirer is None:
            if not isinstance(broker, CcxtBroker):
                raise TypeError("a qualified CCXT broker is required without an injected acquirer")
            exchange_id = str(getattr(broker, "exchange_id", ""))
            acquirer = CcxtExternalSettlementAcquirer(
                CcxtExternalEvidenceReader(broker.exchange, exchange_id=exchange_id),
                CcxtExternalFillEvidenceReader(broker.exchange),
            )
        if not callable(getattr(acquirer, "acquire", None)):
            raise TypeError("acquirer must provide acquire")
        self.acquirer = acquirer

    @staticmethod
    def is_qualified_broker(broker: Broker) -> bool:
        """Return true only for the explicitly qualified external profile."""

        return (
            isinstance(broker, CcxtBroker)
            and broker.external_execution
            and getattr(broker, "exchange_id", None) == "hyperliquid"
            and getattr(broker, "environment", None) == "testnet"
            and getattr(broker, "market_kind", None) == "perp"
        )

    def _order(self, order_id: int) -> dict[str, Any]:
        plan = self.store.get_financial_application_plan(order_id)
        if plan is None:
            raise ReconciliationRequired("LEGACY_APPLICATION_CONTEXT_INCOMPLETE")
        order = self.store.read_order_by_intent(plan.intent_id)
        if order is None or int(order["id"]) != order_id:
            raise ReconciliationRequired("EXTERNAL_RUNTIME_ORDER_BINDING_CONFLICT")
        return order

    def _context_factory(
        self,
        order: Mapping[str, object],
        commitment: AuthoritativeSubmissionFillCommitment,
    ) -> ExternalSettlementAcquisitionContext:
        order_id = int(str(order["id"]))
        persisted_plan = self.store.get_financial_application_plan(order_id)
        if persisted_plan is None:
            raise ReconciliationRequired("LEGACY_APPLICATION_CONTEXT_INCOMPLETE")
        plan = persisted_plan.plan
        if str(order["engine"]) != plan.identity.engine or str(order["slot"]) != plan.identity.slot:
            raise ReconciliationRequired("EXTERNAL_RUNTIME_ORDER_BINDING_CONFLICT")
        if (
            commitment.local_order_id != order_id
            or commitment.intent_id != persisted_plan.intent_id
        ):
            raise ReconciliationRequired("SUBMISSION_COMMITMENT_BINDING_CONFLICT")
        broker_account = str(getattr(self.broker, "account_scope", ""))
        broker_instrument = str(getattr(self.broker, "symbol", ""))
        if commitment.account_scope != broker_account:
            raise ReconciliationRequired("EXTERNAL_RUNTIME_ACCOUNT_BINDING_CONFLICT")
        if commitment.instrument != broker_instrument:
            raise ReconciliationRequired("EXTERNAL_RUNTIME_INSTRUMENT_BINDING_CONFLICT")
        planned = _parse_timestamp(plan.planned_effect_at, "planned_effect_at")
        acquired = _parse_timestamp(commitment.response_acquired_at, "response_acquired_at")
        updated = _parse_timestamp(order.get("updated_at"), "order.updated_at")
        now = self.clock.utc_now()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ReconciliationRequired("clock.utc_now() doit être timezone-aware")
        now = now.astimezone(UTC)
        window_start = planned - WINDOW_SAFETY_MARGIN
        window_end = max(acquired, updated, now) + WINDOW_END_SAFETY_MARGIN
        return ExternalSettlementAcquisitionContext(
            local_order_id=order_id,
            intent_id=persisted_plan.intent_id,
            venue=commitment.venue,
            environment=commitment.environment,
            account_scope=commitment.account_scope,
            instrument=commitment.instrument,
            side=plan.side,
            engine=plan.identity.engine,
            client_order_id=commitment.client_order_id,
            requested_qty=plan.requested_qty,
            planned_effect_at=plan.planned_effect_at,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            submission_commitment=commitment,
            external_order_id=commitment.external_order_id,
        )

    def _context_for_order(self, order_id: int) -> ExternalSettlementAcquisitionContext:
        order = self._order(order_id)
        responses = self.store.read_external_submission_responses(
            str(order["intent_id"]), engine=str(order["engine"])
        )
        commitment = _commitment(responses)
        if commitment is None:
            raise ReconciliationRequired(
                "MANUAL_RECONCILIATION_REQUIRED_MISSING_SUBMISSION_COMMITMENT"
            )
        return self._context_factory(order, commitment)

    def reconcile_order(
        self,
        order_id: int,
        *,
        observed_at: str | None = None,
    ) -> ExternalSettlementRuntimeResult | ExternalZeroEffectRuntimeResult:
        """Perform one bounded read/persist/assess/apply/finalize pass."""

        order = self._order(order_id)
        responses = self.store.read_external_submission_responses(
            str(order["intent_id"]), engine=str(order["engine"])
        )
        zero_responses = [
            response
            for response in responses
            if response.outcome == ExternalSubmissionOutcome.DETERMINISTIC_IOC_NO_MATCH
        ]
        if zero_responses:
            if len(responses) != 1 or len(zero_responses) != 1:
                raise ReconciliationRequired("EXTERNAL_ZERO_EFFECT_RESPONSE_CONFLICT")
            submission_key = zero_responses[0].submission_key
            if submission_key is None:
                raise ReconciliationRequired("EXTERNAL_ZERO_EFFECT_RESPONSE_CONFLICT")
            zero_finalization = self.finalizer.finalize_zero_effect(
                order_id,
                submission_key=submission_key,
            )
            return ExternalZeroEffectRuntimeResult(zero_finalization)

        context = self._context_for_order(order_id)
        reconciliation = self.coordinator.reconcile(context, self.acquirer, observed_at=observed_at)
        if (
            reconciliation.status
            not in {
                ExternalSettlementReconciliationStatus.APPLIED,
                ExternalSettlementReconciliationStatus.ALREADY_APPLIED,
            }
            or reconciliation.settlement_key is None
        ):
            raise ReconciliationRequired(
                reconciliation.blocking_reason or "EXTERNAL_SETTLEMENT_NOT_READY"
            )
        finalization = self.finalizer.finalize(
            order_id,
            settlement_key=reconciliation.settlement_key,
        )
        return ExternalSettlementRuntimeResult(reconciliation, finalization)

    def recover_startup(
        self,
        *,
        observed_at: str | None = None,
    ) -> ExternalSettlementStartupRecoveryReport:
        """Recover pending external orders without a submission call."""

        return ExternalSettlementStartupRecovery(self.store).recover(
            "trend",
            context_factory=self._context_factory,
            acquirer=self.acquirer,
            observed_at=observed_at,
        )


__all__ = [
    "ExternalSettlementRuntime",
    "ExternalSettlementRuntimeResult",
    "ExternalZeroEffectRuntimeResult",
]
