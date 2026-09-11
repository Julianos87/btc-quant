"""Réconciliation état local ↔ exchange (mode live uniquement).

Au démarrage du runner en live, compare la position nette locale (somme des
sous-systèmes) à la position réelle sur l'exchange. Tout écart est signalé
(log + Telegram) : c'est la protection contre les doubles positions ou les
positions orphelines après un crash. Fail-closed : une erreur ou un écart
interdit au runner de démarrer.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from ..notify import notify
from .broker import Broker

log = logging.getLogger(__name__)

# Historical safety cap, not an economic tolerance. A venue/instrument
# policy can only narrow this cap; it can never widen the accepted region.
LEGACY_MAX_POSITION_RECONCILIATION_TOLERANCE = 1e-5


@dataclass(frozen=True)
class PositionReconciliationReport:
    """Résultat explicite du rapprochement de position distante."""

    ok: bool
    supported: bool
    local_net: float | None = None

    remote_net: float | None = None
    reason: str = ""
    context: dict[str, object] | None = None


def _position_tolerance(
    broker: Broker,
    symbol: str,
) -> tuple[float | None, dict[str, object]]:
    """Return a conservative, broker-provided position comparison policy."""

    try:
        quantum = broker.position_quantity_quantum(symbol)
    except Exception as error:
        return None, {
            "policy_source": "broker.position_quantity_quantum",
            "policy_status": "error",
            "policy_error": f"{type(error).__name__}: {error}",
        }
    if quantum is None:
        return None, {
            "policy_source": "broker.position_quantity_quantum",
            "policy_status": "unavailable",
        }
    if isinstance(quantum, bool) or not isinstance(quantum, (int, float)):
        return None, {
            "policy_source": "broker.position_quantity_quantum",
            "policy_status": "invalid",
            "policy_error": "quantum must be a finite positive number",
        }
    if not math.isfinite(quantum) or quantum <= 0:
        return None, {
            "policy_source": "broker.position_quantity_quantum",
            "policy_status": "invalid",
            "policy_error": "quantum must be a finite positive number",
        }
    tolerance = min(LEGACY_MAX_POSITION_RECONCILIATION_TOLERANCE, quantum)
    return tolerance, {
        "policy_source": "broker.position_quantity_quantum",
        "policy_status": "available",
        "quantity_quantum": quantum,
        "legacy_max_tolerance": LEGACY_MAX_POSITION_RECONCILIATION_TOLERANCE,
        "effective_tolerance": tolerance,
    }


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
    if diff == 0:
        return PositionReconciliationReport(
            ok=True,
            supported=True,
            local_net=local_net,
            remote_net=remote_net,
            reason="position_equal",
        )
    tolerance, policy_context = _position_tolerance(broker, symbol)
    if tolerance is not None and abs(diff) < tolerance:
        return PositionReconciliationReport(
            ok=True,
            supported=True,
            local_net=local_net,
            remote_net=remote_net,
            reason="position_equal",
            context={"symbol": symbol, "diff": diff, **policy_context},
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
            "tolerance": tolerance,
            **policy_context,
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
            f"{report.local_net:+.6f} {symbol}, exchange {report.remote_net:+.6f} {symbol} "
            f"(diff {diff:+.6f}). Vérifier manuellement avant de laisser trader."
        )
    log.error(msg)
    notify(msg)
    return False
