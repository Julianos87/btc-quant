"""Frontière transactionnelle entre intention locale et appel broker."""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Mapping
from typing import Any

from .broker import Broker, BrokerOrderResult, Fill
from .errors import FinancialTransitionAlreadyReserved, ReconciliationRequired
from .external_submission_commitment import build_submission_response
from .financial_application_plan import FinancialApplicationPlan
from .order_state import (
    ExternalOrderState,
    FinancialTransitionType,
    LogicalOrderIdentity,
)
from .state_store import OrderReservation, StateStore, utc_now
from .safe_retry import decide_safe_retry


@dataclass(frozen=True)
class SubmittedOrder:
    fill: Fill
    order_id: int
    intent_id: str
    logical_order_key: str
    status: str
    external_state: ExternalOrderState
    remaining_qty: float
    is_terminal: bool
    transition_sequence: int
    application_plan: FinancialApplicationPlan
    #: Réponse broker brute normalisée, conservée uniquement pour permettre
    #: au chemin PAPER de la transformer en preuve durable. Elle ne constitue
    #: jamais une autorité comptable en mémoire.
    broker_result: BrokerOrderResult | None = None


@dataclass(frozen=True, slots=True)
class SubmitMarketCommand:
    """Immutable internal contract for one durable MARKET submission.

    The command deliberately carries the complete business intent.  It keeps
    coupled order, identity and financial-plan fields together so callers do
    not pass a long sequence of independently named primitives.
    """

    engine: str
    slot: str
    side: str
    qty: float
    reference_price: float
    reason: str
    decision_checkpoint: str
    transition_type: FinancialTransitionType | str
    position_generation: str | None = None
    transition_sequence: int = 0
    reduce_only: bool = False
    available_volume: float | None = None
    volatility_annual: float | None = None
    application_plan: FinancialApplicationPlan | None = None

    def validate(
        self,
    ) -> tuple[str, FinancialTransitionType, str, float, float]:
        """Validate the command without mutating it or performing I/O."""

        if not isinstance(self.side, str):
            raise ValueError(f"Côté d'ordre invalide : {self.side!r}")
        normalized_side = self.side.strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError(f"Côté d'ordre invalide : {self.side!r}")
        normalized_transition = FinancialTransitionType(self.transition_type)
        if self.application_plan is None:
            raise ValueError("Un plan financier durable est obligatoire avant soumission MARKET")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason doit être non vide")
        normalized_reason = self.reason.strip()
        if not isinstance(self.reduce_only, bool):
            raise ValueError("reduce_only doit être bool")
        try:
            normalized_qty = float(self.qty)
            normalized_reference_price = float(self.reference_price)
        except (TypeError, ValueError) as error:
            raise ValueError("qty et reference_price doivent être numériques") from error
        if (
            isinstance(self.qty, bool)
            or not math.isfinite(normalized_qty)
            or not math.isclose(
                self.application_plan.requested_qty,
                normalized_qty,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("Le plan financier ne correspond pas à la quantité soumise")
        if (
            isinstance(self.reference_price, bool)
            or not math.isfinite(normalized_reference_price)
            or not math.isclose(
                self.application_plan.reference_price,
                normalized_reference_price,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("Le plan financier ne correspond pas au prix de référence soumis")
        if self.application_plan.side != normalized_side:
            raise ValueError("Le plan financier ne correspond pas au côté soumis")
        if self.application_plan.reason != normalized_reason:
            raise ValueError("Le plan financier ne correspond pas à la raison soumise")
        if self.application_plan.reduce_only != self.reduce_only:
            raise ValueError("Le plan financier ne correspond pas à reduce_only")
        expected_entry_side = {
            FinancialTransitionType.ENTER_LONG: "BUY",
            FinancialTransitionType.ENTER_SHORT: "SELL",
        }.get(normalized_transition)
        if expected_entry_side is not None and normalized_side != expected_entry_side:
            raise ValueError(
                f"{normalized_transition.value} exige le côté {expected_entry_side}, "
                f"pas {normalized_side}"
            )
        return (
            normalized_side,
            normalized_transition,
            normalized_reason,
            normalized_qty,
            normalized_reference_price,
        )


class OrderExecutionService:
    def __init__(
        self,
        store: StateStore,
        broker: Broker,
    ) -> None:
        self.store = store
        self.broker = broker

    @staticmethod
    def _legacy_terminal_status(
        external_state: ExternalOrderState,
    ) -> str:
        if external_state == ExternalOrderState.FILLED:
            return "FILLED"
        if external_state == ExternalOrderState.PARTIAL_TERMINAL:
            return "PARTIAL"
        if external_state == ExternalOrderState.REJECTED:
            return "REJECTED"
        if external_state in (ExternalOrderState.CANCELED, ExternalOrderState.EXPIRED):
            return "CANCELED"
        if external_state in (ExternalOrderState.OPEN, ExternalOrderState.PARTIAL_OPEN):
            return "OPEN"
        return "PENDING"

    @staticmethod
    def _can_start_next_attempt(reservation: OrderReservation) -> bool:
        """Legacy hook retained, but never authorizes retry by aggregates alone."""

        return decide_safe_retry(
            external_execution=True,
            local_state=reservation.local_state,
            external_state=reservation.external_state,
            status=reservation.status,
            filled_qty=reservation.filled_qty,
            remaining_qty=reservation.remaining_qty,
            zero_effect_proven=False,
            proof_source=None,
        ).allowed

    def _persist_external_submission_response(
        self,
        *,
        order_id: int,
        intent_id: str,
        engine: str,
        side: str,
        raw_payload: Mapping[str, Any] | None,
        structured_error: str | None = None,
    ) -> None:
        """Persist the broker response before exposing legacy order state.

        This is deliberately limited to the external path.  The response is
        durable evidence, not an authorization to retry or to apply finances.
        """

        if not self.broker.external_execution:
            return
        venue = str(getattr(self.broker, "exchange_id", "unknown"))
        environment = str(getattr(self.broker, "environment", "unknown"))
        account_scope = str(getattr(self.broker, "account_scope", "unknown"))
        instrument = str(getattr(self.broker, "symbol", "unknown"))
        client_order_id = self.broker.venue_client_order_id(intent_id)
        try:
            response = build_submission_response(
                local_order_id=order_id,
                intent_id=intent_id,
                venue=venue,
                environment=environment,
                account_scope=account_scope,
                instrument=instrument,
                side=side,
                client_order_id=client_order_id,
                raw_payload=raw_payload,
                response_acquired_at=utc_now(),
                ioc_expected=bool(getattr(self.broker, "submission_is_ioc", False)),
                structured_error=structured_error,
            )
            self.store.append_external_submission_response(response, engine=engine)
        except Exception as error:
            raise ReconciliationRequired(
                f"Ordre {order_id}: réponse de soumission externe non durable; arrêt fail-closed"
            ) from error

    def _last_external_submission_response(self) -> Mapping[str, Any] | None:
        """Read the exact response captured by a broker before it raised.

        A structured exchange rejection may be raised by CCXT rather than
        returned as a broker result. Brokers that support this evidence
        boundary expose a read-only accessor; absent evidence remains
        ambiguous and is persisted as such.
        """

        accessor = getattr(self.broker, "last_submission_response", None)
        if not callable(accessor):
            return None
        response = accessor()
        return response if isinstance(response, Mapping) else None

    def submit_market(self, command: SubmitMarketCommand) -> SubmittedOrder:
        """Submit one validated immutable MARKET command."""

        (
            normalized_side,
            normalized_transition,
            _normalized_reason,
            _normalized_qty,
            _normalized_reference_price,
        ) = command.validate()
        application_plan = command.application_plan
        assert application_plan is not None
        attempt_sequence = command.transition_sequence
        while True:
            identity = LogicalOrderIdentity(
                engine=command.engine,
                slot=command.slot,
                decision_checkpoint=command.decision_checkpoint,
                transition_type=normalized_transition,
                position_generation=command.position_generation,
                transition_sequence=attempt_sequence,
            )
            if application_plan.identity != identity:
                raise ValueError("Le plan financier ne correspond pas à l'identité de soumission")
            reservation = self.store.reserve_market_order_with_application_plan(
                identity, plan=application_plan
            )
            if reservation.acquired:
                self.store.mark_order_submitting(reservation.order_id)
                break
            if reservation.status in {"RECOVERED_ABORTED", "FAILED"} and (
                self.store.reclaim_safe_market_order(
                    reservation.order_id,
                    allow_local_failure=(
                        reservation.status == "FAILED" and not self.broker.external_execution
                    ),
                )
            ):
                break
            if self._can_start_next_attempt(reservation):
                # Ne jamais réutiliser le client_order_id d'un ordre que
                # l'exchange connaît déjà, même explicitement terminal.
                attempt_sequence += 1
                continue
            raise FinancialTransitionAlreadyReserved(
                reservation.logical_order_key,
                reservation.order_id,
                reservation.local_state,
                reservation.external_state,
            )
        order_id = reservation.order_id
        intent_id = reservation.intent_id
        try:
            result = self.broker.execute_market(
                application_plan.side,
                application_plan.requested_qty,
                application_plan.reference_price,
                client_order_id=intent_id,
                reduce_only=application_plan.reduce_only,
                available_volume=command.available_volume,
                volatility_annual=command.volatility_annual,
            )
        except Exception as error:
            ambiguous = self.broker.external_execution
            suffix = " (résultat externe ambigu, réconciliation requise)" if ambiguous else ""
            self._persist_external_submission_response(
                order_id=order_id,
                intent_id=intent_id,
                engine=command.engine,
                side=normalized_side,
                raw_payload=self._last_external_submission_response(),
                structured_error=f"{type(error).__name__}: {error}",
            )
            try:
                self.store.record_submission_error(
                    order_id,
                    error=f"{type(error).__name__}: {error}{suffix}",
                    ambiguous=ambiguous,
                )
            except Exception as persistence_error:
                raise ReconciliationRequired(
                    f"Ordre {order_id}: échec broker puis impossibilité de persister "
                    "son état; arrêt fail-closed"
                ) from persistence_error
            if ambiguous:
                raise ReconciliationRequired(
                    f"Ordre {order_id}: résultat externe ambigu après "
                    f"{type(error).__name__}; réconciliation requise"
                ) from error
            raise
        fill = result.fill
        self._persist_external_submission_response(
            order_id=order_id,
            intent_id=intent_id,
            engine=command.engine,
            side=normalized_side,
            raw_payload=result.raw_response,
        )
        try:
            self.store.record_order_observation(
                order_id,
                external_state=result.status,
                filled_qty=fill.qty,
                remaining_qty=result.remaining_qty,
                price=fill.price,
                fee=fill.fee,
                broker_order_id=fill.broker_order_id,
            )
        except Exception as error:
            # L'appel broker est terminé mais sa réponse n'est pas durable. Le
            # processus ne doit traiter aucune autre transition avant reprise
            # par client_order_id, même si l'état était terminal en mémoire.
            raise ReconciliationRequired(
                f"Ordre {order_id}: réponse broker non persistée; arrêt fail-closed"
            ) from error
        return SubmittedOrder(
            fill=fill,
            order_id=order_id,
            intent_id=intent_id,
            logical_order_key=reservation.logical_order_key,
            status=self._legacy_terminal_status(result.status),
            external_state=result.status,
            remaining_qty=result.remaining_qty,
            is_terminal=result.is_terminal,
            transition_sequence=attempt_sequence,
            application_plan=application_plan,
            broker_result=result,
        )
