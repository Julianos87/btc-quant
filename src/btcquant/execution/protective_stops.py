"""Machine de cycle de vie des stops protecteurs externes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .broker import Broker, ProtectiveOrderSnapshot


class StopDecisionKind(StrEnum):
    NOOP = "NOOP"
    REPLACED = "REPLACED"
    FILLED = "FILLED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class StopDecision:
    kind: StopDecisionKind
    previous_stop_id: str | None = None
    replacement_stop_id: str | None = None
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
            return self._replace(None, qty, stop_price, direction)
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
            return StopDecision(StopDecisionKind.NOOP, snapshot=snapshot)
        if snapshot.status in ("CANCELED", "REJECTED", "EXPIRED"):
            return self._replace(
                stop_id,
                qty,
                stop_price,
                direction,
                previous_status=snapshot.status,
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

    def _replace(
        self,
        previous_id: str | None,
        qty: float,
        stop_price: float,
        direction: int,
        *,
        previous_status: str | None = None,
    ) -> StopDecision:
        try:
            replacement = self.broker.place_stop(qty, stop_price, direction)
        except Exception as error:
            prefix = (
                "Impossible de recréer le stop absent"
                if previous_id is None
                else f"Stop {previous_id} inactif et remplacement impossible"
            )
            return StopDecision(
                StopDecisionKind.UNCERTAIN,
                previous_stop_id=previous_id,
                previous_status=previous_status,
                message=f"{prefix} ({type(error).__name__})",
                context={
                    "stop_order_id": previous_id,
                    "status": previous_status,
                },
            )
        if replacement is None:
            message = (
                "Le broker n'a pas confirmé la recréation du stop"
                if previous_id is None
                else f"Stop {previous_id} inactif sans remplacement confirmé"
            )
            return StopDecision(
                StopDecisionKind.UNCERTAIN,
                previous_stop_id=previous_id,
                previous_status=previous_status,
                message=message,
                context={"stop_order_id": previous_id, "status": previous_status},
            )
        return StopDecision(
            StopDecisionKind.REPLACED,
            previous_stop_id=previous_id,
            replacement_stop_id=replacement,
            previous_status=previous_status,
        )
