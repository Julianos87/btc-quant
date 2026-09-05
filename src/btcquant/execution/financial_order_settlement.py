"""Pure contracts and calculations for terminal external IOC settlements.

This module is deliberately below acquisition and above persistence.  It does
not know about SQLite, CCXT, brokers, clocks, or the runner.  A settlement is
the complete immutable result of one bound terminal order attempt; it is not a
synthetic :class:`ExternalFill` and it does not manufacture a venue fill id.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, cast

from .financial_application_plan import (
    PersistedFinancialApplicationPlan,
    canonical_json,
    sha256_json,
)
from .external_submission_commitment import AuthoritativeSubmissionFillCommitment
from .order_state import ExternalOrderState, FinancialTransitionType
from .state_contract import validate_trend_state


SETTLEMENT_VERSION = 1
FINANCIAL_SETTLEMENT_APPLICATION_VERSION = 1
SETTLEMENT_COMPLETENESS_VERSION = 1
SETTLEMENT_COMPLETENESS_COMMITMENT_VERSION = 2
SETTLEMENT_RESPONSE_LIMIT = 2_000
SETTLEMENT_RETENTION_LIMIT = 10_000
SETTLEMENT_FEE_ASSET = "USDC"
SETTLEMENT_QUANTITY_TOLERANCE = 1e-9


class FinancialSettlementError(ValueError):
    """Fail-closed error raised by the pure settlement contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinancialSettlementError(f"{name} doit être une chaîne non vide")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _finite(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FinancialSettlementError(f"{name} doit être numérique")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = " strictement positif" if positive else " fini"
        raise FinancialSettlementError(f"{name} doit être{qualifier}")
    return number


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FinancialSettlementError(f"{name} doit être ISO 8601 avec fuseau")
    candidate = value.strip()
    if candidate[-1:] in {"Z", "z"}:
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise FinancialSettlementError(f"{name} doit être ISO 8601 avec fuseau") from error
    if parsed.tzinfo is None:
        raise FinancialSettlementError(f"{name} doit contenir un fuseau explicite")
    return parsed.astimezone(UTC).isoformat()


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _copy_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(canonical_json(value))
    except (TypeError, ValueError) as error:
        raise FinancialSettlementError("pre-state non sérialisable") from error
    if not isinstance(copied, dict):
        raise FinancialSettlementError("pre-state doit être un objet")
    return copied


def _slot(payload: dict[str, Any], name: str) -> dict[str, Any]:
    slots = payload.get("slots")
    if not isinstance(slots, dict) or not isinstance(slots.get(name), dict):
        raise FinancialSettlementError("FINANCIAL_PRE_STATE_CONFLICT")
    return slots[name]


def _raw_hash(value: object) -> str:
    text = _text(value, "raw_payload_hash")
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise FinancialSettlementError("raw_payload_hash doit être un SHA-256 hexadécimal")
    return text


@dataclass(frozen=True)
class ExternalSettlementFillRow:
    """One raw fill row in a settlement multiset.

    No ``venue_fill_id`` is present by design.  Equal rows remain repeated in
    the enclosing tuple; multiplicity is evidence and is never deduplicated.
    """

    external_order_id: str
    account_scope: str
    instrument: str
    side: str
    quantity: float
    price: float
    fee: float | None
    fee_asset: str | None
    venue_event_at: str
    raw_payload_hash: str
    client_order_id: str | None = None
    reported_trade_id_candidate: str | None = None
    transaction_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "external_order_id", _text(self.external_order_id, "external_order_id")
        )
        object.__setattr__(self, "account_scope", _text(self.account_scope, "account_scope"))
        object.__setattr__(self, "instrument", _text(self.instrument, "instrument"))
        side = _text(self.side, "side").upper()
        if side not in {"BUY", "SELL"}:
            raise FinancialSettlementError("side doit être BUY ou SELL")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", _finite(self.quantity, "quantity", positive=True))
        object.__setattr__(self, "price", _finite(self.price, "price", positive=True))
        if self.fee is not None:
            object.__setattr__(self, "fee", _finite(self.fee, "fee"))
            if self.fee_asset is None:
                raise FinancialSettlementError("fee_asset requis avec une fee")
        object.__setattr__(self, "fee_asset", _optional_text(self.fee_asset, "fee_asset"))
        if self.fee is None and self.fee_asset is not None:
            raise FinancialSettlementError("fee_asset exige une fee explicitement observée")
        object.__setattr__(
            self, "venue_event_at", _timestamp(self.venue_event_at, "venue_event_at")
        )
        object.__setattr__(self, "raw_payload_hash", _raw_hash(self.raw_payload_hash))
        object.__setattr__(
            self, "client_order_id", _optional_text(self.client_order_id, "client_order_id")
        )
        object.__setattr__(
            self,
            "reported_trade_id_candidate",
            _optional_text(self.reported_trade_id_candidate, "reported_trade_id_candidate"),
        )
        object.__setattr__(
            self, "transaction_hash", _optional_text(self.transaction_hash, "transaction_hash")
        )

    def canonical_content(self) -> dict[str, Any]:
        return {
            "account_scope": self.account_scope,
            "client_order_id": self.client_order_id,
            "external_order_id": self.external_order_id,
            "fee": self.fee,
            "fee_asset": self.fee_asset,
            "instrument": self.instrument,
            "price": self.price,
            "quantity": self.quantity,
            "raw_payload_hash": self.raw_payload_hash,
            "reported_trade_id_candidate": self.reported_trade_id_candidate,
            "side": self.side,
            "transaction_hash": self.transaction_hash,
            "venue_event_at": self.venue_event_at,
        }


@dataclass(frozen=True)
class SettlementCompletenessProof:
    """Positive proof that one bounded terminal settlement is complete.

    The proof is intentionally constructed by the acquisition layer.  A plain
    response count is not enough.  Legacy completeness requires a retention
    witness; commitment-backed completeness instead binds the exact raw fill
    multiset to the authoritative submission aggregate.
    """

    local_order_id: int
    intent_id: str
    venue: str
    environment: str
    account_scope: str
    instrument: str
    side: str
    client_order_id: str
    external_order_id: str
    terminal_status: ExternalOrderState | str
    terminal_status_event_at: str
    window_start: str
    window_end: str
    response_count: int
    aggregate_by_time: bool
    malformed_entry_count: int
    retention_response_count: int
    retention_oldest_event_at: str | None
    response_limit: int = SETTLEMENT_RESPONSE_LIMIT
    retention_limit: int = SETTLEMENT_RETENTION_LIMIT
    completeness_version: int = SETTLEMENT_COMPLETENESS_VERSION
    submission_commitment: AuthoritativeSubmissionFillCommitment | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise FinancialSettlementError("local_order_id invalide")
        for name in (
            "intent_id",
            "venue",
            "environment",
            "account_scope",
            "instrument",
            "client_order_id",
            "external_order_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        side = _text(self.side, "side").upper()
        if side not in {"BUY", "SELL"}:
            raise FinancialSettlementError("side doit être BUY ou SELL")
        object.__setattr__(self, "side", side)
        try:
            status = ExternalOrderState(self.terminal_status)
        except ValueError as error:
            raise FinancialSettlementError("terminal_status invalide") from error
        if not status.is_terminal:
            raise FinancialSettlementError("terminal_status doit être terminal")
        object.__setattr__(self, "terminal_status", status)
        object.__setattr__(
            self,
            "terminal_status_event_at",
            _timestamp(self.terminal_status_event_at, "terminal_status_event_at"),
        )
        start = _timestamp(self.window_start, "window_start")
        end = _timestamp(self.window_end, "window_end")
        if end < start:
            raise FinancialSettlementError("window_end antérieur à window_start")
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        for name in (
            "response_count",
            "malformed_entry_count",
            "retention_response_count",
            "response_limit",
            "retention_limit",
            "completeness_version",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FinancialSettlementError(f"{name} invalide")
        if self.response_limit != SETTLEMENT_RESPONSE_LIMIT:
            raise FinancialSettlementError("response_limit doit être 2000")
        if self.retention_limit != SETTLEMENT_RETENTION_LIMIT:
            raise FinancialSettlementError("retention_limit doit être 10000")
        if self.completeness_version not in {
            SETTLEMENT_COMPLETENESS_VERSION,
            SETTLEMENT_COMPLETENESS_COMMITMENT_VERSION,
        }:
            raise FinancialSettlementError("completeness_version inconnue")
        if not isinstance(self.aggregate_by_time, bool):
            raise FinancialSettlementError("aggregate_by_time doit être bool")
        if self.retention_oldest_event_at is not None:
            object.__setattr__(
                self,
                "retention_oldest_event_at",
                _timestamp(self.retention_oldest_event_at, "retention_oldest_event_at"),
            )
        if self.submission_commitment is not None:
            if not isinstance(self.submission_commitment, AuthoritativeSubmissionFillCommitment):
                raise FinancialSettlementError("submission_commitment invalide")
            commitment_fields = (
                "local_order_id",
                "intent_id",
                "venue",
                "environment",
                "account_scope",
                "instrument",
                "side",
                "client_order_id",
                "external_order_id",
            )
            if any(
                getattr(self.submission_commitment, name) != getattr(self, name)
                for name in commitment_fields
            ):
                raise FinancialSettlementError("SETTLEMENT_COMMITMENT_BINDING_CONFLICT")
        if self.completeness_version == SETTLEMENT_COMPLETENESS_COMMITMENT_VERSION:
            if self.submission_commitment is None:
                raise FinancialSettlementError(
                    "COMMITMENT_COMPLETENESS_REQUIRES_SUBMISSION_COMMITMENT"
                )
        elif self.submission_commitment is not None:
            raise FinancialSettlementError(
                "SUBMISSION_COMMITMENT_REQUIRES_COMMITMENT_COMPLETENESS_VERSION"
            )

    @property
    def response_limit_reached(self) -> bool:
        return self.response_count >= self.response_limit

    @property
    def retention_witness_present(self) -> bool:
        return (
            self.retention_response_count > 0
            and self.retention_oldest_event_at is not None
            and self.retention_oldest_event_at <= self.window_start
        )

    @property
    def retention_witness_required(self) -> bool:
        """Whether this completeness version still needs a history witness."""

        return self.completeness_version == SETTLEMENT_COMPLETENESS_VERSION

    @property
    def is_complete(self) -> bool:
        return (
            ExternalOrderState(self.terminal_status).is_terminal
            and bool(self.terminal_status_event_at)
            and not self.aggregate_by_time
            and self.response_count < self.response_limit
            and not self.response_limit_reached
            and self.malformed_entry_count == 0
            and (not self.retention_witness_required or self.retention_witness_present)
            and (
                self.completeness_version == SETTLEMENT_COMPLETENESS_VERSION
                or self.submission_commitment is not None
            )
        )

    def incomplete_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.aggregate_by_time:
            reasons.append("AGGREGATED_FILL_RESPONSE")
        if self.response_limit_reached:
            reasons.append("SETTLEMENT_RESPONSE_LIMIT_REACHED")
        if self.malformed_entry_count:
            reasons.append("MALFORMED_FILL_ENTRY")
        if self.retention_witness_required and not self.retention_witness_present:
            reasons.append("RETENTION_COVERAGE_WITNESS_MISSING")
        if (
            self.completeness_version == SETTLEMENT_COMPLETENESS_COMMITMENT_VERSION
            and self.submission_commitment is None
        ):
            reasons.append("SUBMISSION_FILL_COMMITMENT_MISSING")
        return tuple(reasons)


@dataclass(frozen=True)
class ExternalOrderSettlement:
    """Complete terminal effect evidence for exactly one order attempt."""

    local_order_id: int
    intent_id: str
    venue: str
    environment: str
    account_scope: str
    instrument: str
    side: str
    client_order_id: str
    external_order_id: str
    terminal_status: ExternalOrderState | str
    terminal_status_event_at: str
    completeness: SettlementCompletenessProof
    fills: tuple[ExternalSettlementFillRow, ...] = ()
    version: int = SETTLEMENT_VERSION
    canonical_fill_multiset: tuple[ExternalSettlementFillRow, ...] = field(init=False)
    raw_fill_count: int = field(init=False)
    fill_multiset_sha256: str = field(init=False)
    total_qty: float = field(init=False)
    total_notional: float = field(init=False)
    total_fee: float | None = field(init=False)
    fee_asset: str | None = field(init=False)
    earliest_fill_at: str | None = field(init=False)
    latest_fill_at: str | None = field(init=False)
    settlement_key: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise FinancialSettlementError("local_order_id invalide")
        for name in (
            "intent_id",
            "venue",
            "environment",
            "account_scope",
            "instrument",
            "client_order_id",
            "external_order_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        side = _text(self.side, "side").upper()
        if side not in {"BUY", "SELL"}:
            raise FinancialSettlementError("side doit être BUY ou SELL")
        object.__setattr__(self, "side", side)
        try:
            status = ExternalOrderState(self.terminal_status)
        except ValueError as error:
            raise FinancialSettlementError("terminal_status invalide") from error
        if not status.is_terminal:
            raise FinancialSettlementError("terminal_status doit être terminal")
        object.__setattr__(self, "terminal_status", status)
        terminal_at = _timestamp(self.terminal_status_event_at, "terminal_status_event_at")
        object.__setattr__(self, "terminal_status_event_at", terminal_at)
        if not isinstance(self.completeness, SettlementCompletenessProof):
            raise FinancialSettlementError("completeness invalide")
        proof_fields = (
            "local_order_id",
            "intent_id",
            "venue",
            "environment",
            "account_scope",
            "instrument",
            "side",
            "client_order_id",
            "external_order_id",
            "terminal_status",
            "terminal_status_event_at",
        )
        for name in proof_fields:
            if getattr(self.completeness, name) != getattr(self, name):
                raise FinancialSettlementError(f"SETTLEMENT_BINDING_CONFLICT: {name}")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != SETTLEMENT_VERSION
        ):
            raise FinancialSettlementError("version de settlement inconnue")
        rows = tuple(self.fills)
        if any(not isinstance(row, ExternalSettlementFillRow) for row in rows):
            raise FinancialSettlementError("fills doit contenir des ExternalSettlementFillRow")
        for row in rows:
            if (
                row.external_order_id != self.external_order_id
                or row.account_scope != self.account_scope
                or row.instrument != self.instrument
                or row.side != self.side
                or (row.client_order_id is not None and row.client_order_id != self.client_order_id)
            ):
                raise FinancialSettlementError("SETTLEMENT_FILL_BINDING_CONFLICT")
        ordered = tuple(sorted(rows, key=lambda row: canonical_json(row.canonical_content())))
        commitment = self.completeness.submission_commitment
        if commitment is not None:
            try:
                committed_qty = Decimal(commitment.total_filled_qty)
                committed_avg = Decimal(commitment.average_price)
                total_qty_decimal = sum((Decimal(str(row.quantity)) for row in rows), Decimal(0))
                total_notional_decimal = sum(
                    (Decimal(str(row.quantity)) * Decimal(str(row.price)) for row in rows),
                    Decimal(0),
                )
                if total_qty_decimal != committed_qty:
                    raise FinancialSettlementError("SETTLEMENT_COMMITMENT_QUANTITY_CONFLICT")
                if not rows or total_notional_decimal / total_qty_decimal != committed_avg:
                    raise FinancialSettlementError("SETTLEMENT_COMMITMENT_VWAP_CONFLICT")
            except (InvalidOperation, ZeroDivisionError) as error:
                raise FinancialSettlementError("SETTLEMENT_COMMITMENT_DECIMAL_CONFLICT") from error
        object.__setattr__(self, "canonical_fill_multiset", ordered)
        object.__setattr__(self, "fills", ordered)
        object.__setattr__(self, "raw_fill_count", len(ordered))
        canonical_rows = [row.canonical_content() for row in ordered]
        object.__setattr__(self, "fill_multiset_sha256", _sha256(canonical_rows))
        total_qty = sum(row.quantity for row in ordered)
        total_notional = sum(row.quantity * row.price for row in ordered)
        fees = [row.fee for row in ordered]
        fee_assets = {row.fee_asset for row in ordered if row.fee_asset is not None}
        object.__setattr__(self, "total_qty", total_qty)
        object.__setattr__(self, "total_notional", total_notional)
        object.__setattr__(
            self, "total_fee", sum(fees) if fees and all(fee is not None for fee in fees) else None
        )
        object.__setattr__(
            self,
            "fee_asset",
            next(iter(fee_assets))
            if len(fee_assets) == 1 and all(fee is not None for fee in fees)
            else None,
        )
        times = [row.venue_event_at for row in ordered]
        object.__setattr__(self, "earliest_fill_at", min(times) if times else None)
        object.__setattr__(self, "latest_fill_at", max(times) if times else None)
        object.__setattr__(
            self,
            "settlement_key",
            "settlement-v1-"
            + _sha256(
                {
                    "account_scope": self.account_scope,
                    "client_order_id": self.client_order_id,
                    "environment": self.environment,
                    "external_order_id": self.external_order_id,
                    "fill_multiset_sha256": self.fill_multiset_sha256,
                    "instrument": self.instrument,
                    "intent_id": self.intent_id,
                    "local_order_id": self.local_order_id,
                    "side": self.side,
                    "terminal_status": ExternalOrderState(self.terminal_status).value,
                    "terminal_status_event_at": self.terminal_status_event_at,
                    "venue": self.venue,
                    "version": self.version,
                }
            ),
        )

    def to_persistence_payload(self) -> dict[str, Any]:
        """Return the complete immutable evidence payload for SQLite storage."""

        completeness = self.completeness
        return {
            "version": self.version,
            "local_order_id": self.local_order_id,
            "intent_id": self.intent_id,
            "venue": self.venue,
            "environment": self.environment,
            "account_scope": self.account_scope,
            "instrument": self.instrument,
            "side": self.side,
            "client_order_id": self.client_order_id,
            "external_order_id": self.external_order_id,
            "terminal_status": ExternalOrderState(self.terminal_status).value,
            "terminal_status_event_at": self.terminal_status_event_at,
            "settlement_key": self.settlement_key,
            "fill_multiset_sha256": self.fill_multiset_sha256,
            "completeness": {
                "local_order_id": completeness.local_order_id,
                "intent_id": completeness.intent_id,
                "venue": completeness.venue,
                "environment": completeness.environment,
                "account_scope": completeness.account_scope,
                "instrument": completeness.instrument,
                "side": completeness.side,
                "client_order_id": completeness.client_order_id,
                "external_order_id": completeness.external_order_id,
                "terminal_status": ExternalOrderState(completeness.terminal_status).value,
                "terminal_status_event_at": completeness.terminal_status_event_at,
                "window_start": completeness.window_start,
                "window_end": completeness.window_end,
                "response_count": completeness.response_count,
                "aggregate_by_time": completeness.aggregate_by_time,
                "malformed_entry_count": completeness.malformed_entry_count,
                "retention_response_count": completeness.retention_response_count,
                "retention_oldest_event_at": completeness.retention_oldest_event_at,
                "response_limit": completeness.response_limit,
                "retention_limit": completeness.retention_limit,
                "completeness_version": completeness.completeness_version,
                "submission_commitment": (
                    completeness.submission_commitment.to_payload()
                    if completeness.submission_commitment is not None
                    else None
                ),
            },
            "fills": [row.canonical_content() for row in self.canonical_fill_multiset],
        }

    @classmethod
    def from_persistence_payload(cls, payload: Mapping[str, Any]) -> ExternalOrderSettlement:
        """Rebuild a settlement and re-run every contract validator."""

        if not isinstance(payload, Mapping):
            raise FinancialSettlementError("settlement payload doit être un objet")
        completeness_payload = payload.get("completeness")
        fills_payload = payload.get("fills")
        if not isinstance(completeness_payload, Mapping):
            raise FinancialSettlementError("settlement completeness absente")
        if not isinstance(fills_payload, (list, tuple)):
            raise FinancialSettlementError("settlement fills invalides")
        commitment_payload = completeness_payload.get("submission_commitment")
        submission_commitment = None
        if commitment_payload is not None:
            if not isinstance(commitment_payload, Mapping):
                raise FinancialSettlementError("submission_commitment invalide")
            try:
                submission_commitment = AuthoritativeSubmissionFillCommitment(
                    local_order_id=commitment_payload["local_order_id"],
                    intent_id=commitment_payload["intent_id"],
                    venue=commitment_payload["venue"],
                    environment=commitment_payload["environment"],
                    account_scope=commitment_payload["account_scope"],
                    instrument=commitment_payload["instrument"],
                    side=commitment_payload["side"],
                    client_order_id=commitment_payload["client_order_id"],
                    external_order_id=commitment_payload["external_order_id"],
                    total_filled_qty=commitment_payload["total_filled_qty"],
                    average_price=commitment_payload["average_price"],
                    response_acquired_at=commitment_payload["response_acquired_at"],
                    raw_response_hash=commitment_payload["raw_response_hash"],
                    response_type=commitment_payload.get("response_type", "filled"),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise FinancialSettlementError("submission_commitment invalide") from error
        completeness_payload = dict(completeness_payload)
        completeness_payload["submission_commitment"] = submission_commitment
        try:
            completeness = SettlementCompletenessProof(**dict(completeness_payload))
            fills = tuple(
                ExternalSettlementFillRow(**dict(fill))
                for fill in fills_payload
                if isinstance(fill, Mapping)
            )
        except (TypeError, ValueError) as error:
            raise FinancialSettlementError("settlement payload invalide") from error
        if len(fills) != len(fills_payload):
            raise FinancialSettlementError("settlement fill entry invalide")
        settlement = cls(
            local_order_id=cast(int, payload.get("local_order_id")),
            intent_id=cast(str, payload.get("intent_id")),
            venue=cast(str, payload.get("venue")),
            environment=cast(str, payload.get("environment")),
            account_scope=cast(str, payload.get("account_scope")),
            instrument=cast(str, payload.get("instrument")),
            side=cast(str, payload.get("side")),
            client_order_id=cast(str, payload.get("client_order_id")),
            external_order_id=cast(str, payload.get("external_order_id")),
            terminal_status=cast(str, payload.get("terminal_status")),
            terminal_status_event_at=cast(str, payload.get("terminal_status_event_at")),
            completeness=completeness,
            fills=fills,
            version=cast(int, payload.get("version")),
        )
        if payload.get("settlement_key") != settlement.settlement_key:
            raise FinancialSettlementError("settlement_key conflict")
        if payload.get("fill_multiset_sha256") != settlement.fill_multiset_sha256:
            raise FinancialSettlementError("fill_multiset_sha256 conflict")
        return settlement


@dataclass(frozen=True)
class FinancialSettlementResult:
    """Pure recomposition of the financial result of one settlement."""

    settlement_key: str
    version: int
    local_order_id: int
    intent_id: str
    plan_key: str
    transition_type: FinancialTransitionType | str
    quantity: float
    total_notional: float
    total_fee: float
    fee_asset: str
    economic_effect_at: str
    state_before_sha256: str
    state_after_payload: Mapping[str, Any]
    state_after_sha256: str
    cash_delta: float
    trade_payload: Mapping[str, Any] | None = None
    zero_effect: bool = False
    result_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "settlement_key", _text(self.settlement_key, "settlement_key"))
        if self.version != SETTLEMENT_VERSION:
            raise FinancialSettlementError("settlement result version inconnue")
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise FinancialSettlementError("local_order_id invalide")
        object.__setattr__(self, "intent_id", _text(self.intent_id, "intent_id"))
        object.__setattr__(self, "plan_key", _text(self.plan_key, "plan_key"))
        object.__setattr__(self, "transition_type", FinancialTransitionType(self.transition_type))
        object.__setattr__(self, "quantity", _finite(self.quantity, "quantity"))
        object.__setattr__(self, "total_notional", _finite(self.total_notional, "total_notional"))
        object.__setattr__(self, "total_fee", _finite(self.total_fee, "total_fee"))
        if _text(self.fee_asset, "fee_asset").upper() != SETTLEMENT_FEE_ASSET:
            raise FinancialSettlementError("fee_asset non supporté")
        object.__setattr__(self, "fee_asset", SETTLEMENT_FEE_ASSET)
        object.__setattr__(
            self, "economic_effect_at", _timestamp(self.economic_effect_at, "economic_effect_at")
        )
        object.__setattr__(self, "state_before_sha256", _raw_hash(self.state_before_sha256))
        payload = _copy_payload(self.state_after_payload)
        validate_trend_state(payload)
        if sha256_json(payload) != self.state_after_sha256:
            raise FinancialSettlementError("FINANCIAL_SETTLEMENT_STATE_HASH_CONFLICT")
        object.__setattr__(self, "state_after_payload", _freeze(payload))
        object.__setattr__(self, "state_after_sha256", _raw_hash(self.state_after_sha256))
        object.__setattr__(self, "cash_delta", _finite(self.cash_delta, "cash_delta"))
        if self.trade_payload is not None:
            if not isinstance(self.trade_payload, Mapping):
                raise FinancialSettlementError("trade_payload doit être un objet")
            object.__setattr__(self, "trade_payload", _freeze(_copy_payload(self.trade_payload)))
        if not isinstance(self.zero_effect, bool):
            raise FinancialSettlementError("zero_effect doit être bool")
        object.__setattr__(self, "result_sha256", _sha256(self.result_content()))

    def result_content(self) -> dict[str, Any]:
        """Return every semantic result field used by the application ledger."""

        return {
            "cash_delta": self.cash_delta,
            "economic_effect_at": self.economic_effect_at,
            "fee_asset": self.fee_asset,
            "intent_id": self.intent_id,
            "local_order_id": self.local_order_id,
            "plan_key": self.plan_key,
            "quantity": self.quantity,
            "settlement_key": self.settlement_key,
            "state_after_payload": _plain(self.state_after_payload),
            "state_after_sha256": self.state_after_sha256,
            "state_before_sha256": self.state_before_sha256,
            "total_fee": self.total_fee,
            "total_notional": self.total_notional,
            "transition_type": FinancialTransitionType(self.transition_type).value,
            "version": self.version,
            "zero_effect": self.zero_effect,
            "trade_payload": _plain(self.trade_payload) if self.trade_payload is not None else None,
        }

    def to_persistence_payload(self) -> dict[str, Any]:
        payload = self.result_content()
        payload["result_sha256"] = self.result_sha256
        return payload

    @classmethod
    def from_persistence_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        expected_result_sha256: str | None = None,
    ) -> FinancialSettlementResult:
        if not isinstance(payload, Mapping):
            raise FinancialSettlementError("settlement result payload non objet")
        stored_hash = payload.get("result_sha256")
        result = cls(
            settlement_key=cast(str, payload.get("settlement_key")),
            version=cast(int, payload.get("version")),
            local_order_id=cast(int, payload.get("local_order_id")),
            intent_id=cast(str, payload.get("intent_id")),
            plan_key=cast(str, payload.get("plan_key")),
            transition_type=cast(str, payload.get("transition_type")),
            quantity=cast(float, payload.get("quantity")),
            total_notional=cast(float, payload.get("total_notional")),
            total_fee=cast(float, payload.get("total_fee")),
            fee_asset=cast(str, payload.get("fee_asset")),
            economic_effect_at=cast(str, payload.get("economic_effect_at")),
            state_before_sha256=cast(str, payload.get("state_before_sha256")),
            state_after_payload=cast(Mapping[str, Any], payload.get("state_after_payload")),
            state_after_sha256=cast(str, payload.get("state_after_sha256")),
            cash_delta=cast(float, payload.get("cash_delta")),
            trade_payload=cast(Mapping[str, Any] | None, payload.get("trade_payload")),
            zero_effect=cast(bool, payload.get("zero_effect")),
        )
        if not isinstance(stored_hash, str) or stored_hash != result.result_sha256:
            raise FinancialSettlementError("FINANCIAL_SETTLEMENT_RESULT_HASH_CONFLICT")
        if expected_result_sha256 is not None and expected_result_sha256 != result.result_sha256:
            raise FinancialSettlementError("FINANCIAL_SETTLEMENT_RESULT_HASH_CONFLICT")
        return result


def financial_settlement_application_key(
    *,
    application_version: int,
    local_order_id: int,
    intent_id: str,
    plan_key: str,
    settlement_key: str,
) -> str:
    """Return the stable identity of one settlement application claim."""

    return "settleapp-" + _sha256(
        {
            "application_version": application_version,
            "intent_id": intent_id,
            "local_order_id": local_order_id,
            "plan_key": plan_key,
            "settlement_key": settlement_key,
        }
    )


@dataclass(frozen=True)
class PersistedFinancialSettlementApplication:
    """One immutable durable claim that a settlement was applied."""

    application_key: str
    application_version: int
    settlement_key: str
    local_order_id: int
    intent_id: str
    plan_key: str
    state_before_sha256: str
    state_after_sha256: str
    result: FinancialSettlementResult
    applied_at: str

    def __post_init__(self) -> None:
        for name in (
            "application_key",
            "settlement_key",
            "intent_id",
            "plan_key",
            "state_before_sha256",
            "state_after_sha256",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if (
            isinstance(self.application_version, bool)
            or not isinstance(self.application_version, int)
            or self.application_version != FINANCIAL_SETTLEMENT_APPLICATION_VERSION
        ):
            raise FinancialSettlementError("application_version de settlement inconnue")
        if (
            isinstance(self.local_order_id, bool)
            or not isinstance(self.local_order_id, int)
            or self.local_order_id <= 0
        ):
            raise FinancialSettlementError("local_order_id invalide")
        object.__setattr__(self, "applied_at", _timestamp(self.applied_at, "applied_at"))
        if not isinstance(self.result, FinancialSettlementResult):
            raise FinancialSettlementError("settlement result invalide")
        if (
            self.result.settlement_key != self.settlement_key
            or self.result.local_order_id != self.local_order_id
            or self.result.intent_id != self.intent_id
            or self.result.plan_key != self.plan_key
            or self.result.state_before_sha256 != self.state_before_sha256
            or self.result.state_after_sha256 != self.state_after_sha256
        ):
            raise FinancialSettlementError("EXTERNAL_SETTLEMENT_APPLICATION_CONFLICT")
        expected_key = financial_settlement_application_key(
            application_version=self.application_version,
            local_order_id=self.local_order_id,
            intent_id=self.intent_id,
            plan_key=self.plan_key,
            settlement_key=self.settlement_key,
        )
        if self.application_key != expected_key:
            raise FinancialSettlementError("EXTERNAL_SETTLEMENT_APPLICATION_KEY_CONFLICT")


@dataclass(frozen=True)
class FinancialSettlementCommitResult:
    """Result of one atomic settlement application attempt."""

    application: PersistedFinancialSettlementApplication
    applied: bool
    already_applied: bool
    trade_inserted: bool
    event_id: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.application, PersistedFinancialSettlementApplication):
            raise FinancialSettlementError("settlement application invalide")
        if not isinstance(self.applied, bool) or not isinstance(self.already_applied, bool):
            raise FinancialSettlementError("flags d'application invalides")
        if self.applied == self.already_applied:
            raise FinancialSettlementError("état d'application ambigu")
        if not isinstance(self.trade_inserted, bool):
            raise FinancialSettlementError("trade_inserted invalide")
        if self.event_id is not None and (
            isinstance(self.event_id, bool)
            or not isinstance(self.event_id, int)
            or self.event_id <= 0
        ):
            raise FinancialSettlementError("event_id invalide")
        if self.applied and self.event_id is None:
            raise FinancialSettlementError("settlement application sans événement")
        if self.already_applied and (self.event_id is not None or self.trade_inserted):
            raise FinancialSettlementError("rejeu settlement avec écriture")


def _position(slot: Mapping[str, Any]) -> Mapping[str, Any]:
    value = slot.get("position")
    if not isinstance(value, Mapping):
        raise FinancialSettlementError("FINANCIAL_PRE_STATE_CONFLICT")
    return value


def calculate_financial_order_settlement(
    settlement: ExternalOrderSettlement,
    persisted_plan: PersistedFinancialApplicationPlan,
) -> FinancialSettlementResult:
    """Calculate a settlement from the original durable pre-state only."""

    if not isinstance(settlement, ExternalOrderSettlement):
        raise TypeError("settlement doit être ExternalOrderSettlement")
    if not isinstance(persisted_plan, PersistedFinancialApplicationPlan):
        raise TypeError("persisted_plan doit être PersistedFinancialApplicationPlan")
    if not settlement.completeness.is_complete:
        reasons = (
            ",".join(settlement.completeness.incomplete_reasons())
            or "SETTLEMENT_COMPLETENESS_NOT_PROVEN"
        )
        raise FinancialSettlementError(reasons)
    if (
        persisted_plan.local_order_id != settlement.local_order_id
        or persisted_plan.intent_id != settlement.intent_id
    ):
        raise FinancialSettlementError("SETTLEMENT_PLAN_BINDING_CONFLICT")
    if not settlement.fills:
        raise FinancialSettlementError("SETTLEMENT_ZERO_EFFECT_NOT_AUTHORIZED_FOR_APPLICATION")
    if settlement.total_fee is None or settlement.fee_asset != SETTLEMENT_FEE_ASSET:
        raise FinancialSettlementError("FINANCIAL_SETTLEMENT_FEE_INCOMPLETE")
    plan = persisted_plan.plan
    tolerance = max(SETTLEMENT_QUANTITY_TOLERANCE, plan.requested_qty * 1e-9)
    if settlement.total_qty > plan.requested_qty + tolerance:
        raise FinancialSettlementError("FINANCIAL_SETTLEMENT_QUANTITY_EXCEEDS_PLAN")
    if any(row.fee is None or row.fee_asset != SETTLEMENT_FEE_ASSET for row in settlement.fills):
        raise FinancialSettlementError("FINANCIAL_SETTLEMENT_FEE_INCOMPLETE")

    payload = _copy_payload(plan.pre_state_payload)
    target = _slot(payload, plan.identity.slot)
    transition = plan.identity.transition_type
    position = target.get("position")
    total_fee = settlement.total_fee
    total_qty = settlement.total_qty
    total_notional = settlement.total_notional
    trade_payload: dict[str, Any] | None = None

    if transition in {FinancialTransitionType.ENTER_LONG, FinancialTransitionType.ENTER_SHORT}:
        if position is not None or total_qty <= 0:
            raise FinancialSettlementError("FINANCIAL_PRE_STATE_CONFLICT")
        if plan.entry_direction not in {-1, 1} or plan.entry_stop_price is None:
            raise FinancialSettlementError("FINANCIAL_PLAN_ENTRY_CONTEXT_MISSING")
        vwap = total_notional / total_qty
        target["cash"] = float(target["cash"]) - total_fee
        target["entry_fee"] = total_fee
        target["position"] = {
            "entry_time": settlement.earliest_fill_at,
            "entry_price": vwap,
            "qty": total_qty,
            "stop_price": float(plan.entry_stop_price),
            "direction": int(plan.entry_direction),
            "bars_held": 0,
            "best_close": vwap,
            "initial_qty": total_qty,
            "last_add_price": vwap,
            "pyramid_adds": 0,
        }
        cash_delta = -total_fee
        economic_at = settlement.earliest_fill_at
    elif transition == FinancialTransitionType.ADD:
        current = _position(target)
        pre_qty = _finite(current.get("qty"), "position.qty", positive=True)
        pre_price = _finite(current.get("entry_price"), "position.entry_price", positive=True)
        new_qty = pre_qty + total_qty
        vwap = (pre_qty * pre_price + total_notional) / new_qty
        updated = dict(current)
        updated["entry_price"] = vwap
        updated["qty"] = new_qty
        updated["last_add_price"] = total_notional / total_qty
        updated["pyramid_adds"] = int(current["pyramid_adds"]) + 1
        target["position"] = updated
        target["cash"] = float(target["cash"]) - total_fee
        target["entry_fee"] = float(target["entry_fee"]) + total_fee
        cash_delta = -total_fee
        economic_at = settlement.earliest_fill_at
    elif transition == FinancialTransitionType.EXIT:
        current = _position(target)
        pre_qty = _finite(current.get("qty"), "position.qty", positive=True)
        pre_price = _finite(current.get("entry_price"), "position.entry_price", positive=True)
        direction = current.get("direction")
        if direction not in {-1, 1} or total_qty > pre_qty + tolerance:
            raise FinancialSettlementError("FINANCIAL_SETTLEMENT_POSITION_CONFLICT")
        pre_entry_fee = _finite(target.get("entry_fee"), "entry_fee")
        gross_pnl = sum(
            int(direction) * row.quantity * (row.price - pre_price) for row in settlement.fills
        )
        entry_fee_share = pre_entry_fee * (total_qty / pre_qty)
        cash_delta = gross_pnl - total_fee
        target["cash"] = float(target["cash"]) + cash_delta
        remaining = pre_qty - total_qty
        if remaining <= max(tolerance, pre_qty * 1e-9):
            target["position"] = None
            target["entry_fee"] = 0.0
        else:
            updated = dict(current)
            updated["qty"] = remaining
            target["position"] = updated
            target["entry_fee"] = pre_entry_fee * (remaining / pre_qty)
        trade_payload = {
            "exit_ts": settlement.latest_fill_at,
            "entry_ts": _timestamp(current["entry_time"], "entry_time"),
            "strategy": plan.identity.slot,
            "direction": "LONG" if int(direction) == 1 else "SHORT",
            "qty": total_qty,
            "entry_price": pre_price,
            "exit_price": total_notional / total_qty,
            "pnl": gross_pnl - total_fee - entry_fee_share,
            "bars_held": int(current["bars_held"]),
            "reason": plan.reason,
        }
        economic_at = settlement.latest_fill_at
    else:
        raise FinancialSettlementError("REDUCE_RUNTIME_PATH_NOT_CURRENTLY_ACTIVE")

    if economic_at is None:
        raise FinancialSettlementError("SETTLEMENT_ECONOMIC_TIME_MISSING")
    validate_trend_state(payload)
    after_hash = sha256_json(payload)
    return FinancialSettlementResult(
        settlement_key=settlement.settlement_key,
        version=SETTLEMENT_VERSION,
        local_order_id=settlement.local_order_id,
        intent_id=settlement.intent_id,
        plan_key=plan.plan_key,
        transition_type=transition,
        quantity=total_qty,
        total_notional=total_notional,
        total_fee=total_fee,
        fee_asset=SETTLEMENT_FEE_ASSET,
        economic_effect_at=economic_at,
        state_before_sha256=plan.pre_state_sha256,
        state_after_payload=payload,
        state_after_sha256=after_hash,
        cash_delta=cash_delta,
        trade_payload=trade_payload,
        zero_effect=False,
    )


__all__ = [
    "ExternalOrderSettlement",
    "ExternalSettlementFillRow",
    "FinancialSettlementCommitResult",
    "FinancialSettlementError",
    "FinancialSettlementResult",
    "FINANCIAL_SETTLEMENT_APPLICATION_VERSION",
    "SETTLEMENT_COMPLETENESS_VERSION",
    "SETTLEMENT_FEE_ASSET",
    "SETTLEMENT_RESPONSE_LIMIT",
    "SETTLEMENT_RETENTION_LIMIT",
    "SETTLEMENT_VERSION",
    "SettlementCompletenessProof",
    "calculate_financial_order_settlement",
    "financial_settlement_application_key",
    "PersistedFinancialSettlementApplication",
]
