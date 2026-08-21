"""Contrats sérialisables et validation des checkpoints moteurs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, NotRequired, TypedDict, cast


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
    financial_transition_seq: NotRequired[int]


STOP_PROTECTION_SOFTWARE = "SOFTWARE"
STOP_PROTECTION_EXCHANGE = "EXCHANGE"
VALID_STOP_PROTECTION_MODES = frozenset({STOP_PROTECTION_SOFTWARE, STOP_PROTECTION_EXCHANGE})

SOFTWARE_STOP_ACTIVE = "SOFTWARE_STOP_ACTIVE"
EXCHANGE_STOP_CONFIRMED = "EXCHANGE_STOP_CONFIRMED"
EXCHANGE_STOP_REPLACEMENT_ACTIVE = "EXCHANGE_STOP_REPLACEMENT_ACTIVE"
PROTECTION_MODE_UNKNOWN = "PROTECTION_MODE_UNKNOWN"
SOFTWARE_STOP_INVALID = "SOFTWARE_STOP_INVALID"
SOFTWARE_STOP_INCONSISTENT_TRANSITION = "SOFTWARE_STOP_INCONSISTENT_TRANSITION"
EXCHANGE_STOP_MISSING = "EXCHANGE_STOP_MISSING"
RECONCILIATION_BLOCKS_PROTECTION = "RECONCILIATION_BLOCKS_PROTECTION"


def stop_protection_mode_from_broker(*, supports_stop_orders: bool) -> str:
    """Derive the Trend protection mode from the live broker capability."""

    if supports_stop_orders:
        return STOP_PROTECTION_EXCHANGE
    return STOP_PROTECTION_SOFTWARE


class TrendStatePayload(TypedDict):
    slots: dict[str, TrendSlotState]
    peak_equity: float
    halted: bool
    day: str | None
    day_start_equity: float
    daily_lockout: bool
    reconciliation_required: bool
    last_funding_ts: str | None
    stop_protection_mode: NotRequired[str]


class CarryStatePayload(TypedDict):
    equity: float
    in_position: bool
    execution_state: str
    qty: float
    spot_qty: float
    entry_equity: NotRequired[float | None]
    entry_timestamp: NotRequired[str | None]
    entry_price: NotRequired[float | None]
    spot_notional: NotRequired[float]
    perp_notional: NotRequired[float]
    borrow_principal: NotRequired[float]
    position_generation: NotRequired[str | None]
    funding_notional_price_source: NotRequired[str | None]
    funding_notional_price_timestamp: NotRequired[str | None]
    funding_notional_price: NotRequired[float | None]
    perp_qty: float
    last_funding_ts: str | None
    peak_equity: float
    day: str | None
    day_start_equity: float
    halted: bool
    daily_lockout: bool
    accounting_uncertain: NotRequired[bool]
    accounting_uncertainty_reason: NotRequired[str | None]


def _mapping(payload: object, label: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"État {label} invalide : objet JSON attendu")
    return payload


def _finite_number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"État invalide : {label} absent ou non numérique")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = " strictement positif" if positive else " fini"
        raise ValueError(f"État invalide : {label} doit être{qualifier}")
    return number


def _validate_optional_risk_baselines(raw: Mapping[str, Any], engine: str) -> None:
    """Valide les baselines présentes tout en acceptant les anciens checkpoints."""

    for key in ("peak_equity", "day_start_equity"):
        if key in raw:
            _finite_number(raw[key], f"{engine}.{key}", positive=True)


def validate_trend_state(payload: object) -> TrendStatePayload:
    raw = _mapping(payload, "trend")
    slots = raw.get("slots")
    if not isinstance(slots, Mapping):
        raise ValueError("État trend invalide : slots absent ou non objet")
    if "stop_protection_mode" in raw:
        mode = raw.get("stop_protection_mode")
        if mode not in VALID_STOP_PROTECTION_MODES:
            raise ValueError(
                "État trend invalide : stop_protection_mode doit être "
                f"{STOP_PROTECTION_SOFTWARE} ou {STOP_PROTECTION_EXCHANGE}"
            )
    for name, value in slots.items():
        slot = _mapping(value, f"trend.{name}")
        _finite_number(slot.get("cash"), f"trend.{name}.cash")
        position = slot.get("position")
        sequence = slot.get("financial_transition_seq", 0)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError(
                f"État trend invalide : {name}.financial_transition_seq doit être "
                "un entier positif ou nul"
            )
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
    _validate_optional_risk_baselines(raw, "trend")
    return cast(TrendStatePayload, payload)


def validate_carry_state(payload: object) -> CarryStatePayload:
    raw = _mapping(payload, "carry")
    _finite_number(raw.get("equity"), "carry.equity")
    if not isinstance(raw.get("in_position"), bool):
        raise ValueError("État carry invalide : in_position absent ou non booléen")
    _validate_optional_risk_baselines(raw, "carry")
    return cast(CarryStatePayload, payload)
