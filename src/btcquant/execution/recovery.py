"""Classification sûre des ordres interrompus par un crash."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .broker import Broker
from .state_store import StateStore

log = logging.getLogger(__name__)

SAFE_REMOTE_TERMINAL_STATUSES = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}


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

    Paper n'a aucun effet externe durable : une intention interrompue peut être
    abandonnée. Pour un broker externe, seuls l'absence confirmée de l'ordre ou
    un ordre terminal sans fill sont récupérables automatiquement. Dès qu'un
    fill est possible, l'ordre devient ``UNBALANCED`` et exige une intervention.
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
        if order["status"] == "UNBALANCED":
            report.manual_order_ids.append(order_id)
            continue

        if not external:
            store.complete_order(
                order_id,
                status="RECOVERED_ABORTED",
                error="Ordre paper interrompu : aucun effet externe durable",
            )
            report.recovered_order_ids.append(order_id)
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
            store.complete_order(
                order_id,
                status="RECOVERED_ABORTED",
                error="Broker confirme l'absence de l'ordre : envoi non effectué",
            )
            report.recovered_order_ids.append(order_id)
            continue

        remote_status = snapshot.status.upper()
        if snapshot.filled_qty <= 0 and remote_status in SAFE_REMOTE_TERMINAL_STATUSES:
            store.complete_order(
                order_id,
                status="RECOVERED_ABORTED",
                filled_qty=0.0,
                price=snapshot.price,
                fee=snapshot.fee,
                broker_order_id=snapshot.broker_order_id,
                error=f"Ordre broker terminal sans fill ({remote_status})",
            )
            report.recovered_order_ids.append(order_id)
            continue

        store.complete_order(
            order_id,
            status="UNBALANCED",
            filled_qty=snapshot.filled_qty,
            price=snapshot.price,
            fee=snapshot.fee,
            broker_order_id=snapshot.broker_order_id,
            error=(
                f"État broker {remote_status}, fill {snapshot.filled_qty:g} : "
                "checkpoint local incertain, réconciliation manuelle requise"
            ),
        )
        report.manual_order_ids.append(order_id)

    return report
