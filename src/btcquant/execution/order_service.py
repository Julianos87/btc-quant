"""Frontière transactionnelle entre intention locale et appel broker."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .broker import Broker, Fill
from .errors import AmbiguousOrder, NonTerminalOrder, ReconciliationRequired
from .state_store import StateStore


@dataclass(frozen=True)
class SubmittedOrder:
    fill: Fill
    order_id: int
    intent_id: str
    status: str
    transition: dict[str, object] | None = None


class OrderExecutionService:
    def __init__(
        self,
        store: StateStore,
        broker: Broker,
        *,
        intent_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.broker = broker
        self.intent_factory = intent_factory or (lambda: uuid.uuid4().hex)

    def submit_market(
        self,
        *,
        engine: str,
        slot: str,
        side: str,
        qty: float,
        reference_price: float,
        reason: str,
        reduce_only: bool = False,
        available_volume: float | None = None,
        volatility_annual: float | None = None,
        intent_id: str | None = None,
        state: Mapping[str, Any] | None = None,
        transition: Mapping[str, Any] | None = None,
    ) -> SubmittedOrder:
        """Réserve d'abord, appelle le broker ensuite.

        Une transition trend fournit son état de checkpoint afin que la
        réservation de l'intention et la persistance de active_transition
        soient atomiques. Les appels génériques sans transition conservent
        l'API historique, mais bénéficient eux aussi de la réservation
        UNIQUE(intent_id).
        """

        requested_intent_id = intent_id or f"{engine}-{slot}-{self.intent_factory()}"
        canonical_qty = self.broker.normalize_market_quantity(
            qty,
            reference_price,
            reduce_only=reduce_only,
        )

        checkpoint_state: Mapping[str, Any] | None = state
        effective_transition: dict[str, Any] | None = None
        order_id: int
        created: bool
        existing: dict[str, Any] | None
        if transition is not None:
            if state is None:
                raise ValueError("Une transition active exige un checkpoint d'état")
            (
                order_id,
                created,
                existing,
                persisted_state,
                persisted_transition,
            ) = self.store.reserve_order_and_transition(
                engine,
                slot,
                transition,
                "MARKET",
                side,
                canonical_qty,
                reason,
                state,
                reference_price=reference_price,
            )
            checkpoint_state = persisted_state
            effective_transition = persisted_transition
            actual_intent_id = str(persisted_transition["intent_id"])
        else:
            order_id, created, existing = self.store.reserve_order(
                engine,
                slot,
                requested_intent_id,
                "MARKET",
                side,
                canonical_qty,
                reason,
                reference_price=reference_price,
            )
            actual_intent_id = requested_intent_id

        if not created:
            status = str(existing["status"]) if existing is not None else "UNKNOWN"
            message = (
                f"Intention {actual_intent_id} déjà active ({status}) : "
                "réconciliation requise, aucun nouvel ordre ne sera envoyé"
            )
            if status in {"PENDING", "OPEN", "UNBALANCED"}:
                self.store.mark_order_ambiguous_and_checkpoint(
                    order_id,
                    engine=engine,
                    state=checkpoint_state,
                    error=message,
                )
            else:
                self.store.record_incident(
                    f"execution:{engine}:intent_reuse:{actual_intent_id}",
                    engine=engine,
                    severity="CRITICAL",
                    kind="intent_reuse",
                    message=message,
                    context={"intent_id": actual_intent_id, "status": status},
                )
            raise ReconciliationRequired(message)

        try:
            fill = self.broker.execute_market(
                side,
                canonical_qty,
                reference_price,
                client_order_id=actual_intent_id,
                reduce_only=reduce_only,
                available_volume=available_volume,
                volatility_annual=volatility_annual,
            )
        except Exception as error:
            message = (
                f"Résultat externe ambigu pour l'intention {actual_intent_id} : "
                f"{type(error).__name__}: {error}"
            )
            if not getattr(self.broker, "is_paper", False):
                self.store.mark_order_ambiguous_and_checkpoint(
                    order_id,
                    engine=engine,
                    state=checkpoint_state,
                    error=message,
                )
                raise AmbiguousOrder(message) from error
            if transition is not None:
                self.store.complete_order_and_clear_transition(
                    order_id,
                    status="FAILED",
                    error=f"{type(error).__name__}: {error}",
                )
            else:
                self.store.complete_order(
                    order_id,
                    status="FAILED",
                    error=f"{type(error).__name__}: {error}",
                )
            raise

        raw_status = getattr(fill, "status", None)
        normalized_status = str(raw_status).upper() if raw_status is not None else None
        external_status_missing = normalized_status is None and not getattr(
            self.broker, "is_paper", False
        )
        if (
            external_status_missing
            or normalized_status in {"OPEN", "PENDING", "UNBALANCED", "UNKNOWN"}
            or (
                not getattr(self.broker, "is_paper", False)
                and normalized_status == "FILLED"
                and fill.qty <= 0
            )
            or (
                raw_status is not None
                and normalized_status
                not in {"FILLED", "PARTIAL", "CANCELED", "REJECTED", "EXPIRED"}
            )
        ):
            unresolved_status = normalized_status
            if unresolved_status not in {"OPEN", "PENDING", "UNBALANCED"}:
                # Un fill observé avec un statut absent, inconnu ou
                # contradictoire est encore plus conservateur : il peut déjà
                # avoir créé une position externe et doit rester réconciliable.
                unresolved_status = "UNBALANCED" if fill.qty > 0 else None
            message = (
                f"État non terminal ou inconnu retourné pour l'intention {actual_intent_id} "
                f"({normalized_status or 'UNKNOWN'})"
            )
            self.store.mark_order_ambiguous_and_checkpoint(
                order_id,
                engine=engine,
                state=checkpoint_state,
                error=message,
                status=unresolved_status,
                filled_qty=fill.qty,
                price=fill.price,
                fee=fill.fee,
                broker_order_id=fill.broker_order_id,
            )
            raise NonTerminalOrder(message)

        if normalized_status in {"FILLED", "PARTIAL", "CANCELED", "REJECTED"}:
            if not getattr(self.broker, "is_paper", False) and fill.qty > 0:
                if normalized_status == "PARTIAL":
                    status = "PARTIAL"
                elif fill.qty < canonical_qty - 1e-9:
                    status = "PARTIAL"
                else:
                    status = "FILLED"
            else:
                status = normalized_status
        elif normalized_status == "EXPIRED":
            status = "REJECTED"
        else:
            status = (
                "REJECTED"
                if fill.qty <= 0
                else "PARTIAL"
                if fill.qty < canonical_qty - 1e-9
                else "FILLED"
            )
        return SubmittedOrder(
            fill,
            order_id,
            actual_intent_id,
            status,
            dict(effective_transition) if effective_transition is not None else None,
        )
