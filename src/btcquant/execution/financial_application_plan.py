"""Contrat durable, immuable et vérifiable d'une application financière.

Ce module ne sait ni parler à un broker ni lire SQLite.  Il décrit seulement
les faits qui doivent être durables avant une soumission MARKET.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from .order_state import FinancialTransitionType, LogicalOrderIdentity
from .state_contract import (
    STOP_PROTECTION_EXCHANGE,
    STOP_PROTECTION_SOFTWARE,
    validate_trend_state,
)

APPLICATION_VERSION = 1
_LOGICAL_KEYS = frozenset(
    {
        "version",
        "engine",
        "slot",
        "decision_checkpoint",
        "transition_type",
        "position_generation",
        "transition_sequence",
    }
)


def _plain_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json_value(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    return json.dumps(
        _plain_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_utc_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("planned_effect_at doit être un timestamp ISO 8601 non vide")
    candidate = value.strip()
    if candidate[-1:] in {"Z", "z"}:
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ValueError("planned_effect_at doit être ISO 8601") from error
    if parsed.tzinfo is None:
        raise ValueError("planned_effect_at doit contenir un fuseau horaire explicite")
    return parsed.astimezone(UTC).isoformat()


def parse_logical_order_identity(
    logical_order_key: str, *, intent_id: str, engine: str, slot: str
) -> LogicalOrderIdentity:
    """Reconstruit strictement l'identité v1 persistée, sans tolérer de JSON vague."""

    try:
        raw = json.loads(logical_order_key)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("LOGICAL_IDENTITY_CONFLICT: JSON logique invalide") from error
    if not isinstance(raw, dict) or set(raw) != _LOGICAL_KEYS or raw.get("version") != 1:
        raise ValueError("LOGICAL_IDENTITY_CONFLICT: forme logique v1 invalide")
    try:
        identity = LogicalOrderIdentity(
            engine=raw["engine"],
            slot=raw["slot"],
            decision_checkpoint=raw["decision_checkpoint"],
            transition_type=FinancialTransitionType(raw["transition_type"]),
            position_generation=raw["position_generation"],
            transition_sequence=raw["transition_sequence"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("LOGICAL_IDENTITY_CONFLICT: valeurs logiques invalides") from error
    if (
        identity.logical_key != logical_order_key
        or identity.intent_id != intent_id
        or identity.engine != engine
        or identity.slot != slot
    ):
        raise ValueError("LOGICAL_IDENTITY_CONFLICT: identité logique divergente")
    return identity


def position_generation_from_payload(position: Mapping[str, Any]) -> str:
    entry_time = canonical_utc_timestamp(str(position.get("entry_time", "")))
    initial_qty = position.get("initial_qty")
    if isinstance(initial_qty, bool) or not isinstance(initial_qty, (int, float)):
        raise ValueError("position initial_qty invalide")
    initial = float(initial_qty)
    if not math.isfinite(initial) or initial <= 0:
        raise ValueError("position initial_qty invalide")
    return f"entry={entry_time}|initial_qty={format(initial, '.17g')}"


def _finite_number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} doit être numérique")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = " strictement positif" if positive else " fini"
        raise ValueError(f"{label} doit être{qualifier}")
    return number


def _validate_plan_position(position: Mapping[str, Any], slot: str) -> None:
    """Valide les champs financiers requis par un plan ADD/EXIT."""

    _finite_number(position.get("entry_price"), f"{slot}.position.entry_price", positive=True)
    _finite_number(position.get("qty"), f"{slot}.position.qty", positive=True)
    _finite_number(position.get("stop_price"), f"{slot}.position.stop_price", positive=True)
    _finite_number(position.get("initial_qty"), f"{slot}.position.initial_qty", positive=True)
    _finite_number(position.get("last_add_price"), f"{slot}.position.last_add_price", positive=True)
    _finite_number(position.get("best_close"), f"{slot}.position.best_close")
    entry_time = position.get("entry_time")
    if not isinstance(entry_time, str) or not entry_time.strip():
        raise ValueError(f"{slot}.position.entry_time invalide")
    canonical_utc_timestamp(entry_time)
    direction = position.get("direction")
    if isinstance(direction, bool) or direction not in {-1, 1}:
        raise ValueError(f"{slot}.position.direction invalide")
    for name in ("bars_held", "pyramid_adds"):
        value = position.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{slot}.position.{name} invalide")


def _validate_plan_state(payload: Mapping[str, Any], slot: str) -> None:
    slot_payload = payload.get("slots", {}).get(slot)
    if not isinstance(slot_payload, Mapping):
        raise ValueError(f"pre_state_payload: slot {slot!r} absent")
    _finite_number(slot_payload.get("cash"), f"{slot}.cash")
    _finite_number(slot_payload.get("entry_fee"), f"{slot}.entry_fee")
    position = slot_payload.get("position")
    if position is not None:
        if not isinstance(position, Mapping):
            raise ValueError(f"{slot}.position invalide")
        _validate_plan_position(position, slot)


def _freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json_value(item) for item in value)
    return value


@dataclass(frozen=True)
class FinancialApplicationPlan:
    """Plan v1 immuable associé à une unique intention MARKET Trend."""

    identity: LogicalOrderIdentity
    side: str
    requested_qty: float
    reference_price: float
    reason: str
    reduce_only: bool
    planned_effect_at: str
    pre_state_payload: Mapping[str, Any]
    protection_mode: str
    entry_direction: int | None = None
    entry_stop_price: float | None = None
    application_version: int = APPLICATION_VERSION

    def __post_init__(self) -> None:
        if self.application_version != APPLICATION_VERSION:
            raise ValueError("application_version inconnue")
        if self.identity.engine != "trend":
            raise ValueError("FinancialApplicationPlan v1 ne supporte que trend")
        side = str(self.side).strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side invalide")
        object.__setattr__(self, "side", side)
        for field in ("requested_qty", "reference_price"):
            value = getattr(self, field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field} doit être fini et strictement positif")
            object.__setattr__(self, field, float(value))
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason doit être non vide")
        object.__setattr__(self, "reason", self.reason.strip())
        if not isinstance(self.reduce_only, bool):
            raise ValueError("reduce_only doit être bool")
        object.__setattr__(
            self, "planned_effect_at", canonical_utc_timestamp(self.planned_effect_at)
        )
        if self.protection_mode not in {STOP_PROTECTION_SOFTWARE, STOP_PROTECTION_EXCHANGE}:
            raise ValueError("protection_mode invalide")
        try:
            payload = json.loads(canonical_json(dict(self.pre_state_payload)))
        except (TypeError, ValueError) as error:
            raise ValueError("pre_state_payload non sérialisable") from error
        validate_trend_state(payload)
        _validate_plan_state(payload, self.identity.slot)
        frozen_payload = _freeze_json_value(payload)
        if not isinstance(frozen_payload, Mapping):
            raise ValueError("pre_state_payload doit être un objet JSON")
        object.__setattr__(self, "pre_state_payload", frozen_payload)
        self._validate_transition(frozen_payload)

    def _validate_transition(self, payload: Mapping[str, Any]) -> None:
        position = payload["slots"].get(self.identity.slot, {}).get("position")
        transition = self.identity.transition_type
        if transition == FinancialTransitionType.ENTER_LONG:
            if (
                position is not None
                or self.side != "BUY"
                or self.entry_direction != 1
                or self.reduce_only
            ):
                raise ValueError("ENTER_LONG plan invalide")
            self._validate_entry_stop()
        elif transition == FinancialTransitionType.ENTER_SHORT:
            if (
                position is not None
                or self.side != "SELL"
                or self.entry_direction != -1
                or self.reduce_only
            ):
                raise ValueError("ENTER_SHORT plan invalide")
            self._validate_entry_stop()
        elif transition in {FinancialTransitionType.EXIT, FinancialTransitionType.ADD}:
            if not isinstance(position, Mapping):
                raise ValueError(f"{transition.value} exige une position préexistante")
            if position_generation_from_payload(position) != self.identity.position_generation:
                raise ValueError("position_generation ne correspond pas au pre-state")
            direction = position.get("direction")
            expected_side = "SELL" if direction == 1 else "BUY" if direction == -1 else None
            if expected_side is None or self.side != expected_side:
                raise ValueError("side incompatible avec la position préexistante")
            if transition == FinancialTransitionType.EXIT:
                if not self.reduce_only or self.requested_qty > float(position["qty"]) + 1e-12:
                    raise ValueError("EXIT plan invalide")
            elif self.reduce_only:
                raise ValueError("ADD ne peut pas être reduce_only")
            if self.entry_direction is not None or self.entry_stop_price is not None:
                raise ValueError("contexte ENTRY interdit hors ENTRY")
        else:
            raise ValueError("REDUCE_RUNTIME_PATH_NOT_CURRENTLY_ACTIVE")

    def _validate_entry_stop(self) -> None:
        value = self.entry_stop_price
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("ENTRY exige entry_stop_price fini et strictement positif")
        object.__setattr__(self, "entry_stop_price", float(value))

    @property
    def pre_state_sha256(self) -> str:
        return sha256_json(self.pre_state_payload)

    @property
    def plan_key(self) -> str:
        return sha256_json(self.semantic_content())

    def semantic_content(self) -> dict[str, Any]:
        return {
            "application_version": self.application_version,
            "logical_order_key": self.identity.logical_key,
            "side": self.side,
            "requested_qty": self.requested_qty,
            "reference_price": self.reference_price,
            "reason": self.reason,
            "reduce_only": self.reduce_only,
            "planned_effect_at": self.planned_effect_at,
            "entry_direction": self.entry_direction,
            "entry_stop_price": self.entry_stop_price,
            "pre_state_sha256": self.pre_state_sha256,
            "protection_mode": self.protection_mode,
        }


@dataclass(frozen=True)
class PersistedFinancialApplicationPlan:
    local_order_id: int
    intent_id: str
    plan: FinancialApplicationPlan
    created_at: str
