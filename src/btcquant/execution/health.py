"""Métriques d'exécution et incidents opérationnels dérivés de SQLite."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

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
    fill_ratio: float | None
    rejection_rate: float | None
    partial_rate: float | None
    average_slippage_bps: float | None
    p95_slippage_bps: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
        if order["status"] in ("FILLED", "PARTIAL", "REJECTED", "FAILED", "CANCELED")
    ]
    requested = sum(float(order["requested_qty"]) for order in terminal)
    filled = sum(float(order["filled_qty"]) for order in terminal)
    rejection_count = sum(order["status"] in ("REJECTED", "FAILED") for order in terminal)
    partial_count = sum(order["status"] == "PARTIAL" for order in terminal)

    slippages: list[float] = []
    for order in terminal:
        reference = order.get("reference_price")
        price = order.get("price")
        if (
            reference is None
            or price is None
            or float(reference) <= 0
            or float(order["filled_qty"]) <= 0
        ):
            continue
        reference_value = float(reference)
        price_value = float(price)
        if order["side"].upper() == "BUY":
            slippage = (price_value / reference_value - 1.0) * 10_000.0
        else:
            slippage = (1.0 - price_value / reference_value) * 10_000.0
        slippages.append(slippage)

    return ExecutionHealth(
        engine=engine,
        orders_analyzed=len(terminal),
        unresolved_order_ids=tuple(int(order["id"]) for order in unresolved),
        stale_pending_order_ids=tuple(stale_pending),
        unbalanced_order_ids=tuple(unbalanced),
        fill_ratio=filled / requested if requested else None,
        rejection_rate=rejection_count / len(terminal) if terminal else None,
        partial_rate=partial_count / len(terminal) if terminal else None,
        average_slippage_bps=(sum(slippages) / len(slippages) if slippages else None),
        p95_slippage_bps=_percentile(slippages, 0.95),
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


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)))
    return ordered[index]
