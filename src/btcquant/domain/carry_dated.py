"""Shared domain contracts for a public-data, PAPER-only dated BTC carry.

This module deliberately contains no venue I/O and no execution capability.
Financial values stay Decimal and all timestamps must be timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal


InstrumentKind = Literal["spot", "dated_future"]
SettlementKind = Literal["daily", "expiry"]
DecisionAction = Literal["OPEN", "WAIT"]
SpotFeeCurrencyAssumption = Literal["UNKNOWN_WORST_CASE", "BASE", "QUOTE"]
BOOK_FRESHNESS_SECONDS = Decimal("15")


def _decimal(value: Decimal | int | str, name: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite Decimal") from exc
    if not result.is_finite() or (result < 0 if allow_zero else result <= 0):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'} and finite")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be expressed in UTC")
    return value.astimezone(UTC)


def _signed_decimal(value: Decimal | int | str, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite Decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _decimal_string(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _iso_string(value: datetime | None) -> str | None:
    return None if value is None else _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class BookLevel:
    """One public order-book level; quantity is in the instrument's base units."""

    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", _decimal(self.price, "price"))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity"))


@dataclass(frozen=True, slots=True)
class DatedInstrument:
    venue: str
    instrument_id: str
    kind: InstrumentKind
    base: str
    quote: str
    settlement_currency: str | None
    contract_size: Decimal
    amount_step: Decimal
    min_amount: Decimal
    tick_size: Decimal
    expiration_utc: datetime | None
    active: bool
    maker_commission: Decimal | None
    taker_commission: Decimal | None
    fee_currency: str | None
    fee_provenance: str
    metadata_timestamp: datetime
    metadata_hash: str
    instrument_state: str
    source: str

    def __post_init__(self) -> None:
        if self.kind not in ("spot", "dated_future"):
            raise ValueError("unsupported dated carry instrument kind")
        if not all((self.venue, self.instrument_id, self.base, self.quote, self.source)):
            raise ValueError("instrument identity and source are required")
        for name in ("contract_size", "amount_step", "min_amount", "tick_size"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        for name in ("maker_commission", "taker_commission"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _signed_decimal(value, name))
        object.__setattr__(self, "metadata_timestamp", _utc(self.metadata_timestamp, "metadata_timestamp"))
        if self.expiration_utc is not None:
            object.__setattr__(self, "expiration_utc", _utc(self.expiration_utc, "expiration_utc"))
        if not self.fee_provenance or not self.metadata_hash or not self.instrument_state:
            raise ValueError("fee provenance, metadata hash and instrument state are required")
        if self.kind == "spot" and self.expiration_utc is not None:
            raise ValueError("spot instruments cannot have an expiry")
        if self.kind == "dated_future" and self.expiration_utc is None:
            raise ValueError("dated futures require an expiry")


@dataclass(frozen=True, slots=True)
class DatedBook:
    instrument_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    market_timestamp: datetime
    received_timestamp: datetime
    source: str
    instrument_state: str
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    sequence: int | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.instrument_id or not self.source:
            raise ValueError("book instrument_id and source are required")
        if not all(isinstance(level, BookLevel) for level in (*self.bids, *self.asks)):
            raise TypeError("book levels must be BookLevel instances")
        object.__setattr__(self, "market_timestamp", _utc(self.market_timestamp, "market_timestamp"))
        object.__setattr__(self, "received_timestamp", _utc(self.received_timestamp, "received_timestamp"))
        for name in ("mark_price", "index_price"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name))
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("book sequence must be non-negative")

    def quality_reasons(
        self,
        *,
        now: datetime | None = None,
        max_age_seconds: Decimal = BOOK_FRESHNESS_SECONDS,
    ) -> tuple[str, ...]:
        current = _utc(now or datetime.now(UTC), "now")
        reasons: list[str] = []
        if self.instrument_state != "open":
            reasons.append("INSTRUMENT_NOT_OPEN")
        if not self.bids or not self.asks:
            reasons.append("BOOK_SIDE_EMPTY")
        elif self.bids[0].price >= self.asks[0].price:
            reasons.append("BOOK_CROSSED_OR_LOCKED")
        if self.market_timestamp > self.received_timestamp:
            reasons.append("MARKET_TIME_IN_FUTURE_OF_RECEIPT")
        age = Decimal(str((current - self.market_timestamp).total_seconds()))
        if age > max_age_seconds:
            reasons.append("BOOK_STALE")
        if age < Decimal("-1"):
            reasons.append("CLOCK_SKEW")
        return tuple(dict.fromkeys(reasons))

    def is_tradable(
        self,
        *,
        now: datetime | None = None,
        max_age_seconds: Decimal = BOOK_FRESHNESS_SECONDS,
    ) -> bool:
        return not self.quality_reasons(now=now, max_age_seconds=max_age_seconds)


@dataclass(frozen=True, slots=True)
class DatedSettlement:
    venue: str
    instrument_id: str
    settlement_key: str
    kind: SettlementKind
    period_start: datetime
    period_end: datetime
    official_index_price: Decimal
    source: str
    received_at: datetime
    content_hash: str
    correction_version: str
    event_timestamp: datetime

    def __post_init__(self) -> None:
        if self.kind not in ("daily", "expiry"):
            raise ValueError("unsupported settlement kind")
        if not all((self.venue, self.instrument_id, self.settlement_key, self.source, self.content_hash)):
            raise ValueError("settlement identity, provenance and hash are required")
        for name in ("period_start", "period_end", "received_at", "event_timestamp"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.period_start >= self.period_end:
            raise ValueError("settlement period must be positive")
        if not self.period_start <= self.event_timestamp <= self.period_end:
            raise ValueError("settlement event timestamp must lie in its period")
        object.__setattr__(self, "official_index_price", _decimal(self.official_index_price, "official_index_price"))
        if not self.correction_version:
            raise ValueError("settlement correction/version identity is required")


@dataclass(frozen=True, slots=True)
class DatedCarryDecision:
    """Auditable, serializable decision evidence shared by engine and research."""

    decision_id: str
    decision_at: datetime
    action: DecisionAction
    reason_code: str
    reason_message: str
    as_of: datetime
    selected_instrument_id: str | None
    expiry_utc: datetime | None
    quantity: Decimal
    spot_gross_quantity: Decimal
    spot_expected_net_quantity: Decimal
    expected_hedge_residual: Decimal
    spot_fee_currency_assumption: SpotFeeCurrencyAssumption
    spot_estimated_price: Decimal | None
    future_estimated_price: Decimal | None
    spot_depth_quantity: Decimal
    future_depth_quantity: Decimal
    capital: Decimal
    free_cash: Decimal
    spot_cash_required: Decimal
    margin_cash: Decimal
    stress_cash: Decimal
    fees: Decimal | None
    gross_premium: Decimal
    net_before_reserve: Decimal | None
    risk_reserve: Decimal
    net_after_reserve: Decimal | None
    policy_version: str
    spot_market_timestamp: datetime | None
    future_market_timestamp: datetime | None
    spot_fee: Decimal | None
    future_entry_fee: Decimal | None
    delivery_fee_estimate: Decimal | None
    exit_fee_estimate: Decimal | None
    exit_execution_reserve: Decimal
    initial_margin_rate: Decimal | None
    maintenance_margin_rate: Decimal | None
    total_cash_required: Decimal
    cash_after_entry: Decimal
    fee_model_version: str
    margin_model_version: str
    source_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.decision_id or not self.reason_code or not self.policy_version:
            raise ValueError("decision identity, reason code and policy version are required")
        if self.action not in ("OPEN", "WAIT"):
            raise ValueError("decision action must be OPEN or WAIT")
        if self.spot_fee_currency_assumption not in ("UNKNOWN_WORST_CASE", "BASE", "QUOTE"):
            raise ValueError("unsupported spot fee currency assumption")
        for name in ("decision_at", "as_of", "expiry_utc", "spot_market_timestamp", "future_market_timestamp"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value, name))
        if self.action == "OPEN" and (not self.selected_instrument_id or self.expiry_utc is None):
            raise ValueError("OPEN decision requires a selected dated instrument and expiry")
        for name in (
            "quantity",
            "spot_gross_quantity",
            "spot_expected_net_quantity",
            "spot_depth_quantity",
            "future_depth_quantity",
            "capital",
            "free_cash",
            "spot_cash_required",
            "margin_cash",
            "stress_cash",
            "risk_reserve",
            "exit_execution_reserve",
            "total_cash_required",
            "cash_after_entry",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, allow_zero=True))
        for name in (
            "fees",
            "spot_fee",
            "future_entry_fee",
            "delivery_fee_estimate",
            "exit_fee_estimate",
            "initial_margin_rate",
            "maintenance_margin_rate",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name, allow_zero=True))
        for name in ("spot_estimated_price", "future_estimated_price"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _decimal(value, name))
        for name in ("gross_premium", "net_before_reserve", "net_after_reserve"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _signed_decimal(value, name))
        object.__setattr__(
            self,
            "expected_hedge_residual",
            _signed_decimal(self.expected_hedge_residual, "expected_hedge_residual"),
        )
        if not self.fee_model_version or not self.margin_model_version:
            raise ValueError("fee and margin model versions are required")
        if not all(self.source_hashes):
            raise ValueError("source hashes must be non-empty when provided")

    def to_dict(self) -> dict[str, Any]:
        """Serialize money/quantities as exact decimal strings and times as UTC."""

        decimal_fields = (
            "quantity",
            "spot_gross_quantity",
            "spot_expected_net_quantity",
            "expected_hedge_residual",
            "spot_estimated_price",
            "future_estimated_price",
            "spot_depth_quantity",
            "future_depth_quantity",
            "capital",
            "free_cash",
            "spot_cash_required",
            "margin_cash",
            "stress_cash",
            "fees",
            "gross_premium",
            "net_before_reserve",
            "risk_reserve",
            "net_after_reserve",
            "spot_fee",
            "future_entry_fee",
            "delivery_fee_estimate",
            "exit_fee_estimate",
            "exit_execution_reserve",
            "initial_margin_rate",
            "maintenance_margin_rate",
            "total_cash_required",
            "cash_after_entry",
        )
        time_fields = (
            "decision_at",
            "as_of",
            "expiry_utc",
            "spot_market_timestamp",
            "future_market_timestamp",
        )
        payload: dict[str, Any] = {
            name: _decimal_string(getattr(self, name)) for name in decimal_fields
        }
        payload.update({name: _iso_string(getattr(self, name)) for name in time_fields})
        payload.update(
            {
                "decision_id": self.decision_id,
                "action": self.action,
                "spot_fee_currency_assumption": self.spot_fee_currency_assumption,
                "reason_code": self.reason_code,
                "reason_message": self.reason_message,
                "selected_instrument_id": self.selected_instrument_id,
                "policy_version": self.policy_version,
                "fee_model_version": self.fee_model_version,
                "margin_model_version": self.margin_model_version,
                "source_hashes": list(self.source_hashes),
            }
        )
        return payload


__all__ = [
    "BOOK_FRESHNESS_SECONDS",
    "BookLevel",
    "DatedBook",
    "DatedCarryDecision",
    "DatedInstrument",
    "DatedSettlement",
    "InstrumentKind",
    "DecisionAction",
    "SettlementKind",
    "SpotFeeCurrencyAssumption",
]
