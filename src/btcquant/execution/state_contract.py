"""Contrats sérialisables et validation des checkpoints moteurs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict, cast


class PositionState(TypedDict):
    entry_time: str
    entry_price: float
    qty: float
    stop_price: float
    direction: int
    bars_held: int
    best_close: float
    initial_qty: float
    last_add_price: float
    pyramid_adds: int


class TrendSlotState(TypedDict):
    cash: float
    position: PositionState | None
    stop_order_id: str | None
    stop_order_local_id: int | None
    stop_intent_id: str | None
    stop_transition: dict[str, Any] | None
    entry_fee: float
    last_bar_ts: str | None


class TrendStatePayload(TypedDict):
    slots: dict[str, TrendSlotState]
    peak_equity: float
    halted: bool
    day: str | None
    day_start_equity: float
    daily_lockout: bool
    reconciliation_required: bool
    last_funding_ts: str | None


class CarryStatePayload(TypedDict):
    equity: float
    in_position: bool
    execution_state: str
    qty: float
    spot_qty: float
    perp_qty: float
    last_funding_ts: str | None
    peak_equity: float
    day: str | None
    day_start_equity: float
    halted: bool
    daily_lockout: bool


def _mapping(payload: object, label: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"État {label} invalide : objet JSON attendu")
    return payload


def validate_trend_state(payload: object) -> TrendStatePayload:
    raw = _mapping(payload, "trend")
    slots = raw.get("slots")
    if not isinstance(slots, Mapping):
        raise ValueError("État trend invalide : slots absent ou non objet")
    for name, value in slots.items():
        slot = _mapping(value, f"trend.{name}")
        if not isinstance(slot.get("cash"), (int, float)):
            raise ValueError(f"État trend invalide : {name}.cash absent ou non numérique")
        position = slot.get("position")
        if position is not None:
            pos = _mapping(position, f"trend.{name}.position")
            required = {
                "entry_time",
                "entry_price",
                "qty",
                "stop_price",
                "direction",
                "bars_held",
                "best_close",
            }
            missing = sorted(required - pos.keys())
            if missing:
                raise ValueError(f"État trend invalide : {name}.position incomplet {missing}")
    return cast(TrendStatePayload, payload)


def validate_carry_state(payload: object) -> CarryStatePayload:
    raw = _mapping(payload, "carry")
    if not isinstance(raw.get("equity"), (int, float)):
        raise ValueError("État carry invalide : equity absente ou non numérique")
    if not isinstance(raw.get("in_position"), bool):
        raise ValueError("État carry invalide : in_position absent ou non booléen")
    return cast(CarryStatePayload, payload)
