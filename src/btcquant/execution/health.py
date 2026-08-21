"""Métriques d'exécution et incidents opérationnels dérivés de SQLite."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from ..observability import SafetyStatus
from .quality_metrics import percentile, slippages_bps
from .state_contract import (
    EXCHANGE_STOP_CONFIRMED,
    EXCHANGE_STOP_MISSING,
    EXCHANGE_STOP_REPLACEMENT_ACTIVE,
    PROTECTION_MODE_UNKNOWN,
    RECONCILIATION_BLOCKS_PROTECTION,
    SOFTWARE_STOP_ACTIVE,
    SOFTWARE_STOP_INCONSISTENT_TRANSITION,
    SOFTWARE_STOP_INVALID,
    STOP_PROTECTION_EXCHANGE,
    STOP_PROTECTION_SOFTWARE,
    VALID_STOP_PROTECTION_MODES,
)
from .state_store import StateStore


@dataclass(frozen=True)
class HealthThresholds:
    stale_pending_seconds: float = 300.0
    rejection_rate_warning: float = 0.05
    partial_rate_warning: float = 0.10
    slippage_bps_warning: float = 20.0
    order_window: int = 200


@dataclass(frozen=True)
class ExecutionHealth:
    engine: str
    orders_analyzed: int
    unresolved_order_ids: tuple[int, ...]
    stale_pending_order_ids: tuple[int, ...]
    unbalanced_order_ids: tuple[int, ...]
    unprotected_slots: tuple[str, ...]
    stop_transition_slots: tuple[str, ...]
    reconciliation_required: bool
    fill_ratio: float | None
    rejection_rate: float | None
    partial_rate: float | None
    average_slippage_bps: float | None
    p95_slippage_bps: float | None
    protection_mode: str | None = None
    slot_protection: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionSafetyHealth:
    """Authoritative safety verdict shared by operational read surfaces."""

    status: SafetyStatus
    engines: dict[str, ExecutionHealth]
    reasons: tuple[str, ...] = ()
    open_critical_incidents: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "engines": {name: health.to_dict() for name, health in self.engines.items()},
            "reasons": list(self.reasons),
            "open_critical_incidents": list(self.open_critical_incidents),
        }


def _finite_positive(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and number > 0


def _valid_signed_direction(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    if not math.isfinite(number) or number not in (-1.0, 1.0):
        return False
    return True


def software_stop_contract_valid(position: object) -> bool:
    """True when a persisted PAPER/SOFTWARE stop can actually protect the slot."""

    if not isinstance(position, Mapping):
        return False
    if not _finite_positive(position.get("qty")):
        return False
    if not _finite_positive(position.get("stop_price")):
        return False
    if not _valid_signed_direction(position.get("direction")):
        return False
    return True


def evaluate_open_slot_protection(
    slot: Mapping[str, Any],
    *,
    protection_mode: object,
    reconciliation_required: bool,
) -> tuple[str, bool, bool]:
    """Return ``(reason, protected, exchange_transition_pending)`` for an OPEN slot.

    Missing/unknown protection mode is fail-closed. SOFTWARE never infers
    safety from a numeric stop alone on an EXCHANGE checkpoint.
    """

    transition = slot.get("stop_transition")
    transition_pending = transition is not None
    previous_stop = transition.get("previous_stop_id") if isinstance(transition, dict) else None
    if reconciliation_required:
        return RECONCILIATION_BLOCKS_PROTECTION, False, transition_pending
    if protection_mode not in VALID_STOP_PROTECTION_MODES:
        return PROTECTION_MODE_UNKNOWN, False, transition_pending
    if protection_mode == STOP_PROTECTION_SOFTWARE:
        if transition is not None or slot.get("stop_order_id") not in (None, ""):
            return SOFTWARE_STOP_INCONSISTENT_TRANSITION, False, True
        if not software_stop_contract_valid(slot.get("position")):
            return SOFTWARE_STOP_INVALID, False, False
        return SOFTWARE_STOP_ACTIVE, True, False
    if protection_mode != STOP_PROTECTION_EXCHANGE:
        return PROTECTION_MODE_UNKNOWN, False, transition_pending
    if slot.get("stop_order_id") is None and previous_stop is None:
        return EXCHANGE_STOP_MISSING, False, transition_pending
    if previous_stop is not None and slot.get("stop_order_id") is None:
        return EXCHANGE_STOP_REPLACEMENT_ACTIVE, True, transition_pending
    return EXCHANGE_STOP_CONFIRMED, True, transition_pending


@dataclass(frozen=True)
class _IncidentCondition:
    active: bool
    severity: str
    kind: str
    message: str
    context: dict[str, Any]


def execution_health(
    store: StateStore,
    engine: str,
    thresholds: HealthThresholds | None = None,
    *,
    now: datetime | None = None,
) -> ExecutionHealth:
    cfg = thresholds or HealthThresholds()
    current = now or datetime.now(UTC)
    orders = store.read_orders(engine)[-cfg.order_window :]
    unresolved = store.unresolved_orders(engine)
    state = store.load_engine_state(engine) or {}
    unprotected_slots: list[str] = []
    stop_transition_slots: list[str] = []
    slot_protection: list[tuple[str, str]] = []
    protection_mode = state.get("stop_protection_mode")
    recorded_mode = str(protection_mode) if protection_mode in VALID_STOP_PROTECTION_MODES else None
    reconciliation_required = bool(state.get("reconciliation_required"))
    for slot_name, slot in (state.get("slots") or {}).items():
        if not isinstance(slot, dict) or slot.get("position") is None:
            continue
        reason, protected, transition_pending = evaluate_open_slot_protection(
            slot,
            protection_mode=protection_mode,
            reconciliation_required=reconciliation_required,
        )
        slot_protection.append((str(slot_name), reason))
        if transition_pending:
            stop_transition_slots.append(str(slot_name))
        if not protected:
            unprotected_slots.append(str(slot_name))
    stale_pending: list[int] = []
    unbalanced: list[int] = []
    for order in unresolved:
        order_id = int(order["id"])
        if order["status"] == "UNBALANCED":
            unbalanced.append(order_id)
        if order["status"] in ("PENDING", "OPEN"):
            updated = datetime.fromisoformat(str(order["updated_at"]))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if (current - updated).total_seconds() > cfg.stale_pending_seconds:
                stale_pending.append(order_id)

    terminal = [
        order
        for order in orders
        if order["order_type"] != "STOP"
        and order["status"] in ("FILLED", "PARTIAL", "REJECTED", "FAILED", "CANCELED")
    ]
    requested = sum(float(order["requested_qty"]) for order in terminal)
    filled = sum(float(order["filled_qty"]) for order in terminal)
    rejection_count = sum(order["status"] in ("REJECTED", "FAILED") for order in terminal)
    partial_count = sum(order["status"] == "PARTIAL" for order in terminal)

    slippages = slippages_bps(terminal)

    return ExecutionHealth(
        engine=engine,
        orders_analyzed=len(terminal),
        unresolved_order_ids=tuple(int(order["id"]) for order in unresolved),
        stale_pending_order_ids=tuple(stale_pending),
        unbalanced_order_ids=tuple(unbalanced),
        unprotected_slots=tuple(unprotected_slots),
        stop_transition_slots=tuple(stop_transition_slots),
        reconciliation_required=reconciliation_required,
        protection_mode=recorded_mode,
        slot_protection=tuple(slot_protection),
        fill_ratio=filled / requested if requested else None,
        rejection_rate=rejection_count / len(terminal) if terminal else None,
        partial_rate=partial_count / len(terminal) if terminal else None,
        average_slippage_bps=(sum(slippages) / len(slippages) if slippages else None),
        p95_slippage_bps=percentile(slippages, 0.95),
    )


def execution_safety_health(
    store: StateStore,
    engines: tuple[str, ...] = ("trend", "carry"),
    *,
    now: datetime | None = None,
) -> ExecutionSafetyHealth:
    """Return one fail-closed safety verdict for all known execution state.

    Component optionality belongs to availability/readiness. A known unsafe
    position, order or reconciliation state remains unsafe regardless of the
    component's required flag.
    """

    try:
        health_by_engine = {engine: execution_health(store, engine, now=now) for engine in engines}
        open_critical = tuple(
            str(item["fingerprint"])
            for item in store.read_incidents(open_only=True)
            if item.get("severity") == "CRITICAL"
            and str(item.get("fingerprint", "")).startswith("execution:")
        )
    except Exception:
        return ExecutionSafetyHealth(
            status=SafetyStatus.UNKNOWN,
            engines={},
            reasons=("EXECUTION_SAFETY_UNKNOWN",),
        )

    reasons: list[str] = []
    for engine, health in health_by_engine.items():
        if health.unresolved_order_ids:
            reasons.append(f"{engine.upper()}_UNRESOLVED_ORDER")
        if health.unbalanced_order_ids:
            reasons.append(f"{engine.upper()}_UNBALANCED")
        if health.unprotected_slots:
            reasons.append(f"{engine.upper()}_UNPROTECTED_POSITION")
        if health.stop_transition_slots:
            reasons.append(f"{engine.upper()}_STOP_TRANSITION_PENDING")
        if health.reconciliation_required:
            reasons.append(f"{engine.upper()}_RECONCILIATION_REQUIRED")
    if open_critical:
        reasons.append("OPEN_CRITICAL_EXECUTION_INCIDENT")
    return ExecutionSafetyHealth(
        status=SafetyStatus.FAIL if reasons else SafetyStatus.PASS,
        engines=health_by_engine,
        reasons=tuple(sorted(set(reasons))),
        open_critical_incidents=open_critical,
    )


def sync_execution_incidents(
    store: StateStore,
    health: ExecutionHealth,
    thresholds: HealthThresholds | None = None,
) -> list[dict[str, Any]]:
    """Synchronise les incidents et retourne ceux qui doivent être notifiés."""

    cfg = thresholds or HealthThresholds()
    engine = health.engine
    conditions: dict[str, _IncidentCondition] = {
        f"execution:{engine}:unbalanced": _IncidentCondition(
            active=bool(health.unbalanced_order_ids),
            severity="CRITICAL",
            kind="unbalanced_orders",
            message=f"{len(health.unbalanced_order_ids)} ordre(s) UNBALANCED",
            context={"order_ids": health.unbalanced_order_ids},
        ),
        f"execution:{engine}:unprotected_position": _IncidentCondition(
            active=bool(health.unprotected_slots),
            severity="CRITICAL",
            kind="unprotected_position",
            message=f"{len(health.unprotected_slots)} position(s) sans stop confirmé",
            context={
                "slots": health.unprotected_slots,
                "protection_mode": health.protection_mode,
                "slot_protection": dict(health.slot_protection),
            },
        ),
        f"execution:{engine}:stop_transition_pending": _IncidentCondition(
            active=bool(health.stop_transition_slots),
            severity="CRITICAL",
            kind="stop_transition_pending",
            message=f"{len(health.stop_transition_slots)} transition(s) de stop en attente",
            context={"slots": health.stop_transition_slots},
        ),
        f"execution:{engine}:reconciliation_required": _IncidentCondition(
            active=health.reconciliation_required,
            severity="CRITICAL",
            kind="reconciliation_required",
            message="Le moteur exige une réconciliation manuelle",
            context={},
        ),
        f"execution:{engine}:stale_pending": _IncidentCondition(
            active=bool(health.stale_pending_order_ids),
            severity="CRITICAL",
            kind="stale_pending_orders",
            message=f"{len(health.stale_pending_order_ids)} ordre(s) PENDING trop ancien(s)",
            context={"order_ids": health.stale_pending_order_ids},
        ),
        f"execution:{engine}:high_rejection_rate": _IncidentCondition(
            active=health.rejection_rate is not None
            and health.rejection_rate > cfg.rejection_rate_warning,
            severity="WARNING",
            kind="high_rejection_rate",
            message=f"Taux de rejet élevé : {health.rejection_rate or 0:.1%}",
            context={"rejection_rate": health.rejection_rate},
        ),
        f"execution:{engine}:high_partial_rate": _IncidentCondition(
            active=health.partial_rate is not None
            and health.partial_rate > cfg.partial_rate_warning,
            severity="WARNING",
            kind="high_partial_rate",
            message=f"Taux de fills partiels élevé : {health.partial_rate or 0:.1%}",
            context={"partial_rate": health.partial_rate},
        ),
        f"execution:{engine}:high_slippage": _IncidentCondition(
            active=health.p95_slippage_bps is not None
            and health.p95_slippage_bps > cfg.slippage_bps_warning,
            severity="WARNING",
            kind="high_slippage",
            message=f"Slippage p95 élevé : {health.p95_slippage_bps or 0:.1f} bps",
            context={"p95_slippage_bps": health.p95_slippage_bps},
        ),
    }
    notifications: list[dict[str, Any]] = []
    for fingerprint, condition in conditions.items():
        if condition.active:
            incident = store.record_incident(
                fingerprint,
                engine=engine,
                severity=condition.severity,
                kind=condition.kind,
                message=condition.message,
                context=condition.context,
            )
            if incident["is_new_or_reopened"]:
                notifications.append(incident)
        else:
            store.resolve_incident(fingerprint)
    return notifications
