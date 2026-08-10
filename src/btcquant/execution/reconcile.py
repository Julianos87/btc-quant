"""Réconciliation état local ↔ exchange (mode live uniquement).

Au démarrage du runner en live, compare la position nette locale (somme des
sous-systèmes) à la position réelle sur l'exchange. Tout écart est signalé
(log + Telegram) : c'est la protection contre les doubles positions ou les
positions orphelines après un crash. Fail-closed : une erreur ou un écart
interdit au runner de démarrer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..notify import notify
from .broker import Broker

log = logging.getLogger(__name__)

TOLERANCE_BTC = 1e-5


@dataclass(frozen=True)
class PositionReconciliationReport:
    """Résultat explicite du rapprochement de position distante."""

    ok: bool
    supported: bool
    local_net: float | None = None
    remote_net: float | None = None
    reason: str = ""
    context: dict[str, object] | None = None


def inspect_position_reconciliation(
    broker: Broker,
    slots: list,
    symbol: str,
) -> PositionReconciliationReport:
    """Observe la position sans jamais corriger automatiquement SQLite.

    Un endpoint uniquement net ne permet pas d'attribuer deux expositions
    locales à des slots distincts. Dans ce cas le résultat est explicitement
    non prouvable, même lorsque la somme nette semble correcte.
    """

    if not broker.supports_position_reconciliation:
        return PositionReconciliationReport(
            ok=True,
            supported=False,
            reason="broker_without_position_reconciliation",
        )

    active_slots = [s for s in slots if s.position is not None]
    if len(active_slots) > 1:
        names = [str(getattr(getattr(s, "strategy", None), "name", "<slot>")) for s in active_slots]
        return PositionReconciliationReport(
            ok=False,
            supported=True,
            reason="multi_slot_net_attribution_unavailable",
            context={
                "slots": names,
                "message": (
                    "Le broker expose seulement la position nette ; "
                    "l'attribution slot par slot n'est pas prouvée"
                ),
            },
        )

    local_net = sum((s.position.direction * s.position.qty) for s in active_slots)
    try:
        remote_net = broker.net_position(symbol)
    except Exception as error:
        return PositionReconciliationReport(
            ok=False,
            supported=True,
            local_net=local_net,
            reason="remote_position_lookup_failed",
            context={
                "error": f"{type(error).__name__}: {error}",
                "symbol": symbol,
            },
        )

    diff = local_net - remote_net
    if abs(diff) <= TOLERANCE_BTC:
        return PositionReconciliationReport(
            ok=True,
            supported=True,
            local_net=local_net,
            remote_net=remote_net,
            reason="position_equal",
        )
    return PositionReconciliationReport(
        ok=False,
        supported=True,
        local_net=local_net,
        remote_net=remote_net,
        reason="position_mismatch",
        context={
            "symbol": symbol,
            "diff": diff,
            "tolerance": TOLERANCE_BTC,
        },
    )


def reconcile(broker: Broker, slots: list, symbol: str) -> bool:
    """Compatibilité booléenne du port de réconciliation."""

    report = inspect_position_reconciliation(broker, slots, symbol)
    if not report.supported:
        return True
    if report.ok:
        log.info(
            "Réconciliation OK : net local %.6f = exchange %.6f",
            report.local_net,
            report.remote_net,
        )
        return True
    if report.reason == "multi_slot_net_attribution_unavailable":
        msg = (
            "⛔ Réconciliation multi-slot impossible : le broker ne fournit "
            "pas l'attribution par slot — trading interdit"
        )
    elif report.reason == "remote_position_lookup_failed":
        msg = (
            f"⛔ Réconciliation impossible ({(report.context or {}).get('error')}) "
            "— trading interdit"
        )
    else:
        diff = (report.context or {}).get("diff")
        msg = (
            f"⚠ RÉCONCILIATION : écart détecté ! État local "
            f"{report.local_net:+.6f} BTC, exchange {report.remote_net:+.6f} BTC "
            f"(diff {diff:+.6f}). Vérifier manuellement avant de laisser trader."
        )
    log.error(msg)
    notify(msg)
    return False
