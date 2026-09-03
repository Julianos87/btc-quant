"""Classification sûre des ordres interrompus par un crash."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .broker import Broker
from .order_state import ExternalOrderState, LocalOrderState
from .reconciliation_coordinator import OrderReconciliationCoordinator, ReconciliationStatus
from .resolution_projection import ProjectionStatus
from .state_store import StateStore

log = logging.getLogger(__name__)


def _external_state(value: object) -> ExternalOrderState:
    raw = str(value).upper()
    aliases = {
        "CANCELLED": ExternalOrderState.CANCELED,
        # L'ancien contrat ne persistait pas remaining_qty : PARTIAL pouvait
        # être terminal ou encore ouvert après timeout.
        "PARTIAL": ExternalOrderState.UNKNOWN,
    }
    if raw in aliases:
        return aliases[raw]
    try:
        return ExternalOrderState(raw)
    except ValueError:
        return ExternalOrderState.UNKNOWN


@dataclass
class RecoveryReport:
    recovered_order_ids: list[int] = field(default_factory=list)
    manual_order_ids: list[int] = field(default_factory=list)
    lookup_errors: dict[int, str] = field(default_factory=dict)

    @property
    def can_start(self) -> bool:
        return not self.manual_order_ids and not self.lookup_errors


def recover_interrupted_orders(
    store: StateStore,
    broker: Broker,
    engine: str,
    *,
    external: bool,
) -> RecoveryReport:
    """Récupère ce qui peut l'être sans jamais inventer un état de position.

    Paper sans preuve individuelle durable peut être abandonné après interruption.
    Dès qu'une preuve PAPER a été persistée, le même coordinateur et E3 sont
    rejoués avant toute décision de reprise. Pour un broker externe, toute soumission potentiellement initiée
    reste UNBALANCED ou PENDING_RECONCILIATION jusqu'à réconciliation explicite;
    le seul abandon automatique est prouvé localement avant soumission.
    """

    report = RecoveryReport()
    for order in store.unresolved_orders(engine):
        # Les stops ont leur propre saga persistante. Un stop OPEN est un état
        # nominal et un stop PENDING doit être repris avec son contexte
        # (ancien stop, quantité et trigger), indisponible dans ce récupérateur
        # générique d'ordres market.
        if order["order_type"] == "STOP":
            continue
        order_id = int(order["id"])
        if order["status"] == "UNBALANCED" and not external:
            report.manual_order_ids.append(order_id)
            continue

        # La réservation existe mais SUBMITTING n'a jamais été persisté : le
        # chemin broker n'a pas pu commencer. C'est le seul cas externe où
        # l'absence d'effet est prouvée uniquement par l'état local.
        if order["local_state"] == LocalOrderState.INTENT_CREATED.value:
            store.complete_order(
                order_id,
                status="RECOVERED_ABORTED",
                error="Crash après réservation et avant soumission broker",
            )
            report.recovered_order_ids.append(order_id)
            continue

        if not external:
            reconciliation = OrderReconciliationCoordinator(store).reconcile(order_id)
            projection = reconciliation.after.projection
            has_durable_fill = (
                projection.status == ProjectionStatus.READY
                and projection.bundle is not None
                and bool(projection.bundle.fills)
            )
            if (
                has_durable_fill
                or reconciliation.status == ReconciliationStatus.APPLICATION_BLOCKED
                or projection.status == ProjectionStatus.INVALID_PERSISTED_EVIDENCE
            ):
                # A local PAPER fill can already have been committed by E3
                # before a process crash. It must never be converted to the
                # legacy zero-effect recovery state: finalization is a later,
                # explicit contract. The coordinator is deliberately the
                # same one used by the normal runtime path.
                report.manual_order_ids.append(order_id)
                continue
            recovered = store.recover_local_market_order(
                order_id,
                error="Ordre paper interrompu : aucun effet externe durable",
            )
            if recovered:
                report.recovered_order_ids.append(order_id)
            else:
                report.manual_order_ids.append(order_id)
            continue

        if not broker.supports_order_lookup:
            store.complete_order(
                order_id,
                status="UNBALANCED",
                error="Broker externe sans recherche fiable par identifiant client",
            )
            report.manual_order_ids.append(order_id)
            continue

        try:
            snapshot = broker.lookup_order(str(order["intent_id"]))
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            log.exception("Recherche broker impossible pour l'ordre %s", order_id)
            report.lookup_errors[order_id] = message
            continue

        if snapshot is None:
            message = (
                "Ordre absent du lookup après début de soumission : "
                "absence externe non démontrée, nouvelle émission interdite"
            )
            store.complete_order(
                order_id,
                status="PENDING",
                remaining_qty=float(order["remaining_qty"]),
                error=message,
                external_state=ExternalOrderState.UNKNOWN,
                local_state=LocalOrderState.PENDING_RECONCILIATION,
            )
            report.lookup_errors[order_id] = message
            continue

        remote_status = _external_state(snapshot.status)
        remaining_qty = (
            snapshot.remaining_qty
            if snapshot.remaining_qty is not None
            else max(0.0, float(order["requested_qty"]) - snapshot.filled_qty)
        )
        store.complete_order(
            order_id,
            status="UNBALANCED",
            filled_qty=snapshot.filled_qty,
            remaining_qty=remaining_qty,
            price=snapshot.price,
            fee=snapshot.fee,
            broker_order_id=snapshot.broker_order_id,
            external_state=remote_status,
            local_state=LocalOrderState.PENDING_RECONCILIATION,
            error=(
                f"État broker {remote_status.value}, fill {snapshot.filled_qty:g} : "
                "checkpoint local incertain, réconciliation manuelle requise"
            ),
        )
        report.manual_order_ids.append(order_id)

    return report
