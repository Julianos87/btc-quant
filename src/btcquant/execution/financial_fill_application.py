"""Calcul pur et vérifiable de l'application financière d'un fill externe.

Ce module est volontairement situé sous les readers et au-dessus de toute
orchestration.  Il ne connaît ni ``StateStore``, ni broker, ni réseau.  Une
invocation reconstruit l'état depuis le pre-state durable et les fills déjà
appliqués; elle ne fait jamais confiance à une mutation incrémentale fournie
par l'appelant.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast

from .external_evidence import ExternalFill
from .financial_application_plan import (
    PersistedFinancialApplicationPlan,
    canonical_json,
    sha256_json,
    _plain_json_value,
)
from .order_state import FinancialTransitionType
from .resolution import ResolutionAssessment
from .state_contract import validate_trend_state


FINANCIAL_FILL_APPLICATION_VERSION = 1
FINANCIAL_APPLICATION_VERSION = FINANCIAL_FILL_APPLICATION_VERSION
FEE_ASSET_USDC = "USDC"
QUANTITY_TOLERANCE = 1e-9


class FinancialFillApplicationError(ValueError):
    """Erreur fail-closed d'éligibilité, de calcul ou d'intégrité."""


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinancialFillApplicationError(f"{name} doit être une chaîne non vide")
    return value.strip()


def _finite(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FinancialFillApplicationError(f"{name} doit être numérique")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = " strictement positif" if positive else " fini"
        raise FinancialFillApplicationError(f"{name} doit être{qualifier}")
    return number


def _same_quantity(left: float, right: float) -> bool:
    return abs(left - right) <= max(QUANTITY_TOLERANCE, max(abs(left), abs(right)) * 1e-9)


def _canonical_timestamp(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinancialFillApplicationError(f"{name} doit être un timestamp ISO 8601")
    candidate = value.strip()
    if candidate[-1:] in {"Z", "z"}:
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise FinancialFillApplicationError(f"{name} doit être un timestamp ISO 8601") from error
    if parsed.tzinfo is None:
        raise FinancialFillApplicationError(f"{name} doit contenir un fuseau explicite")
    return parsed.astimezone(UTC).isoformat()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: object) -> object:
    return _plain_json_value(value)


def _copy_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(canonical_json(value))
    except (TypeError, ValueError) as error:
        raise FinancialFillApplicationError("state payload non sérialisable") from error
    if not isinstance(copied, dict):
        raise FinancialFillApplicationError("state payload doit être un objet")
    return copied


def _ensure_finite_json(value: object, path: str = "payload") -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise FinancialFillApplicationError(f"{path} contient un nombre non fini")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _ensure_finite_json(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _ensure_finite_json(item, f"{path}[{index}]")
        return
    raise FinancialFillApplicationError(f"{path} contient une valeur non JSON")


def _economic_effect_at(plan: PersistedFinancialApplicationPlan, fill: ExternalFill) -> str:
    return _canonical_timestamp(
        fill.venue_event_at or plan.plan.planned_effect_at,
        "economic_effect_at",
    )


def financial_fill_application_key(
    *,
    application_version: int,
    local_order_id: int,
    intent_id: str,
    plan_key: str,
    fill_key: str,
) -> str:
    """Return the stable identity of one application attempt."""

    return (
        "finapp-"
        + hashlib.sha256(
            canonical_json(
                {
                    "application_version": application_version,
                    "fill_key": fill_key,
                    "intent_id": intent_id,
                    "local_order_id": local_order_id,
                    "plan_key": plan_key,
                }
            ).encode("utf-8")
        ).hexdigest()
    )


class FinancialApplicationLedgerConflict(FinancialFillApplicationError):
    """Fail-closed error raised while reading the durable ledger."""


@dataclass(frozen=True)
class FinancialFillApplicationResult:
    """Résultat immuable d'un calcul d'application pour un fill."""

    application_version: int
    application_key: str
    local_order_id: int
    intent_id: str
    plan_key: str
    fill_key: str
    transition_type: FinancialTransitionType | str
    economic_effect_at: str
    quantity: float
    price: float
    fee: float
    fee_asset: str
    state_before_sha256: str
    state_after_payload: Mapping[str, Any]
    state_after_sha256: str
    cash_delta: float
    trade_payload: Mapping[str, Any] | None = None
    result_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.application_version, bool)
            or not isinstance(self.application_version, int)
            or self.application_version != FINANCIAL_FILL_APPLICATION_VERSION
        ):
            raise FinancialFillApplicationError("application_version inconnue")
        for name in ("application_key", "intent_id", "plan_key", "fill_key"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise FinancialFillApplicationError("local_order_id invalide")
        try:
            transition = FinancialTransitionType(self.transition_type)
        except ValueError as error:
            raise FinancialFillApplicationError("transition_type invalide") from error
        object.__setattr__(self, "transition_type", transition)
        object.__setattr__(
            self,
            "economic_effect_at",
            _canonical_timestamp(self.economic_effect_at, "economic_effect_at"),
        )
        object.__setattr__(self, "quantity", _finite(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "price", _finite(self.price, "price", positive=True))
        object.__setattr__(self, "fee", _finite(self.fee, "fee"))
        fee_asset = _required_text(self.fee_asset, "fee_asset").upper()
        if fee_asset != FEE_ASSET_USDC:
            raise FinancialFillApplicationError("fee_asset non supporté")
        object.__setattr__(self, "fee_asset", fee_asset)
        for name in ("state_before_sha256", "state_after_sha256"):
            digest = _required_text(getattr(self, name), name)
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise FinancialFillApplicationError(f"{name} invalide")
            object.__setattr__(self, name, digest)
        object.__setattr__(self, "cash_delta", _finite(self.cash_delta, "cash_delta"))
        payload = _copy_payload(self.state_after_payload)
        _ensure_finite_json(payload, "state_after_payload")
        validate_trend_state(payload)
        if sha256_json(payload) != self.state_after_sha256:
            raise FinancialFillApplicationError("FINANCIAL_APPLICATION_STATE_HASH_CONFLICT")
        object.__setattr__(self, "state_after_payload", _freeze(payload))
        if self.trade_payload is not None:
            if not isinstance(self.trade_payload, Mapping):
                raise FinancialFillApplicationError("trade_payload doit être un objet")
            trade_payload = _copy_payload(self.trade_payload)
            _ensure_finite_json(trade_payload, "trade_payload")
            object.__setattr__(self, "trade_payload", _freeze(trade_payload))
        expected_key = financial_fill_application_key(
            application_version=self.application_version,
            local_order_id=self.local_order_id,
            intent_id=self.intent_id,
            plan_key=self.plan_key,
            fill_key=self.fill_key,
        )
        if self.application_key != expected_key:
            raise FinancialFillApplicationError("FINANCIAL_APPLICATION_KEY_CONFLICT")
        object.__setattr__(self, "result_sha256", sha256_json(self.result_content()))

    def result_content(self) -> dict[str, Any]:
        return {
            "application_key": self.application_key,
            "application_version": self.application_version,
            "cash_delta": self.cash_delta,
            "economic_effect_at": self.economic_effect_at,
            "fee": self.fee,
            "fee_asset": self.fee_asset,
            "fill_key": self.fill_key,
            "intent_id": self.intent_id,
            "local_order_id": self.local_order_id,
            "plan_key": self.plan_key,
            "price": self.price,
            "quantity": self.quantity,
            "state_after_payload": _plain(self.state_after_payload),
            "state_after_sha256": self.state_after_sha256,
            "state_before_sha256": self.state_before_sha256,
            "trade_payload": _plain(self.trade_payload) if self.trade_payload is not None else None,
            "transition_type": FinancialTransitionType(self.transition_type).value,
        }

    def as_payload(self) -> dict[str, Any]:
        payload = self.result_content()
        payload["result_sha256"] = self.result_sha256
        return payload

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        expected_result_sha256: str | None = None,
    ) -> FinancialFillApplicationResult:
        if not isinstance(payload, Mapping):
            raise FinancialFillApplicationError("result_payload doit être un objet")
        stored_hash = payload.get("result_sha256")
        result = cls(
            application_version=cast(int, payload.get("application_version")),
            application_key=cast(str, payload.get("application_key")),
            local_order_id=cast(int, payload.get("local_order_id")),
            intent_id=cast(str, payload.get("intent_id")),
            plan_key=cast(str, payload.get("plan_key")),
            fill_key=cast(str, payload.get("fill_key")),
            transition_type=cast(str, payload.get("transition_type")),
            economic_effect_at=cast(str, payload.get("economic_effect_at")),
            quantity=cast(float, payload.get("quantity")),
            price=cast(float, payload.get("price")),
            fee=cast(float, payload.get("fee")),
            fee_asset=cast(str, payload.get("fee_asset")),
            state_before_sha256=cast(str, payload.get("state_before_sha256")),
            state_after_payload=cast(Mapping[str, Any], payload.get("state_after_payload")),
            state_after_sha256=cast(str, payload.get("state_after_sha256")),
            cash_delta=cast(float, payload.get("cash_delta")),
            trade_payload=cast(Mapping[str, Any] | None, payload.get("trade_payload")),
        )
        if not isinstance(stored_hash, str) or stored_hash != result.result_sha256:
            raise FinancialFillApplicationError("FINANCIAL_APPLICATION_RESULT_HASH_CONFLICT")
        if expected_result_sha256 is not None and expected_result_sha256 != result.result_sha256:
            raise FinancialFillApplicationError("FINANCIAL_APPLICATION_RESULT_HASH_CONFLICT")
        return result


@dataclass(frozen=True)
class PersistedFinancialFillApplication:
    """Ligne de ledger lue et vérifiée, jamais écrite par ce lot."""

    application_key: str
    application_version: int
    local_order_id: int
    intent_id: str
    plan_key: str
    fill_key: str
    application_index: int
    previous_application_key: str | None
    transition_type: FinancialTransitionType | str
    economic_effect_at: str
    state_before_sha256: str
    state_after_sha256: str
    result: FinancialFillApplicationResult
    applied_at: str

    def __post_init__(self) -> None:
        for name in (
            "application_key",
            "intent_id",
            "plan_key",
            "fill_key",
            "state_before_sha256",
            "state_after_sha256",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(
            self,
            "previous_application_key",
            _required_text(self.previous_application_key, "previous_application_key")
            if self.previous_application_key is not None
            else None,
        )
        if (
            isinstance(self.application_version, bool)
            or not isinstance(self.application_version, int)
            or self.application_version != FINANCIAL_FILL_APPLICATION_VERSION
        ):
            raise FinancialFillApplicationError("application_version inconnue")
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise FinancialFillApplicationError("local_order_id invalide")
        if isinstance(self.application_index, bool) or not isinstance(self.application_index, int):
            raise FinancialFillApplicationError("application_index invalide")
        if self.application_index < 0:
            raise FinancialFillApplicationError("application_index invalide")
        object.__setattr__(self, "transition_type", FinancialTransitionType(self.transition_type))
        object.__setattr__(
            self,
            "economic_effect_at",
            _canonical_timestamp(self.economic_effect_at, "economic_effect_at"),
        )
        object.__setattr__(self, "applied_at", _canonical_timestamp(self.applied_at, "applied_at"))
        if not isinstance(self.result, FinancialFillApplicationResult):
            raise FinancialFillApplicationError("result invalide")
        expected_key = financial_fill_application_key(
            application_version=self.application_version,
            local_order_id=self.local_order_id,
            intent_id=self.intent_id,
            plan_key=self.plan_key,
            fill_key=self.fill_key,
        )
        if self.application_key != expected_key:
            raise FinancialFillApplicationError("FINANCIAL_APPLICATION_KEY_CONFLICT")
        if (
            self.result.application_key != self.application_key
            or self.result.application_version != self.application_version
            or self.result.local_order_id != self.local_order_id
            or self.result.intent_id != self.intent_id
            or self.result.plan_key != self.plan_key
            or self.result.fill_key != self.fill_key
            or self.result.transition_type != self.transition_type
            or self.result.economic_effect_at != self.economic_effect_at
            or self.result.state_before_sha256 != self.state_before_sha256
            or self.result.state_after_sha256 != self.state_after_sha256
        ):
            raise FinancialFillApplicationError("FINANCIAL_APPLICATION_RECORD_CONFLICT")


@dataclass(frozen=True)
class FinancialFillApplicationRequest:
    """Entrée complète et immuable du calcul pur."""

    persisted_plan: PersistedFinancialApplicationPlan
    fill: ExternalFill
    assessment: ResolutionAssessment
    current_state_payload: Mapping[str, Any]
    current_state_sha256: str
    previously_applied_fills: tuple[ExternalFill, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.persisted_plan, PersistedFinancialApplicationPlan):
            raise FinancialFillApplicationError("persisted_plan invalide")
        if not isinstance(self.fill, ExternalFill):
            raise FinancialFillApplicationError("fill invalide")
        if not isinstance(self.assessment, ResolutionAssessment):
            raise FinancialFillApplicationError("assessment invalide")
        if not isinstance(self.current_state_payload, Mapping):
            raise FinancialFillApplicationError("current_state_payload invalide")
        copied = _copy_payload(self.current_state_payload)
        _ensure_finite_json(copied, "current_state_payload")
        validate_trend_state(copied)
        object.__setattr__(self, "current_state_payload", _freeze(copied))
        expected_hash = sha256_json(copied)
        if self.current_state_sha256 != expected_hash:
            raise FinancialFillApplicationError("FINANCIAL_APPLICATION_STATE_CONFLICT")
        fills = tuple(self.previously_applied_fills)
        if any(not isinstance(item, ExternalFill) for item in fills):
            raise FinancialFillApplicationError("previously_applied_fills invalide")
        object.__setattr__(self, "previously_applied_fills", fills)


@dataclass(frozen=True)
class _FillStep:
    fill: ExternalFill
    economic_effect_at: str
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    cash_delta: float
    trade_payload: dict[str, Any] | None


def _slot(payload: dict[str, Any], slot_name: str) -> dict[str, Any]:
    slots = payload.get("slots")
    if not isinstance(slots, dict) or not isinstance(slots.get(slot_name), dict):
        raise FinancialFillApplicationError("financial slot absent du state")
    return slots[slot_name]


def _validate_fill_for_plan(
    persisted_plan: PersistedFinancialApplicationPlan,
    fill: ExternalFill,
    *,
    assessment: ResolutionAssessment | None,
    new_fill: bool,
) -> None:
    plan = persisted_plan.plan
    if persisted_plan.intent_id != plan.identity.intent_id:
        raise FinancialFillApplicationError("FINANCIAL_APPLICATION_PLAN_CONFLICT")
    if (
        fill.local_order_id != persisted_plan.local_order_id
        or fill.intent_id != persisted_plan.intent_id
    ):
        raise FinancialFillApplicationError("FINANCIAL_FILL_BINDING_CONFLICT")
    if fill.side != plan.side:
        raise FinancialFillApplicationError("FINANCIAL_FILL_BINDING_CONFLICT")
    if fill.fill_key is None:
        raise FinancialFillApplicationError("FINANCIAL_FILL_IDENTITY_UNPROVEN")
    if fill.venue_fill_id is None:
        raise FinancialFillApplicationError("IRREVERSIBLE_FILL_IDENTITY_UNPROVEN")
    if fill.fee is None:
        raise FinancialFillApplicationError("FINANCIAL_FEE_INCOMPLETE")
    if fill.fee_asset is None or fill.fee_asset.upper() != FEE_ASSET_USDC:
        raise FinancialFillApplicationError("FINANCIAL_FEE_ASSET_UNSUPPORTED")
    _finite(fill.quantity, "fill.quantity", positive=True)
    _finite(fill.price, "fill.price", positive=True)
    _finite(fill.fee, "fill.fee")
    if fill.quantity > plan.requested_qty + max(QUANTITY_TOLERANCE, plan.requested_qty * 1e-9):
        raise FinancialFillApplicationError("FINANCIAL_FILL_QUANTITY_EXCEEDS_PLAN")
    if new_fill:
        if assessment is None:
            raise FinancialFillApplicationError("ResolutionAssessment requis")
        if not assessment.binding_complete:
            raise FinancialFillApplicationError("FINANCIAL_FILL_BINDING_INCOMPLETE")
        if fill.fill_key not in assessment.financially_applicable_fill_keys:
            raise FinancialFillApplicationError("FINANCIAL_FILL_NOT_APPLICABLE")
        if fill.fill_key in assessment.financially_ambiguous_fill_keys:
            raise FinancialFillApplicationError("IRREVERSIBLE_FILL_IDENTITY_UNPROVEN")


def _ordered_fills(
    persisted_plan: PersistedFinancialApplicationPlan,
    fills: Sequence[ExternalFill],
) -> tuple[tuple[ExternalFill, str], ...]:
    keyed = [(fill, _economic_effect_at(persisted_plan, fill)) for fill in fills]
    return tuple(sorted(keyed, key=lambda item: (item[1], item[0].fill_key or "")))


def _apply_sequence(
    persisted_plan: PersistedFinancialApplicationPlan,
    fills: Sequence[ExternalFill],
) -> tuple[dict[str, Any], tuple[_FillStep, ...]]:
    plan = persisted_plan.plan
    transition = plan.identity.transition_type
    if transition == FinancialTransitionType.REDUCE:
        raise FinancialFillApplicationError("REDUCE_RUNTIME_PATH_NOT_CURRENTLY_ACTIVE")
    payload = _copy_payload(plan.pre_state_payload)
    target_slot = _slot(payload, plan.identity.slot)
    if transition in {FinancialTransitionType.ENTER_LONG, FinancialTransitionType.ENTER_SHORT}:
        if target_slot.get("position") is not None or not _same_quantity(
            float(target_slot.get("entry_fee", 0.0)), 0.0
        ):
            raise FinancialFillApplicationError("FINANCIAL_PRE_STATE_CONFLICT")
    elif not isinstance(target_slot.get("position"), dict):
        raise FinancialFillApplicationError("FINANCIAL_PRE_STATE_CONFLICT")

    ordered = _ordered_fills(persisted_plan, fills)
    steps: list[_FillStep] = []
    plan_fills: list[ExternalFill] = []
    initial_position = target_slot.get("position")
    initial_pyramid_adds = (
        int(initial_position.get("pyramid_adds", 0)) if isinstance(initial_position, dict) else None
    )
    total_qty = 0.0
    total_notional = 0.0
    total_exit_qty = 0.0
    for fill, effect_at in ordered:
        before = _copy_payload(payload)
        slot_state = _slot(payload, plan.identity.slot)
        qty = _finite(fill.quantity, "fill.quantity", positive=True)
        price = _finite(fill.price, "fill.price", positive=True)
        fee = _finite(fill.fee, "fill.fee")
        if transition in {FinancialTransitionType.ENTER_LONG, FinancialTransitionType.ENTER_SHORT}:
            total_qty += qty
            total_notional += qty * price
            if total_qty > plan.requested_qty + max(QUANTITY_TOLERANCE, plan.requested_qty * 1e-9):
                raise FinancialFillApplicationError("FINANCIAL_FILL_QUANTITY_EXCEEDS_PLAN")
            total_fee = float(slot_state["entry_fee"]) + fee
            vwap = total_notional / total_qty
            entry_direction = plan.entry_direction
            entry_stop = plan.entry_stop_price
            if entry_direction not in {-1, 1} or entry_stop is None:
                raise FinancialFillApplicationError("FINANCIAL_PLAN_ENTRY_CONTEXT_MISSING")
            slot_state["cash"] = float(slot_state["cash"]) - fee
            slot_state["entry_fee"] = total_fee
            slot_state["position"] = {
                "entry_time": min(
                    _economic_effect_at(persisted_plan, prior) for prior in plan_fills + [fill]
                ),
                "entry_price": vwap,
                "qty": total_qty,
                "stop_price": float(entry_stop),
                "direction": int(entry_direction),
                "bars_held": 0,
                "best_close": vwap,
                "initial_qty": total_qty,
                "last_add_price": vwap,
                "pyramid_adds": 0,
            }
            cash_delta = -fee
            trade = None
            plan_fills.append(fill)
        elif transition == FinancialTransitionType.ADD:
            position = slot_state.get("position")
            if not isinstance(position, dict):
                raise FinancialFillApplicationError("FINANCIAL_PRE_STATE_CONFLICT")
            previous_qty = _finite(position.get("qty"), "position.qty", positive=True)
            previous_price = _finite(
                position.get("entry_price"), "position.entry_price", positive=True
            )
            total_qty += qty
            total_notional += qty * price
            if total_qty > plan.requested_qty + max(QUANTITY_TOLERANCE, plan.requested_qty * 1e-9):
                raise FinancialFillApplicationError("FINANCIAL_FILL_QUANTITY_EXCEEDS_PLAN")
            new_qty = previous_qty + qty
            position["entry_price"] = (previous_qty * previous_price + qty * price) / new_qty
            position["qty"] = new_qty
            slot_state["cash"] = float(slot_state["cash"]) - fee
            slot_state["entry_fee"] = float(slot_state["entry_fee"]) + fee
            plan_fills.append(fill)
            plan_vwap = sum(item.quantity * item.price for item in plan_fills) / sum(
                item.quantity for item in plan_fills
            )
            position["last_add_price"] = plan_vwap
            # Multiple physical fills in one application plan are one pyramid add.
            assert initial_pyramid_adds is not None
            position["pyramid_adds"] = initial_pyramid_adds + 1
            cash_delta = -fee
            trade = None
        else:
            position = slot_state.get("position")
            if not isinstance(position, dict):
                raise FinancialFillApplicationError("FINANCIAL_PRE_STATE_CONFLICT")
            current_qty = _finite(position.get("qty"), "position.qty", positive=True)
            direction = position.get("direction")
            if direction not in {-1, 1}:
                raise FinancialFillApplicationError("FINANCIAL_PRE_STATE_CONFLICT")
            total_exit_qty += qty
            if total_exit_qty > plan.requested_qty + max(
                QUANTITY_TOLERANCE, plan.requested_qty * 1e-9
            ):
                raise FinancialFillApplicationError("FINANCIAL_FILL_QUANTITY_EXCEEDS_PLAN")
            if qty > current_qty + max(QUANTITY_TOLERANCE, current_qty * 1e-9):
                raise FinancialFillApplicationError("FINANCIAL_FILL_QUANTITY_EXCEEDS_POSITION")
            entry_price = _finite(
                position.get("entry_price"), "position.entry_price", positive=True
            )
            entry_fee = _finite(slot_state.get("entry_fee"), "entry_fee")
            gross_pnl = int(direction) * qty * (price - entry_price)
            entry_fee_share = entry_fee * (qty / current_qty)
            cash_delta = gross_pnl - fee
            slot_state["cash"] = float(slot_state["cash"]) + cash_delta
            remaining_qty = current_qty - qty
            if remaining_qty <= max(QUANTITY_TOLERANCE, current_qty * 1e-9):
                slot_state["position"] = None
                slot_state["entry_fee"] = 0.0
            else:
                position["qty"] = remaining_qty
                slot_state["entry_fee"] = entry_fee - entry_fee_share
                slot_state["position"] = position
            trade = {
                "exit_ts": effect_at,
                "entry_ts": _canonical_timestamp(str(position["entry_time"]), "entry_time"),
                "strategy": plan.identity.slot,
                "direction": "LONG" if int(direction) == 1 else "SHORT",
                "qty": qty,
                "entry_price": entry_price,
                "exit_price": price,
                "pnl": gross_pnl - fee - entry_fee_share,
                "bars_held": int(position["bars_held"]),
                "reason": plan.reason,
            }
        validate_trend_state(payload)
        steps.append(_FillStep(fill, effect_at, before, _copy_payload(payload), cash_delta, trade))
    if not steps:
        raise FinancialFillApplicationError("au moins un fill est requis")
    _ensure_finite_json(payload, "state_after_payload")
    validate_trend_state(payload)
    return payload, tuple(steps)


def calculate_financial_fill_application(
    request: FinancialFillApplicationRequest,
) -> FinancialFillApplicationResult:
    """Calculate one application without I/O or mutation.

    ``current_state_payload`` is the state immediately before the new fill.
    The state is independently recomputed from the durable plan and all prior
    fills.  An arbitrary caller-provided hash can therefore never bypass the
    replay check.
    """

    if not isinstance(request, FinancialFillApplicationRequest):
        raise TypeError("request doit être FinancialFillApplicationRequest")
    plan = request.persisted_plan
    fill = request.fill
    _validate_fill_for_plan(plan, fill, assessment=request.assessment, new_fill=True)
    all_keys = [item.fill_key for item in request.previously_applied_fills]
    if any(key is None for key in all_keys) or len(set(all_keys)) != len(all_keys):
        raise FinancialFillApplicationError("FINANCIAL_APPLICATION_DUPLICATE_FILL")
    if fill.fill_key in all_keys:
        raise FinancialFillApplicationError("FINANCIAL_APPLICATION_DUPLICATE_FILL")
    for prior in request.previously_applied_fills:
        _validate_fill_for_plan(plan, prior, assessment=None, new_fill=False)
    previous_ordered = _ordered_fills(plan, request.previously_applied_fills)
    current_effect = _economic_effect_at(plan, fill)
    if previous_ordered and (current_effect, fill.fill_key or "") < (
        previous_ordered[-1][1],
        previous_ordered[-1][0].fill_key or "",
    ):
        raise FinancialFillApplicationError("FINANCIAL_APPLICATION_OUT_OF_ORDER")
    state_before_payload, _ = (
        _apply_sequence(plan, request.previously_applied_fills)
        if request.previously_applied_fills
        else (
            _copy_payload(plan.plan.pre_state_payload),
            (),
        )
    )
    state_before_hash = sha256_json(state_before_payload)
    if state_before_hash != request.current_state_sha256:
        raise FinancialFillApplicationError("FINANCIAL_APPLICATION_STATE_CONFLICT")
    after_payload, steps = _apply_sequence(plan, (*request.previously_applied_fills, fill))
    current_steps = [step for step in steps if step.fill.fill_key == fill.fill_key]
    if len(current_steps) != 1:
        raise FinancialFillApplicationError("FINANCIAL_APPLICATION_DUPLICATE_FILL")
    current = current_steps[0]
    application_key = financial_fill_application_key(
        application_version=FINANCIAL_FILL_APPLICATION_VERSION,
        local_order_id=plan.local_order_id,
        intent_id=plan.intent_id,
        plan_key=plan.plan.plan_key,
        fill_key=fill.fill_key or "",
    )
    return FinancialFillApplicationResult(
        application_version=FINANCIAL_FILL_APPLICATION_VERSION,
        application_key=application_key,
        local_order_id=plan.local_order_id,
        intent_id=plan.intent_id,
        plan_key=plan.plan.plan_key,
        fill_key=fill.fill_key or "",
        transition_type=plan.plan.identity.transition_type,
        economic_effect_at=current.economic_effect_at,
        quantity=fill.quantity,
        price=fill.price,
        fee=fill.fee if fill.fee is not None else 0.0,
        fee_asset=fill.fee_asset or "",
        state_before_sha256=sha256_json(current.state_before),
        state_after_payload=after_payload,
        state_after_sha256=sha256_json(after_payload),
        cash_delta=current.cash_delta,
        trade_payload=current.trade_payload,
    )


def apply_financial_fill(
    request: FinancialFillApplicationRequest,
) -> FinancialFillApplicationResult:
    """Alias explicite du calcul pur; aucun ledger writer n'est appelé."""

    return calculate_financial_fill_application(request)


__all__ = [
    "FEE_ASSET_USDC",
    "FINANCIAL_APPLICATION_VERSION",
    "FINANCIAL_FILL_APPLICATION_VERSION",
    "FinancialApplicationLedgerConflict",
    "FinancialFillApplicationError",
    "FinancialFillApplicationRequest",
    "FinancialFillApplicationResult",
    "PersistedFinancialFillApplication",
    "apply_financial_fill",
    "calculate_financial_fill_application",
    "financial_fill_application_key",
]
