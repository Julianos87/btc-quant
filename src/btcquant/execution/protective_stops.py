"""Machine de cycle de vie des stops protecteurs externes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .broker import Broker, ProtectiveOrderSnapshot


class StopDecisionKind(StrEnum):
    NOOP = "NOOP"
    REPLACE_REQUIRED = "REPLACE_REQUIRED"
    FILLED = "FILLED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class StopDecision:
    kind: StopDecisionKind
    previous_stop_id: str | None = None
    previous_status: str | None = None
    snapshot: ProtectiveOrderSnapshot | None = None
    message: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


class ProtectiveStopService:
    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    def inspect(
        self,
        *,
        stop_id: str | None,
        qty: float | None,
        stop_price: float | None,
        direction: int | None,
    ) -> StopDecision:
        if qty is None:
            if stop_id is None:
                return StopDecision(StopDecisionKind.NOOP)
            return StopDecision(
                StopDecisionKind.UNCERTAIN,
                previous_stop_id=stop_id,
                message="Stop externe présent sans position locale",
                context={"stop_order_id": stop_id},
            )
        assert stop_price is not None and direction is not None
        if stop_id is None:
            return StopDecision(
                StopDecisionKind.REPLACE_REQUIRED,
                message="Position locale sans stop protecteur confirmé",
            )
        try:
            snapshot = self.broker.protective_order_snapshot(stop_id)
        except Exception as error:
            return StopDecision(
                StopDecisionKind.UNCERTAIN,
                previous_stop_id=stop_id,
                message=f"Statut du stop {stop_id} inconnu ({type(error).__name__})",
                context={"stop_order_id": stop_id},
            )
        if snapshot.status == "OPEN":
            if abs(snapshot.requested_qty - qty) <= 1e-9:
                return StopDecision(StopDecisionKind.NOOP, snapshot=snapshot)
            return StopDecision(
                StopDecisionKind.REPLACE_REQUIRED,
                previous_stop_id=stop_id,
                previous_status=snapshot.status,
                snapshot=snapshot,
                message=(
                    f"Quantité du stop {stop_id} à réaligner "
                    f"({snapshot.requested_qty:.8f} != {qty:.8f})"
                ),
            )
        if snapshot.status in ("CANCELED", "REJECTED", "EXPIRED"):
            return StopDecision(
                StopDecisionKind.REPLACE_REQUIRED,
                previous_stop_id=stop_id,
                previous_status=snapshot.status,
                snapshot=snapshot,
                message=f"Stop {stop_id} inactif ({snapshot.status})",
            )
        if snapshot.status == "FILLED" and abs(snapshot.filled_qty - qty) <= 1e-9:
            return StopDecision(
                StopDecisionKind.FILLED,
                previous_stop_id=stop_id,
                snapshot=snapshot,
            )
        return StopDecision(
            StopDecisionKind.UNCERTAIN,
            previous_stop_id=stop_id,
            snapshot=snapshot,
            message=(
                f"Stop {stop_id} dans un état ambigu ({snapshot.status}, "
                f"filled={snapshot.filled_qty:.8f}, local={qty:.8f})"
            ),
            context={
                "stop_order_id": stop_id,
                "status": snapshot.status,
                "filled_qty": snapshot.filled_qty,
                "remaining_qty": snapshot.remaining_qty,
                "local_qty": qty,
            },
        )
