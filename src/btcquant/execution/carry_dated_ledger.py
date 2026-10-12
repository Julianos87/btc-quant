"""Decimal PAPER ledger for one spot/dated-future BTC carry cycle.

The ledger has no venue-order methods.  It prices simulated fills only from the
supplied public book levels and persists every financial transition through the
dedicated ``carry_dated`` StateStore event/checkpoint transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

from btcquant.domain.carry_dated import (
    canonical_market_book_snapshot,
    canonical_payload_sha256,
)

from .errors import AccountingIdentityCollision
from .state_store import StateStore

ENGINE_NAME = "carry_dated"
STRATEGY_NAME = "carry_btc_dated"
CHECKPOINT_SCHEMA_VERSION = 1
BOOK_EVIDENCE_PROTOCOL_VERSION = "carry-dated-market-evidence-v2"
BOOK_DEPTH_LEVELS = 20
DEFAULT_MARKET_EVIDENCE_QUOTA_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_MARKET_EVIDENCE_MIN_FREE_BYTES = 10 * 1024 * 1024 * 1024


class DatedLedgerError(ValueError):
    """A PAPER ledger transition violates a quantity or cash invariant."""


def decimal(value: Decimal | int | str, *, field: str = "value") -> Decimal:
    if isinstance(value, bool):
        raise DatedLedgerError(f"{field} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise DatedLedgerError(f"{field} must be a finite decimal") from error
    if not result.is_finite():
        raise DatedLedgerError(f"{field} must be finite")
    return result


def decimal_string(value: Decimal | int | str) -> str:
    result = decimal(value)
    if result == 0:
        return "0"
    return format(result.normalize(), "f")


def utc_string(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != UTC.utcoffset(current):
        raise DatedLedgerError("timestamp must be timezone-aware UTC")
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def next_daily_settlement_after(opened_at: datetime) -> datetime:
    """Return the first 08:00 UTC settlement boundary strictly after entry."""

    stamp = opened_at.astimezone(UTC)
    boundary = stamp.replace(hour=8, minute=0, second=0, microsecond=0)
    if stamp >= boundary:
        boundary += timedelta(days=1)
    return boundary


def _book_snapshot(book: Any) -> dict[str, Any]:
    return canonical_market_book_snapshot(book)


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    return canonical_payload_sha256(payload)


def state_decimal(state: Mapping[str, Any], key: str) -> Decimal:
    raw = state.get(key)
    if raw is None:
        return Decimal("0")
    return decimal(raw, field=key)


def reserved_total(state: Mapping[str, Any]) -> Decimal:
    return state_decimal(state, "reserved_margin") + state_decimal(state, "stress_cash_buffer")


def initial_checkpoint(
    capital: Decimal,
    *,
    now: datetime | None = None,
    market_evidence_quota_bytes: int = DEFAULT_MARKET_EVIDENCE_QUOTA_BYTES,
) -> dict[str, Any]:
    amount = decimal(capital, field="capital")
    if amount <= 0:
        raise DatedLedgerError("capital must be positive")
    if (
        not isinstance(market_evidence_quota_bytes, int)
        or isinstance(market_evidence_quota_bytes, bool)
        or market_evidence_quota_bytes < 1
    ):
        raise DatedLedgerError("market evidence quota must be a positive integer")
    stamp = utc_string(now)
    return {
        "strategy": STRATEGY_NAME,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "market_evidence_protocol_version": BOOK_EVIDENCE_PROTOCOL_VERSION,
        "book_depth_levels": BOOK_DEPTH_LEVELS,
        "market_evidence_quota_bytes": market_evidence_quota_bytes,
        "paper_only": True,
        "market_evidence_gap": None,
        "status": "FLAT",
        "instrument": None,
        "expiry_utc": None,
        "capital": decimal_string(amount),
        "cash_total": decimal_string(amount),
        "free_cash": decimal_string(amount),
        "reserved_margin": "0",
        "stress_cash_buffer": "0",
        "initial_margin_rate": None,
        "maintenance_margin_rate": None,
        "maintenance_margin": "0",
        "unrealized_short_loss_to_mark": "0",
        "derivative_cash_after_unrealized_short_loss": decimal_string(amount),
        "cash_after_margin_stress": decimal_string(amount),
        "spot_qty": "0",
        "spot_dust_qty": "0",
        "spot_dust_avg_entry": None,
        "spot_dust_mark": None,
        "spot_dust_mark_timestamp": None,
        "spot_dust_instrument_id": None,
        "future_qty": "0",
        "hedge_residual": "0",
        "hedge_tolerance": "0",
        "spot_fee_currency_assumption": None,
        "spot_fee_currency_applied_scenario": None,
        "expected_hedge_residual": "0",
        "spot_avg_entry": None,
        "future_avg_entry": None,
        "future_reference_price": None,
        "last_settlement_key": None,
        "last_settlement_index": None,
        "last_settlement_period_end": None,
        "settlements_applied": 0,
        "pnl_realized": "0",
        "pnl_unrealized": "0",
        "equity": decimal_string(amount),
        "fees_paid": "0",
        "decision": None,
        "decision_at": None,
        "reason_code": None,
        "reason_message": None,
        "market": {"spot_timestamp": None, "future_timestamp": None},
        "spot_market_timestamp": None,
        "future_market_timestamp": None,
        "market_timestamps": {"spot": None, "future": None},
        "accounting_uncertain": False,
        "incident": None,
        "pending_saga_ids": [],
        "active_saga_id": None,
        "fill_sequences": {"spot": 0, "future": 0},
        "dust_sweep_sequence": 0,
        "delivery_fee_estimate": None,
        "delivery_fee_rate": None,
        "delivery_fee_applied": None,
        "delivery_fee_model_version": None,
        "spot_entry_fee_rate": None,
        "future_taker_fee_rate": None,
        "spot_exit_fee_rate": None,
        "settlement_audit_start_key": None,
        "expiry_spot_liquidation_late": None,
        "expiry_spot_liquidation_delay_seconds": None,
        "expiry_spot_liquidation_book_timestamp": None,
        "fee_model_version": None,
        "margin_model_version": None,
        "last_updated_at": stamp,
    }


@dataclass(frozen=True, slots=True)
class BookFill:
    """A depth-limited PAPER fill, with quantity expressed in BTC base units."""

    fill_id: str
    instrument_id: str
    leg: str
    side: str
    requested_qty: Decimal
    filled_qty: Decimal
    average_price: Decimal | None
    fee_currency: str
    fee_amount: Decimal
    fee_quote_equivalent: Decimal
    market_timestamp: str
    book_received_timestamp: str
    book_source: str
    book_sequence: int | None
    book_hash: str | None
    consumed_levels: tuple[tuple[Decimal, Decimal], ...]
    book_snapshot: dict[str, Any]
    book_snapshot_sha256: str
    market_evidence_id: str | None
    fee_rate: Decimal

    @property
    def notional(self) -> Decimal:
        if self.average_price is None:
            return Decimal("0")
        return self.filled_qty * self.average_price

    @property
    def fee(self) -> Decimal:
        """Fee value converted to quote currency for PnL reporting."""

        return self.fee_quote_equivalent

    @property
    def net_base_qty(self) -> Decimal:
        if self.leg == "spot" and self.side == "BUY" and self.fee_currency == "BASE":
            return self.filled_qty - self.fee_amount
        return self.filled_qty

    @property
    def base_consumed(self) -> Decimal:
        if self.leg == "spot" and self.side == "SELL" and self.fee_currency == "BASE":
            return self.filled_qty + self.fee_amount
        return self.filled_qty

    @property
    def complete(self) -> bool:
        return self.filled_qty == self.requested_qty

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "execution_kind": "PAPER_DEPTH_SIMULATION",
            "fill_id": self.fill_id,
            "instrument_id": self.instrument_id,
            "leg": self.leg,
            "side": self.side,
            "requested_qty": decimal_string(self.requested_qty),
            "filled_qty": decimal_string(self.filled_qty),
            "average_price": (
                decimal_string(self.average_price) if self.average_price is not None else None
            ),
            "fee_currency": self.fee_currency,
            "fee_amount": decimal_string(self.fee_amount),
            "fee_quote_equivalent": decimal_string(self.fee_quote_equivalent),
            "market_timestamp": self.market_timestamp,
            "book_received_timestamp": self.book_received_timestamp,
            "book_source": self.book_source,
            "book_sequence": self.book_sequence,
            "book_hash": self.book_hash,
            "fee_rate": decimal_string(self.fee_rate),
            "consumed_levels": [
                {"price": decimal_string(price), "quantity": decimal_string(quantity)}
                for price, quantity in self.consumed_levels
            ],
            "book_snapshot_sha256": self.book_snapshot_sha256,
            "market_evidence_id": self.market_evidence_id,
        }
        # Durable runner snapshots live once in the market evidence archive.
        # Keep an inline copy only for protective exits whose evidence sink is
        # unavailable; those fills must remain auditable while risk is managed.
        if not self.market_evidence_id:
            payload["book_snapshot"] = self.book_snapshot
        return payload


def fill_from_book(
    *,
    book: Any,
    leg: str,
    side: str,
    quantity: Decimal,
    fee_rate: Decimal,
    fee_currency: str,
    fill_id: str,
    market_evidence_id: str | None = None,
) -> BookFill:
    """Consume asks for BUY or bids for SELL, never using the mark or mid."""

    requested = decimal(quantity, field="quantity")
    rate = decimal(fee_rate, field="fee_rate")
    if requested <= 0 or rate < 0:
        raise DatedLedgerError("quantity must be positive and fee_rate non-negative")
    normalized_side = side.upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise DatedLedgerError("side must be BUY or SELL")
    currency = fee_currency.upper()
    if currency not in {"BASE", "QUOTE"}:
        raise DatedLedgerError("fee_currency must be an explicit BASE or QUOTE scenario")
    levels = book.asks if normalized_side == "BUY" else book.bids
    remaining = requested
    filled = Decimal("0")
    notional = Decimal("0")
    consumed_levels: list[tuple[Decimal, Decimal]] = []
    for level in levels:
        level_price = decimal(level.price, field="level.price")
        level_qty = decimal(level.quantity, field="level.quantity")
        take = min(remaining, level_qty)
        if take <= 0:
            continue
        filled += take
        notional += take * level_price
        consumed_levels.append((level_price, take))
        remaining -= take
        if remaining == 0:
            break
    average = notional / filled if filled else None
    fee_quote = notional * rate
    fee_amount = fee_quote / average if currency == "BASE" and average is not None else fee_quote
    snapshot = _book_snapshot(book)
    return BookFill(
        fill_id=fill_id,
        instrument_id=book.instrument_id,
        leg=leg,
        side=normalized_side,
        requested_qty=requested,
        filled_qty=filled,
        average_price=average,
        fee_currency=currency,
        fee_amount=fee_amount,
        fee_quote_equivalent=fee_quote,
        market_timestamp=utc_string(book.market_timestamp),
        book_received_timestamp=utc_string(book.received_timestamp),
        book_source=book.source,
        book_sequence=book.sequence,
        book_hash=getattr(book, "content_hash", None),
        consumed_levels=tuple(consumed_levels),
        book_snapshot=snapshot,
        book_snapshot_sha256=_canonical_hash(snapshot),
        market_evidence_id=market_evidence_id or None,
        fee_rate=rate,
    )


def common_quantity_step(spot_step: Decimal, future_step: Decimal) -> Decimal:
    """Return the smallest quantity that is a multiple of both decimal steps."""

    spot = decimal(spot_step, field="spot_step")
    future = decimal(future_step, field="future_step")
    if spot <= 0 or future <= 0:
        raise DatedLedgerError("instrument amount steps must be positive")
    exponent = int(min(spot.as_tuple().exponent, future.as_tuple().exponent))
    scale = Decimal(10) ** (-exponent)
    spot_int = int(spot * scale)
    future_int = int(future * scale)
    # lcm(a,b) = abs(a*b) / gcd(a,b); use Euclid without a float conversion.
    a, b = spot_int, future_int
    while b:
        a, b = b, a % b
    lcm = abs(spot_int * future_int) // a
    return Decimal(lcm) / scale


def floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    amount = decimal(quantity, field="quantity")
    increment = decimal(step, field="step")
    if amount < 0 or increment <= 0:
        raise DatedLedgerError("quantity must be non-negative and step positive")
    return (amount / increment).to_integral_value(rounding=ROUND_DOWN) * increment


class CarryDatedLedger:
    """Persistence and accounting transitions for an isolated dated-carry DB."""

    def __init__(
        self,
        store: StateStore,
        capital: Decimal,
        *,
        market_evidence_quota_bytes: int = DEFAULT_MARKET_EVIDENCE_QUOTA_BYTES,
        market_evidence_recorder: Any | None = None,
    ) -> None:
        self.store = store
        self.market_evidence_recorder = market_evidence_recorder
        if (
            not isinstance(market_evidence_quota_bytes, int)
            or isinstance(market_evidence_quota_bytes, bool)
            or market_evidence_quota_bytes < 1
        ):
            raise DatedLedgerError("market evidence quota must be a positive integer")
        self.market_evidence_quota_bytes = market_evidence_quota_bytes
        expected_capital = decimal(capital, field="capital")
        persisted = store.load_engine_state(ENGINE_NAME)
        if persisted is None:
            self.state = initial_checkpoint(
                expected_capital,
                market_evidence_quota_bytes=market_evidence_quota_bytes,
            )
            self.store.save_engine_state(
                ENGINE_NAME,
                self.state,
                event_type="dated_carry_initialized",
                event_payload={"capital": decimal_string(expected_capital), "paper_only": True},
                event_aggregate_type="dated_carry",
                event_aggregate_id="initialization",
            )
        else:
            self._validate_checkpoint(persisted)
            if state_decimal(persisted, "capital") != expected_capital:
                raise DatedLedgerError(
                    "configured capital differs from the dedicated PAPER checkpoint"
                )
            self.state = persisted
        config_payload = {
            "protocol_version": BOOK_EVIDENCE_PROTOCOL_VERSION,
            "book_depth_levels": BOOK_DEPTH_LEVELS,
            "market_evidence_quota_bytes": market_evidence_quota_bytes,
        }
        configured_state = dict(self.state)
        configured_state.update(config_payload)
        self._commit(
            event_type="dated_market_evidence_config",
            aggregate_id=(
                f"{BOOK_EVIDENCE_PROTOCOL_VERSION}:{BOOK_DEPTH_LEVELS}:"
                f"{market_evidence_quota_bytes}"
            ),
            business_payload=config_payload,
            state=configured_state,
        )

    @staticmethod
    def _validate_checkpoint(state: Mapping[str, Any]) -> None:
        if (
            state.get("strategy") != STRATEGY_NAME
            or state.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
            or state.get("paper_only") is not True
        ):
            raise DatedLedgerError("unsupported or non-PAPER carry-dated checkpoint")
        for name in (
            "capital",
            "cash_total",
            "free_cash",
            "reserved_margin",
            "stress_cash_buffer",
            "spot_qty",
            "future_qty",
        ):
            value = state.get(name)
            if value is None:
                raise DatedLedgerError(f"checkpoint is missing {name}")
            decimal(value, field=name)

    @property
    def checkpoint(self) -> dict[str, Any]:
        return dict(self.state)

    def _commit(
        self,
        *,
        event_type: str,
        aggregate_id: str,
        business_payload: Mapping[str, Any],
        event_metadata: Mapping[str, Any] | None = None,
        state: dict[str, Any],
    ) -> str:
        try:
            result = self.store.apply_carry_dated_event_and_checkpoint(
                engine=ENGINE_NAME,
                event_type=event_type,
                aggregate_id=aggregate_id,
                business_payload=business_payload,
                event_metadata=event_metadata,
                state=state,
            )
        except AccountingIdentityCollision:
            # StateStore durably latches the uncertainty before raising. Refresh
            # this in-memory view so the same runner cannot continue on stale state.
            persisted = self.store.load_engine_state(ENGINE_NAME)
            if persisted is not None:
                self.state = persisted
            raise
        persisted = self.store.load_engine_state(ENGINE_NAME)
        if persisted is None:
            raise DatedLedgerError("StateStore lost the dated checkpoint after commit")
        self.state = persisted
        return result

    def _assert_position_writable(self) -> None:
        if (
            self.state.get("accounting_uncertain") is True
            or self.state.get("status") == "ACCOUNTING_UNCERTAIN"
        ):
            raise DatedLedgerError("position writes are blocked by ACCOUNTING_UNCERTAIN")

    def persist_decision(
        self,
        decision: Any,
        *,
        market_evidence_ids: tuple[str, ...] = (),
    ) -> str:
        payload = decision.to_dict()
        payload["market_evidence_ids"] = list(market_evidence_ids)
        candidate = dict(self.state)
        previous = candidate.get("decision") or {}
        stable_fields = (
            "action",
            "reason_code",
            "selected_instrument_id",
            "quantity",
            "net_after_reserve",
            "policy_version",
            "fee_model_version",
            "margin_model_version",
        )
        materially_changed = any(previous.get(name) != payload.get(name) for name in stable_fields)
        candidate["decision"] = payload
        candidate["decision_at"] = utc_string(decision.decision_at)
        candidate["reason_code"] = decision.reason_code
        candidate["reason_message"] = decision.reason_message
        candidate["last_updated_at"] = utc_string(decision.decision_at)
        if not materially_changed:
            self.store.save_carry_dated_checkpoint(candidate)
            self.state = candidate
            return "refreshed"
        return self._commit(
            event_type="dated_carry_decision",
            aggregate_id=str(decision.decision_id),
            business_payload=payload,
            state=candidate,
        )

    def persist_market_evidence(
        self,
        book: Any,
        *,
        purpose: str,
        requested_depth: int,
        protocol_version: str,
        now: datetime,
        required: bool = True,
    ) -> str:
        """Persist the exact observed snapshot used by a decision or risk action."""

        if requested_depth != BOOK_DEPTH_LEVELS:
            raise DatedLedgerError(
                f"market evidence protocol requires depth={BOOK_DEPTH_LEVELS}"
            )
        if protocol_version != BOOK_EVIDENCE_PROTOCOL_VERSION:
            raise DatedLedgerError("unsupported market evidence protocol version")
        if len(book.bids) > requested_depth or len(book.asks) > requested_depth:
            raise DatedLedgerError("market snapshot exceeds the requested evidence depth")
        if self.market_evidence_recorder is None:
            error: Exception = DatedLedgerError("MARKET_EVIDENCE_RECORDER_UNAVAILABLE")
        else:
            try:
                evidence_id = self.market_evidence_recorder.persist(
                    book,
                    purpose=purpose,
                    requested_depth=requested_depth,
                    protocol_version=protocol_version,
                    now=now,
                )
            except (OSError, RuntimeError, ValueError) as caught:
                error = caught
            else:
                prior_gap = self.state.get("market_evidence_gap")
                if prior_gap is not None:
                    candidate = dict(self.state)
                    candidate["market_evidence_gap"] = None
                    self.store.save_carry_dated_checkpoint(candidate)
                    self.state = candidate
                return evidence_id

        quota_failure = "MARKET_EVIDENCE_QUOTA_EXCEEDED" in str(error)
        if required:
            raise DatedLedgerError(str(error)) from error
        candidate = dict(self.state)
        gap = {
            "reason_code": (
                "MARKET_EVIDENCE_QUOTA_EXCEEDED"
                if quota_failure
                else "MARKET_EVIDENCE_UNAVAILABLE"
            ),
            "reason": f"{type(error).__name__}: {str(error)[:200]}",
            "detected_at": utc_string(now),
        }
        existing_gap = candidate.get("market_evidence_gap") or {}
        if existing_gap.get("reason_code") != gap["reason_code"]:
            candidate["market_evidence_gap"] = gap
            self.store.save_carry_dated_checkpoint(candidate)
            self.state = candidate
        return ""

    def record_wait(self, *, reason_code: str, reason_message: str, now: datetime) -> None:
        """Expose a bounded technical WAIT without creating a synthetic fill."""

        candidate = dict(self.state)
        previous = candidate.get("decision") or {}
        candidate["decision"] = {
            "action": "WAIT",
            "reason_code": reason_code,
            "reason_message": reason_message,
            "as_of": utc_string(now),
            "gross_premium": None,
            "net_before_reserve": None,
            "risk_reserve": "0",
            "net_after_reserve": None,
        }
        candidate["decision_at"] = utc_string(now)
        candidate["reason_code"] = reason_code
        candidate["reason_message"] = reason_message
        candidate["last_updated_at"] = utc_string(now)
        if previous.get("action") != "WAIT" or previous.get("reason_code") != reason_code:
            self._commit(
                event_type="dated_carry_technical_wait",
                aggregate_id=f"wait:{reason_code}:{utc_string(now)}",
                business_payload={"reason_code": reason_code, "reason_message": reason_message},
                state=candidate,
            )
            return
        self.store.save_carry_dated_checkpoint(candidate)
        self.state = candidate

    def begin_entry(
        self,
        *,
        decision: Any,
        spot_instrument: Any,
        future_instrument: Any,
        spot_book: Any,
        future_book: Any,
        reserved_margin: Decimal,
        stress_cash_buffer: Decimal,
        delivery_fee_estimate: Decimal | None,
        delivery_fee_rate: Decimal | None,
        delivery_fee_model_version: str,
        spot_entry_fee_rate: Decimal,
        future_taker_fee_rate: Decimal,
        spot_exit_fee_rate: Decimal,
        spot_fee_currency_scenario: str,
        hedge_tolerance: Decimal,
        market_evidence_ids: tuple[str, ...] = (),
    ) -> str:
        self._assert_position_writable()
        if self.state.get("status") != "FLAT":
            raise DatedLedgerError("entry requires a flat dated-carry checkpoint")
        if self.state.get("accounting_uncertain") is True:
            raise DatedLedgerError("entry blocked by accounting uncertainty")
        if decision.action != "OPEN":
            raise DatedLedgerError("an OPEN decision is required")
        quantity = decimal(decision.quantity, field="decision.quantity")
        reserve = decimal(reserved_margin, field="reserved_margin")
        stress = decimal(stress_cash_buffer, field="stress_cash_buffer")
        spot_entry_fee = decimal(spot_entry_fee_rate, field="spot_entry_fee_rate")
        future_taker_fee = decimal(future_taker_fee_rate, field="future_taker_fee_rate")
        spot_exit_fee = decimal(spot_exit_fee_rate, field="spot_exit_fee_rate")
        tolerance = decimal(hedge_tolerance, field="hedge_tolerance")
        if (
            quantity <= 0
            or reserve < 0
            or stress < 0
            or tolerance < 0
            or min(spot_entry_fee, future_taker_fee, spot_exit_fee) < 0
        ):
            raise DatedLedgerError("entry quantity must be positive and reserve non-negative")
        required_cash = decimal(decision.total_cash_required, field="decision.total_cash_required")
        if required_cash > state_decimal(self.state, "free_cash"):
            raise DatedLedgerError("entry decision exceeds available PAPER cash")
        if spot_fee_currency_scenario not in {"BASE", "QUOTE"}:
            raise DatedLedgerError("spot fee currency scenario must be explicitly BASE or QUOTE")
        saga_id = f"dated-entry:{decision.decision_id}"
        candidate = dict(self.state)
        candidate.update(
            {
                "status": "OPENING",
                "instrument": self._instrument_payload(spot_instrument, future_instrument),
                "expiry_utc": utc_string(future_instrument.expiration_utc),
                "reserved_margin": decimal_string(reserve),
                "stress_cash_buffer": decimal_string(stress),
                "hedge_tolerance": decimal_string(tolerance),
                "expected_hedge_residual": decimal_string(
                    getattr(decision, "expected_hedge_residual", Decimal("0"))
                ),
                "delivery_fee_estimate": (
                    decimal_string(delivery_fee_estimate)
                    if delivery_fee_estimate is not None
                    else None
                ),
                "delivery_fee_rate": (
                    decimal_string(delivery_fee_rate) if delivery_fee_rate is not None else None
                ),
                "delivery_fee_applied": None,
                "delivery_fee_model_version": delivery_fee_model_version,
                "spot_entry_fee_rate": decimal_string(spot_entry_fee),
                "future_taker_fee_rate": decimal_string(future_taker_fee),
                "spot_exit_fee_rate": decimal_string(spot_exit_fee),
                "spot_fee_currency_assumption": getattr(
                    decision, "spot_fee_currency_assumption", "UNKNOWN_WORST_CASE"
                ),
                "spot_fee_currency_applied_scenario": spot_fee_currency_scenario,
                "hedge_residual": "0",
                "position_opened_at": utc_string(decision.decision_at),
                "decision": decision.to_dict(),
                "decision_at": utc_string(decision.decision_at),
                "reason_code": decision.reason_code,
                "reason_message": decision.reason_message,
                "market": {
                    "spot_timestamp": utc_string(spot_book.market_timestamp),
                    "future_timestamp": utc_string(future_book.market_timestamp),
                },
                "pending_saga_ids": [saga_id],
                "active_saga_id": saga_id,
                "last_settlement_key": None,
                "settlement_audit_start_key": None,
                "last_settlement_index": None,
                "last_settlement_period_end": None,
                "settlements_applied": 0,
                "expiry_spot_liquidation_late": None,
                "expiry_spot_liquidation_delay_seconds": None,
                "expiry_spot_liquidation_book_timestamp": None,
                "fee_model_version": decision.fee_model_version,
                "margin_model_version": decision.margin_model_version,
                "last_updated_at": utc_string(decision.decision_at),
            }
        )
        business = {
            "decision_id": decision.decision_id,
            "selected_instrument_id": future_instrument.instrument_id,
            "quantity": decimal_string(quantity),
            "spot_book_hash": getattr(spot_book, "content_hash", None),
            "future_book_hash": getattr(future_book, "content_hash", None),
            "reserved_margin": decimal_string(reserve),
            "stress_cash_buffer": decimal_string(stress),
            "spot_fee_currency_scenario": spot_fee_currency_scenario,
            "spot_entry_fee_rate": decimal_string(spot_entry_fee),
            "future_taker_fee_rate": decimal_string(future_taker_fee),
            "spot_exit_fee_rate": decimal_string(spot_exit_fee),
            "market_evidence_ids": list(market_evidence_ids),
        }
        return self._commit(
            event_type="dated_entry_intent",
            aggregate_id=saga_id,
            business_payload=business,
            state=candidate,
        )

    @staticmethod
    def _instrument_payload(spot: Any, future: Any) -> dict[str, Any]:
        def serialize(instrument: Any) -> dict[str, Any]:
            expiry = getattr(instrument, "expiration_utc", None)
            return {
                "venue": instrument.venue,
                "instrument_id": instrument.instrument_id,
                "kind": instrument.kind,
                "base": instrument.base,
                "quote": instrument.quote,
                "settlement_currency": instrument.settlement_currency,
                "contract_size": decimal_string(instrument.contract_size),
                "amount_step": decimal_string(instrument.amount_step),
                "min_amount": decimal_string(instrument.min_amount),
                "expiration_utc": utc_string(expiry) if expiry is not None else None,
                "metadata_hash": instrument.metadata_hash,
                "metadata_timestamp": utc_string(instrument.metadata_timestamp),
                "fee_provenance": instrument.fee_provenance,
                "taker_commission": (
                    decimal_string(instrument.taker_commission)
                    if instrument.taker_commission is not None
                    else None
                ),
                "fee_currency": getattr(instrument, "fee_currency", None),
                "source": instrument.source,
            }

        return {"spot": serialize(spot), "future": serialize(future)}

    def apply_fill(self, fill: BookFill, *, timestamp: datetime | None = None) -> str:
        self._assert_position_writable()
        if fill.filled_qty <= 0 or fill.average_price is None:
            raise DatedLedgerError("a zero-quantity fill cannot change the ledger")
        candidate = dict(self.state)
        cash = state_decimal(candidate, "cash_total")
        realized = state_decimal(candidate, "pnl_realized")
        total_fees = state_decimal(candidate, "fees_paid")
        qty_field = "spot_qty" if fill.leg == "spot" else "future_qty"
        avg_field = "spot_avg_entry" if fill.leg == "spot" else "future_avg_entry"
        quantity = state_decimal(candidate, qty_field)
        old_avg = (
            decimal(candidate[avg_field], field=avg_field)
            if candidate.get(avg_field) is not None
            else Decimal("0")
        )
        notional = fill.notional

        if fill.leg == "spot" and fill.side == "BUY":
            if candidate.get("status") != "OPENING" or quantity != 0:
                raise DatedLedgerError("spot buy is only allowed as the first opening leg")
            cash_fee = fill.fee if fill.fee_currency == "QUOTE" else Decimal("0")
            if notional + cash_fee > cash:
                raise DatedLedgerError("spot purchase would borrow quote cash")
            acquired_qty = fill.net_base_qty
            if acquired_qty <= 0:
                raise DatedLedgerError("base-denominated fee consumed the spot fill")
            new_quantity = quantity + acquired_qty
            new_average = (old_avg * quantity + fill.average_price * acquired_qty) / new_quantity
            cash -= notional + cash_fee
            realized -= fill.fee
            candidate["spot_qty"] = decimal_string(new_quantity)
            candidate["spot_avg_entry"] = decimal_string(new_average)
        elif fill.leg == "spot" and fill.side == "SELL":
            if candidate.get("status") not in {
                "OPENING",
                "OPEN",
                "CLOSING",
                "SETTLEMENT_PENDING",
                "UNBALANCED",
            }:
                raise DatedLedgerError("spot sale is not valid in the current carry state")
            if fill.base_consumed > quantity:
                raise DatedLedgerError("spot sale exceeds the PAPER position")
            cash_fee = fill.fee if fill.fee_currency == "QUOTE" else Decimal("0")
            cash += notional - cash_fee
            if fill.fee_currency == "BASE":
                # The base fee removes extra BTC without quote proceeds. Its
                # opportunity cost is captured by charging the consumed BTC at
                # its historical basis, not by subtracting its current value.
                realized += notional - fill.base_consumed * old_avg
            else:
                realized += fill.filled_qty * (fill.average_price - old_avg) - fill.fee
            new_quantity = quantity - fill.base_consumed
            candidate["spot_qty"] = decimal_string(new_quantity)
            if new_quantity == 0:
                candidate["spot_avg_entry"] = None
        elif fill.leg == "future" and fill.side == "SELL":
            if candidate.get("status") != "OPENING" or quantity != 0:
                raise DatedLedgerError("future short is only allowed as the first opening leg")
            new_quantity = quantity + fill.filled_qty
            new_average = (old_avg * quantity + fill.average_price * fill.filled_qty) / new_quantity
            cash -= fill.fee
            realized -= fill.fee
            candidate["future_qty"] = decimal_string(new_quantity)
            candidate["future_avg_entry"] = decimal_string(new_average)
            candidate["future_reference_price"] = decimal_string(new_average)
        elif fill.leg == "future" and fill.side == "BUY":
            if candidate.get("status") not in {
                "OPENING",
                "OPEN",
                "CLOSING",
                "SETTLEMENT_PENDING",
                "UNBALANCED",
            }:
                raise DatedLedgerError("future cover is not valid in the current carry state")
            if fill.filled_qty > quantity:
                raise DatedLedgerError("future cover exceeds the PAPER short")
            reference_raw = candidate.get("future_reference_price")
            if reference_raw is None:
                raise DatedLedgerError("future cover has no settlement reference price")
            reference = decimal(reference_raw, field="future_reference_price")
            pnl = fill.filled_qty * (reference - fill.average_price)
            cash += pnl - fill.fee
            realized += pnl - fill.fee
            new_quantity = quantity - fill.filled_qty
            candidate["future_qty"] = decimal_string(new_quantity)
            if new_quantity == 0:
                candidate["future_avg_entry"] = None
                candidate["future_reference_price"] = None
                candidate["reserved_margin"] = "0"
                candidate["stress_cash_buffer"] = "0"
                candidate["maintenance_margin"] = "0"
                candidate["unrealized_short_loss_to_mark"] = "0"
        else:
            raise DatedLedgerError(f"unsupported PAPER fill {fill.leg}/{fill.side}")

        total_fees += fill.fee
        sequences = dict(candidate.get("fill_sequences") or {})
        sequences[fill.leg] = int(sequences.get(fill.leg, 0)) + 1
        candidate["fill_sequences"] = sequences
        candidate["cash_total"] = decimal_string(cash)
        candidate["pnl_realized"] = decimal_string(realized)
        candidate["fees_paid"] = decimal_string(total_fees)
        candidate["last_updated_at"] = utc_string(timestamp)
        candidate["free_cash"] = decimal_string(cash - reserved_total(candidate))
        candidate["derivative_cash_after_unrealized_short_loss"] = decimal_string(cash)
        candidate["cash_after_margin_stress"] = decimal_string(cash - reserved_total(candidate))
        spot_position = state_decimal(candidate, "spot_qty")
        future_position = state_decimal(candidate, "future_qty")
        prior_status = self.state.get("status")
        if spot_position == 0 and future_position == 0:
            candidate["status"] = "FLAT"
            candidate["reserved_margin"] = "0"
            candidate["stress_cash_buffer"] = "0"
            candidate["maintenance_margin"] = "0"
            candidate["unrealized_short_loss_to_mark"] = "0"
            candidate["maintenance_margin"] = "0"
            candidate["unrealized_short_loss_to_mark"] = "0"
            candidate["derivative_cash_after_unrealized_short_loss"] = decimal_string(cash)
            candidate["cash_after_margin_stress"] = decimal_string(cash)
            candidate["free_cash"] = decimal_string(cash)
            candidate["pending_saga_ids"] = []
            candidate["active_saga_id"] = None
            candidate["instrument"] = None
            candidate["expiry_utc"] = None
            candidate["spot_avg_entry"] = None
            candidate["future_avg_entry"] = None
            candidate["future_reference_price"] = None
            candidate["spot_fee_currency_assumption"] = None
            candidate["spot_fee_currency_applied_scenario"] = None
            candidate["spot_entry_fee_rate"] = None
            candidate["future_taker_fee_rate"] = None
            candidate["spot_exit_fee_rate"] = None
            candidate["expected_hedge_residual"] = "0"
            candidate["hedge_tolerance"] = "0"
            candidate["hedge_residual"] = "0"
            candidate["incident"] = None
            dust_qty = state_decimal(candidate, "spot_dust_qty")
            dust_basis_raw = candidate.get("spot_dust_avg_entry")
            dust_mark_raw = candidate.get("spot_dust_mark")
            if dust_qty and (dust_basis_raw is None or dust_mark_raw is None):
                raise DatedLedgerError("residual BTC inventory lacks its basis or mark")
            if dust_qty:
                assert dust_basis_raw is not None and dust_mark_raw is not None
                dust_basis = decimal(dust_basis_raw, field="spot_dust_avg_entry")
                dust_mark = decimal(dust_mark_raw, field="spot_dust_mark")
            else:
                dust_basis = Decimal("0")
                dust_mark = Decimal("0")
            candidate["pnl_unrealized"] = decimal_string(dust_qty * (dust_mark - dust_basis))
            candidate["equity"] = decimal_string(cash + dust_qty * dust_mark)
        elif cash < 0:
            candidate["status"] = "UNBALANCED"
            candidate["incident"] = {
                "kind": "DATED_CARRY_CASH_DEFICIT",
                "detected_at": utc_string(timestamp),
            }
        elif state_decimal(candidate, "free_cash") < 0:
            candidate["status"] = "UNBALANCED"
            candidate["incident"] = {
                "kind": "DATED_CARRY_CASH_SHORTFALL",
                "detected_at": utc_string(timestamp),
            }
        elif prior_status == "OPENING":
            candidate["status"] = "OPENING"
        elif prior_status in {"CLOSING", "SETTLEMENT_PENDING"}:
            candidate["status"] = prior_status
        elif spot_position == future_position and spot_position > 0:
            candidate["status"] = "OPENING" if prior_status in {"FLAT", "OPENING"} else prior_status
        else:
            candidate["status"] = "UNBALANCED"
        residual = spot_position - future_position
        candidate["hedge_residual"] = decimal_string(residual)
        tolerance = state_decimal(candidate, "hedge_tolerance")
        if abs(residual) > tolerance and spot_position > 0 and future_position > 0:
            candidate["status"] = "UNBALANCED"
            candidate["incident"] = {
                "kind": "DATED_CARRY_HEDGE_QUANTITY_MISMATCH",
                "spot_qty": candidate["spot_qty"],
                "future_qty": candidate["future_qty"],
                "detected_at": utc_string(timestamp),
            }
        business = fill.to_dict()
        event_id = fill.fill_id
        return self._commit(
            event_type="dated_paper_fill",
            aggregate_id=event_id,
            business_payload=business,
            state=candidate,
        )

    def mark_open(self, *, decision_id: str, now: datetime) -> str:
        self._assert_position_writable()
        if self.state.get("status") not in {"OPENING", "UNBALANCED"}:
            raise DatedLedgerError("only an opening position can be finalized as open")
        spot_qty = state_decimal(self.state, "spot_qty")
        future_qty = state_decimal(self.state, "future_qty")
        tolerance = state_decimal(self.state, "hedge_tolerance")
        incident = self.state.get("incident") or {}
        if incident and incident.get("kind") != "DATED_CARRY_HEDGE_QUANTITY_MISMATCH":
            raise DatedLedgerError("opening has a non-hedge incident and cannot be finalized")
        if spot_qty <= 0 or future_qty <= 0 or abs(spot_qty - future_qty) > tolerance:
            raise DatedLedgerError("a covered OPEN requires quantities within hedge tolerance")
        if state_decimal(self.state, "free_cash") < 0:
            raise DatedLedgerError("a covered OPEN requires non-negative derivative free cash")
        candidate = dict(self.state)
        candidate.update(
            {
                "status": "OPEN",
                "pending_saga_ids": [],
                "active_saga_id": None,
                "incident": None,
                "last_updated_at": utc_string(now),
            }
        )
        business = {"decision_id": decision_id, "spot_qty": decimal_string(spot_qty)}
        return self._commit(
            event_type="dated_entry_completed",
            aggregate_id=f"dated-entry-complete:{decision_id}",
            business_payload=business,
            state=candidate,
        )

    def abort_unfilled_entry(self, *, decision_id: str, now: datetime, reason: str) -> str:
        """Close an entry saga that has no durable fills, without changing cash."""

        self._assert_position_writable()
        if self.state.get("status") != "OPENING":
            raise DatedLedgerError("only an opening saga can be aborted")
        if state_decimal(self.state, "spot_qty") or state_decimal(self.state, "future_qty"):
            raise DatedLedgerError("entry saga with a fill requires compensation")
        candidate = dict(self.state)
        candidate.update(
            {
                "status": "FLAT",
                "reserved_margin": "0",
                "stress_cash_buffer": "0",
                "free_cash": candidate["cash_total"],
                "instrument": None,
                "expiry_utc": None,
                "spot_fee_currency_assumption": None,
                "spot_fee_currency_applied_scenario": None,
                "expected_hedge_residual": "0",
                "hedge_residual": "0",
                "expiry_spot_liquidation_late": None,
                "expiry_spot_liquidation_delay_seconds": None,
                "expiry_spot_liquidation_book_timestamp": None,
                "pending_saga_ids": [],
                "active_saga_id": None,
                "reason_code": "ENTRY_ABORTED_UNFILLED",
                "reason_message": reason,
                "incident": None,
                "last_updated_at": utc_string(now),
            }
        )
        return self._commit(
            event_type="dated_entry_aborted",
            aggregate_id=f"dated-entry-abort:{decision_id}",
            business_payload={"decision_id": decision_id, "reason": reason},
            state=candidate,
        )

    def begin_close(self, *, reason_code: str, reason_message: str, now: datetime) -> str:
        """Persist a protective close saga before simulating either exit leg."""

        self._assert_position_writable()
        if self.state.get("status") not in {
            "OPENING",
            "OPEN",
            "UNBALANCED",
            "SETTLEMENT_PENDING",
            "CLOSING",
        }:
            raise DatedLedgerError("close requires an opening, open or pending dated position")
        candidate = dict(self.state)
        saga_id = (
            str(candidate.get("active_saga_id"))
            if candidate.get("status") == "CLOSING" and candidate.get("active_saga_id")
            else f"dated-close:{reason_code}:{utc_string(now)}"
        )
        candidate.update(
            {
                "status": "CLOSING",
                "active_saga_id": saga_id,
                "pending_saga_ids": [saga_id],
                "reason_code": reason_code,
                "reason_message": reason_message,
                "decision_at": utc_string(now),
                "last_updated_at": utc_string(now),
            }
        )
        return self._commit(
            event_type="dated_close_intent",
            aggregate_id=saga_id,
            business_payload={"reason_code": reason_code, "reason_message": reason_message},
            state=candidate,
        )

    def fill_id(self, *, leg: str, side: str) -> str:
        if leg not in {"spot", "future"} or side not in {"BUY", "SELL"}:
            raise DatedLedgerError("invalid PAPER fill identity fields")
        saga_id = self.state.get("active_saga_id") or "dated-unspecified"
        sequences = self.state.get("fill_sequences") or {}
        sequence = int(sequences.get(leg, 0)) + 1
        return f"{saga_id}:{leg}:{side.lower()}:{sequence}"

    def recovery_fill_sequence(self, *, leg: str) -> int:
        sequences = self.state.get("fill_sequences") or {}
        return int(sequences.get(leg, 0))

    def dust_fill_id(self) -> str:
        return f"dated-spot-dust-sweep:{int(self.state.get('dust_sweep_sequence', 0)) + 1}"

    def classify_spot_dust(
        self,
        *,
        minimum_amount: Decimal,
        orderable_quantity: Decimal,
        market_book: Any,
        now: datetime,
    ) -> str:
        """Close the carry cycle while retaining sub-minimum BTC as visible inventory."""

        self._assert_position_writable()
        if self.state.get("status") not in {"CLOSING", "SETTLEMENT_PENDING", "UNBALANCED"}:
            raise DatedLedgerError("spot dust can only be classified during a close saga")
        if state_decimal(self.state, "future_qty") != 0:
            raise DatedLedgerError("spot dust cannot be detached while a future short remains")
        quantity = state_decimal(self.state, "spot_qty")
        if quantity <= 0:
            raise DatedLedgerError("spot dust classification requires a positive carry spot balance")
        minimum = decimal(minimum_amount, field="minimum_amount")
        orderable = decimal(orderable_quantity, field="orderable_quantity")
        if minimum <= 0 or orderable >= minimum:
            raise DatedLedgerError("spot quantity is still executable and cannot be classified as dust")
        if not market_book.bids:
            raise DatedLedgerError("spot dust classification requires an observed bid for valuation")
        instrument = self.state.get("instrument") or {}
        spot_record = instrument.get("spot") or {}
        if market_book.instrument_id != spot_record.get("instrument_id"):
            raise DatedLedgerError("spot dust valuation book does not match the held spot instrument")
        mark = decimal(market_book.bids[0].price, field="spot_dust_mark")
        active_basis_raw = self.state.get("spot_avg_entry")
        if active_basis_raw is None:
            raise DatedLedgerError("spot dust classification has no active spot cost basis")
        active_basis = decimal(active_basis_raw, field="spot_avg_entry")
        old_dust = state_decimal(self.state, "spot_dust_qty")
        old_dust_instrument = self.state.get("spot_dust_instrument_id")
        if old_dust and old_dust_instrument != market_book.instrument_id:
            raise DatedLedgerError("residual BTC inventory belongs to another spot instrument")
        old_dust_basis_raw = self.state.get("spot_dust_avg_entry")
        old_dust_basis = (
            decimal(old_dust_basis_raw, field="spot_dust_avg_entry")
            if old_dust and old_dust_basis_raw is not None
            else Decimal("0")
        )
        total_dust = old_dust + quantity
        total_basis = (old_dust * old_dust_basis + quantity * active_basis) / total_dust
        candidate = dict(self.state)
        candidate.update(
            {
                "status": "FLAT",
                "spot_qty": "0",
                "spot_avg_entry": None,
                "spot_dust_qty": decimal_string(total_dust),
                "spot_dust_avg_entry": decimal_string(total_basis),
                "spot_dust_mark": decimal_string(mark),
                "spot_dust_mark_timestamp": utc_string(market_book.market_timestamp),
                "spot_dust_instrument_id": market_book.instrument_id,
                "future_qty": "0",
                "future_avg_entry": None,
                "future_reference_price": None,
                "reserved_margin": "0",
                "stress_cash_buffer": "0",
                "maintenance_margin": "0",
                "unrealized_short_loss_to_mark": "0",
                "derivative_cash_after_unrealized_short_loss": candidate["cash_total"],
                "cash_after_margin_stress": candidate["cash_total"],
                "free_cash": candidate["cash_total"],
                "instrument": None,
                "expiry_utc": None,
                "pending_saga_ids": [],
                "active_saga_id": None,
                "spot_fee_currency_assumption": None,
                "spot_fee_currency_applied_scenario": None,
                "spot_entry_fee_rate": None,
                "future_taker_fee_rate": None,
                "spot_exit_fee_rate": None,
                "expected_hedge_residual": "0",
                "hedge_tolerance": "0",
                "hedge_residual": "0",
                "pnl_unrealized": decimal_string(total_dust * (mark - total_basis)),
                "equity": decimal_string(
                    state_decimal(candidate, "cash_total") + total_dust * mark
                ),
                "reason_code": "CLOSED_WITH_SPOT_DUST",
                "reason_message": (
                    "Carry cycle closed; untradeable BTC remains in a separately tracked dust "
                    "inventory and will be sold from a fresh bid book when it reaches minimum size"
                ),
                "incident": None,
                "last_updated_at": utc_string(now),
            }
        )
        active_saga_id = str(self.state.get("active_saga_id") or "unknown")
        return self._commit(
            event_type="dated_spot_dust_classified",
            aggregate_id=f"spot-dust-classification:{active_saga_id}",
            business_payload={
                "quantity": decimal_string(quantity),
                "cost_basis": decimal_string(active_basis),
                "minimum_amount": decimal_string(minimum),
                "orderable_quantity": decimal_string(orderable),
                "residual_inventory_quantity": decimal_string(total_dust),
                "valuation_price": decimal_string(mark),
                "valuation_is_fill": False,
                "market_timestamp": utc_string(market_book.market_timestamp),
                "received_timestamp": utc_string(market_book.received_timestamp),
                "book_source": market_book.source,
                "book_sequence": market_book.sequence,
                "book_hash": market_book.content_hash,
            },
            state=candidate,
        )

    def apply_spot_dust_fill(self, fill: BookFill, *, timestamp: datetime | None = None) -> str:
        """Apply a public-book PAPER sale against the segregated dust inventory."""

        self._assert_position_writable()
        if self.state.get("status") != "FLAT" or fill.leg != "spot" or fill.side != "SELL":
            raise DatedLedgerError("dust sweep requires a flat PAPER spot SELL")
        if fill.filled_qty <= 0 or fill.average_price is None:
            raise DatedLedgerError("zero-quantity dust fill cannot change the ledger")
        quantity = state_decimal(self.state, "spot_dust_qty")
        if fill.instrument_id != self.state.get("spot_dust_instrument_id"):
            raise DatedLedgerError("dust fill instrument does not match tracked residual inventory")
        if fill.base_consumed > quantity:
            raise DatedLedgerError("dust sale exceeds the tracked residual BTC inventory")
        basis_raw = self.state.get("spot_dust_avg_entry")
        if basis_raw is None:
            raise DatedLedgerError("residual BTC inventory has no cost basis")
        basis = decimal(basis_raw, field="spot_dust_avg_entry")
        cash = state_decimal(self.state, "cash_total")
        realized = state_decimal(self.state, "pnl_realized")
        fees_paid = state_decimal(self.state, "fees_paid")
        cash_fee = fill.fee if fill.fee_currency == "QUOTE" else Decimal("0")
        cash += fill.notional - cash_fee
        if fill.fee_currency == "BASE":
            realized += fill.notional - fill.base_consumed * basis
        else:
            realized += fill.notional - fill.fee - fill.filled_qty * basis
        remaining = quantity - fill.base_consumed
        candidate = dict(self.state)
        snapshot = fill.book_snapshot
        observed_mark = (
            decimal(snapshot["bids"][0]["price"], field="book_snapshot.bid.price")
            if snapshot.get("bids")
            else fill.average_price
        )
        candidate.update(
            {
                "spot_dust_qty": decimal_string(remaining),
                "spot_dust_avg_entry": decimal_string(basis) if remaining else None,
                "spot_dust_mark": decimal_string(observed_mark) if remaining else None,
                "spot_dust_instrument_id": (
                    candidate.get("spot_dust_instrument_id") if remaining else None
                ),
                "spot_dust_mark_timestamp": (
                    fill.market_timestamp if remaining else None
                ),
                "cash_total": decimal_string(cash),
                "free_cash": decimal_string(cash),
                "pnl_realized": decimal_string(realized),
                "pnl_unrealized": (
                    decimal_string(remaining * (observed_mark - basis)) if remaining else "0"
                ),
                "fees_paid": decimal_string(fees_paid + fill.fee),
                "equity": decimal_string(cash + remaining * observed_mark),
                "reason_code": "SPOT_DUST_SWEEP_PARTIAL" if remaining else "SPOT_DUST_SWEPT",
                "reason_message": (
                    "Residual BTC inventory was partially sold from observed bid depth"
                    if remaining
                    else "Residual BTC inventory was fully sold from observed bid depth"
                ),
                "last_updated_at": utc_string(timestamp),
                "dust_sweep_sequence": int(candidate.get("dust_sweep_sequence", 0)) + 1,
            }
        )
        return self._commit(
            event_type="dated_spot_dust_sweep_fill",
            aggregate_id=fill.fill_id,
            business_payload=fill.to_dict(),
            state=candidate,
        )

    def mark_spot_dust(
        self,
        *,
        spot_mark: Decimal | None,
        now: datetime,
        market_timestamp: datetime | None = None,
    ) -> None:
        """Update residual-inventory valuation without creating a PAPER fill."""

        if state_decimal(self.state, "spot_dust_qty") <= 0:
            return
        if self.state.get("status") != "FLAT":
            raise DatedLedgerError("flat dust valuation cannot overlap an active carry position")
        if spot_mark is None:
            return
        mark = decimal(spot_mark, field="spot_dust_mark")
        quantity = state_decimal(self.state, "spot_dust_qty")
        basis_raw = self.state.get("spot_dust_avg_entry")
        if basis_raw is None:
            raise DatedLedgerError("residual BTC inventory has no cost basis")
        basis = decimal(basis_raw, field="spot_dust_avg_entry")
        cash = state_decimal(self.state, "cash_total")
        candidate = dict(self.state)
        candidate.update(
            {
                "spot_dust_mark": decimal_string(mark),
                "spot_dust_mark_timestamp": (
                    utc_string(market_timestamp) if market_timestamp is not None else None
                ),
                "pnl_unrealized": decimal_string(quantity * (mark - basis)),
                "equity": decimal_string(cash + quantity * mark),
                "last_updated_at": utc_string(now),
            }
        )
        self.store.save_carry_dated_checkpoint(candidate)
        self.state = candidate

    def apply_settlement(self, settlement: Any, *, now: datetime | None = None) -> str:
        self._assert_position_writable()
        if self.state.get("status") not in {"OPEN", "SETTLEMENT_PENDING"}:
            raise DatedLedgerError("settlement requires an open dated position")
        instrument = self.state.get("instrument") or {}
        future_instrument = instrument.get("future") or {}
        if settlement.instrument_id != future_instrument.get("instrument_id"):
            raise DatedLedgerError("settlement instrument does not match the held future")
        if settlement.venue != future_instrument.get("venue"):
            raise DatedLedgerError("settlement venue does not match the held future")
        if settlement.kind not in {"daily", "expiry"}:
            raise DatedLedgerError("unsupported settlement kind")
        aggregate = f"{settlement.venue}:{settlement.instrument_id}:{settlement.settlement_key}"
        stable_payload = self._settlement_payload(settlement)
        already_applied = self.store.has_event(ENGINE_NAME, "dated_future_settlement", aggregate)
        if already_applied:
            return self._commit(
                event_type="dated_future_settlement",
                aggregate_id=aggregate,
                business_payload=stable_payload,
                state=dict(self.state),
            )
        previous_key = self.state.get("last_settlement_key")
        opened_at_raw = self.state.get("position_opened_at")
        if opened_at_raw is not None:
            opened_at = datetime.fromisoformat(str(opened_at_raw).replace("Z", "+00:00"))
            if settlement.event_timestamp <= opened_at:
                # History returned before this position began. It is not an
                # event in this position's PnL and must not move its reference.
                return "before_position"
        if settlement.settlement_key == previous_key:
            # Cursor without its corresponding immutable event means checkpoint
            # corruption; replaying would double-book settled cash.
            self.mark_uncertain(
                kind="DATED_SETTLEMENT_EVENT_MISSING",
                message=f"Cursor references absent settlement event {aggregate}",
                now=now or settlement.received_at,
            )
            raise DatedLedgerError("settlement cursor exists without its immutable event")
        elif settlement.kind == "daily":
            if previous_key is None:
                if opened_at_raw is None:
                    self.mark_uncertain(
                        kind="POSITION_OPEN_TIME_MISSING",
                        message="Cannot identify the first daily settlement boundary for this position",
                        now=now or settlement.received_at,
                    )
                    raise DatedLedgerError("position open time is required for first settlement")
                expected_end = next_daily_settlement_after(opened_at)
                if settlement.period_end != expected_end:
                    self.mark_settlement_pending(
                        reason_code="DAILY_SETTLEMENT_GAP",
                        reason_message=(
                            f"First daily settlement expected at {utc_string(expected_end)}, "
                            f"received {utc_string(settlement.period_end)}"
                        ),
                        now=now or settlement.received_at,
                    )
                    raise DatedLedgerError("first daily settlement is missing; cash PnL was not applied")
            else:
                previous_end = datetime.fromisoformat(
                    str(self.state["last_settlement_period_end"]).replace("Z", "+00:00")
                )
                expected_end = previous_end + timedelta(days=1)
                if settlement.period_end != expected_end or settlement.period_start != previous_end:
                    self.mark_settlement_pending(
                        reason_code="DAILY_SETTLEMENT_GAP",
                        reason_message=(
                            f"Settlement interval should end at {utc_string(expected_end)}, "
                            f"received {utc_string(settlement.period_end)}"
                        ),
                        now=now or settlement.received_at,
                    )
                    raise DatedLedgerError("daily settlement gap; cash PnL was not applied")
            if (
                settlement.event_timestamp != settlement.period_end
                or (settlement.period_end - settlement.period_start).total_seconds() != 86_400
            ):
                self.mark_settlement_pending(
                    reason_code="DAILY_SETTLEMENT_PERIOD_INVALID",
                    reason_message="Daily settlement event/period does not match the official 24-hour boundary",
                    now=now or settlement.received_at,
                )
                raise DatedLedgerError("daily settlement period is invalid; cash PnL was not applied")
            expiry_raw = future_instrument.get("expiration_utc")
            if expiry_raw:
                expiry = datetime.fromisoformat(str(expiry_raw).replace("Z", "+00:00"))
                if settlement.event_timestamp >= expiry:
                    raise DatedLedgerError("daily settlement cannot be applied at/after contract expiry")
        elif settlement.kind == "expiry":
            previous_end_raw = self.state.get("last_settlement_period_end")
            if previous_end_raw is not None:
                previous_end = datetime.fromisoformat(str(previous_end_raw).replace("Z", "+00:00"))
                delta = settlement.period_end - previous_end
                if delta.total_seconds() != 86_400:
                    self.mark_settlement_pending(
                        reason_code="EXPIRY_SETTLEMENT_GAP",
                        reason_message=f"Settlement interval gap before expiry: {delta}",
                        now=now or settlement.received_at,
                    )
                    raise DatedLedgerError("expiry settlement gap; cash PnL was not applied")
            expiry_raw = future_instrument.get("expiration_utc")
            if expiry_raw is None or utc_string(settlement.event_timestamp) != expiry_raw:
                self.mark_uncertain(
                    kind="EXPIRY_TIMESTAMP_MISMATCH",
                    message="Official expiry event does not match the held contract expiry",
                    now=now or settlement.received_at,
                )
                raise DatedLedgerError(
                    "expiry settlement timestamp does not match contract metadata"
                )

        future_qty = state_decimal(self.state, "future_qty")
        reference_raw = self.state.get("future_reference_price")
        if future_qty <= 0 or reference_raw is None:
            if settlement.kind == "expiry" and future_qty == 0:
                return "replayed"
            raise DatedLedgerError("settlement has no open short quantity/reference")
        reference = decimal(reference_raw, field="future_reference_price")
        index_price = decimal(settlement.official_index_price, field="official_index_price")
        cash_pnl = future_qty * (reference - index_price)
        candidate = dict(self.state)
        cash = state_decimal(candidate, "cash_total") + cash_pnl
        realized = state_decimal(candidate, "pnl_realized") + cash_pnl
        fees_paid = state_decimal(candidate, "fees_paid")
        delivery_fee: Decimal | None = None
        delivery_rate: Decimal | None = None
        if settlement.kind == "expiry":
            raw_rate = candidate.get("delivery_fee_rate")
            if raw_rate is None:
                candidate["status"] = "SETTLEMENT_PENDING"
                candidate["incident"] = {
                    "kind": "DELIVERY_FEE_RATE_MISSING",
                    "settlement_key": settlement.settlement_key,
                    "detected_at": utc_string(now or settlement.received_at),
                }
                self._commit(
                    event_type="dated_settlement_pending",
                    aggregate_id=f"{settlement.venue}:{settlement.instrument_id}:{settlement.settlement_key}",
                    business_payload=self._settlement_payload(settlement),
                    state=candidate,
                )
                raise DatedLedgerError("expiry settlement lacks its versioned delivery-fee rate")
            delivery_rate = decimal(raw_rate, field="delivery_fee_rate")
            if delivery_rate < 0:
                raise DatedLedgerError("delivery fee rate must be non-negative")
            # The entry-time amount is an estimate only. At expiry the PAPER
            # model applies its versioned rate to the official index notional.
            delivery_fee = future_qty * index_price * delivery_rate
            cash -= delivery_fee
            realized -= delivery_fee
            fees_paid += delivery_fee
            candidate["delivery_fee_applied"] = decimal_string(delivery_fee)
            candidate["future_qty"] = "0"
            candidate["future_avg_entry"] = None
            candidate["future_reference_price"] = None
            candidate["reserved_margin"] = "0"
            candidate["stress_cash_buffer"] = "0"
            candidate["status"] = "SETTLEMENT_PENDING"
        else:
            candidate["future_reference_price"] = decimal_string(index_price)
            candidate["status"] = "OPEN"
        candidate.update(
            {
                "cash_total": decimal_string(cash),
                "pnl_realized": decimal_string(realized),
                "fees_paid": decimal_string(fees_paid),
                "last_settlement_key": settlement.settlement_key,
                "settlement_audit_start_key": candidate.get("settlement_audit_start_key")
                or settlement.settlement_key,
                "last_settlement_index": decimal_string(index_price),
                "last_settlement_period_end": utc_string(settlement.period_end),
                "settlements_applied": int(candidate.get("settlements_applied", 0)) + 1,
                "last_updated_at": utc_string(now or settlement.received_at),
                "reason_code": (
                    "EXPIRY_SPOT_LIQUIDATION_PENDING"
                    if settlement.kind == "expiry"
                    else "SETTLEMENT_APPLIED"
                ),
                "reason_message": (
                    "Official future settlement booked; spot liquidation remains separate"
                    if settlement.kind == "expiry"
                    else "Official daily variation settlement applied"
                ),
                "incident": (
                    {"kind": "EXPIRY_SPOT_LIQUIDATION_PENDING"}
                    if settlement.kind == "expiry"
                    else None
                ),
            }
        )
        if candidate.get("status") == "OPEN":
            candidate["free_cash"] = decimal_string(cash - reserved_total(candidate))
        else:
            candidate["free_cash"] = decimal_string(cash)
        return self._commit(
            event_type="dated_future_settlement",
            aggregate_id=aggregate,
            business_payload=stable_payload,
            event_metadata={
                "application": {
                    "cash_pnl": decimal_string(cash_pnl),
                    "delivery_fee_paper_assumption_applied": (
                        decimal_string(delivery_fee) if delivery_fee is not None else None
                    ),
                    "delivery_fee_rate": (
                        decimal_string(delivery_rate) if delivery_rate is not None else None
                    ),
                    "delivery_fee_model_version": candidate.get("delivery_fee_model_version"),
                    "delivery_fee_basis": (
                        "future_quantity_x_official_index_x_versioned_paper_rate"
                        if settlement.kind == "expiry"
                        else None
                    ),
                    "future_quantity": decimal_string(future_qty),
                    "reference_price": decimal_string(reference),
                }
            },
            state=candidate,
        )

    @staticmethod
    def _settlement_payload(settlement: Any) -> dict[str, Any]:
        return {
            "venue": settlement.venue,
            "instrument_id": settlement.instrument_id,
            "settlement_key": settlement.settlement_key,
            "kind": settlement.kind,
            "period_start": utc_string(settlement.period_start),
            "period_end": utc_string(settlement.period_end),
            "official_index_price": decimal_string(settlement.official_index_price),
            "source": settlement.source,
            "content_hash": settlement.content_hash,
            "correction_version": settlement.correction_version,
            "event_timestamp": utc_string(settlement.event_timestamp),
        }

    def mark_settlement_pending(
        self, *, reason_code: str, reason_message: str, now: datetime
    ) -> None:
        if (
            self.state.get("accounting_uncertain") is True
            or self.state.get("status") == "ACCOUNTING_UNCERTAIN"
        ):
            # Operational symptoms must never overwrite the stronger latch.
            return
        candidate = dict(self.state)
        candidate["status"] = "SETTLEMENT_PENDING"
        candidate["incident"] = {
            "kind": reason_code,
            "message": reason_message,
            "detected_at": utc_string(now),
        }
        candidate["decision"] = {
            "action": "WAIT",
            "reason_code": reason_code,
            "reason_message": reason_message,
            "as_of": utc_string(now),
            "gross_premium": None,
            "net_before_reserve": None,
            "risk_reserve": "0",
            "net_after_reserve": None,
        }
        candidate["reason_code"] = reason_code
        candidate["reason_message"] = reason_message
        candidate["decision_at"] = utc_string(now)
        candidate["last_updated_at"] = utc_string(now)
        prior_incident = self.state.get("incident") or {}
        if (
            self.state.get("status") == "SETTLEMENT_PENDING"
            and self.state.get("reason_code") == reason_code
            and prior_incident.get("message") == reason_message
        ):
            self.store.save_carry_dated_checkpoint(candidate)
            self.state = candidate
            return
        self.store.save_engine_state(
            ENGINE_NAME,
            candidate,
            event_type="dated_settlement_pending",
            event_payload={"reason_code": reason_code, "reason_message": reason_message},
        )
        self.state = candidate

    def resume_open_after_recovery(self, *, now: datetime) -> bool:
        """Clear only a resolved operational WAIT, never a missing settlement."""

        recoverable = {
            "STALE_MARKET_RISK_REVIEW",
            "BOOKS_DESYNCHRONIZED_RISK_REVIEW",
            "FUTURE_MARK_MISSING_RISK_REVIEW",
            "POSITION_MARKET_DATA_UNAVAILABLE",
            "SETTLEMENT_HISTORY_UNAVAILABLE",
            "RUNNER_CYCLE_ERROR",
        }
        if (
            self.state.get("status") != "SETTLEMENT_PENDING"
            or self.state.get("reason_code") not in recoverable
            or self.state.get("active_saga_id")
            or state_decimal(self.state, "future_qty") <= 0
        ):
            return False
        previous_reason = self.state.get("reason_code")
        candidate = dict(self.state)
        candidate.update(
            {
                "status": "OPEN",
                "incident": None,
                "reason_code": "POSITION_OPERATIONAL_RECOVERY",
                "reason_message": f"Fresh market and settlement data restored after {previous_reason}",
                "decision_at": utc_string(now),
                "last_updated_at": utc_string(now),
            }
        )
        self._commit(
            event_type="dated_position_operational_recovery",
            aggregate_id=f"operational-recovery:{previous_reason}:{utc_string(now)}",
            business_payload={
                "previous_reason_code": previous_reason,
                "as_of": utc_string(now),
            },
            state=candidate,
        )
        return True

    def record_expiry_spot_liquidation_timing(
        self,
        *,
        expiry: datetime,
        observed_book_timestamp: datetime,
        now: datetime,
        late_threshold_seconds: int = 300,
    ) -> None:
        """Persist whether the first available spot book missed the target window."""

        if self.state.get("expiry_spot_liquidation_late") is not None:
            return
        delay = max(0, int((now - expiry).total_seconds()))
        late = delay > late_threshold_seconds
        settlement_key = str(self.state.get("last_settlement_key") or "unknown")
        candidate = dict(self.state)
        candidate.update(
            {
                "expiry_spot_liquidation_late": late,
                "expiry_spot_liquidation_delay_seconds": delay,
                "expiry_spot_liquidation_book_timestamp": utc_string(observed_book_timestamp),
                "reason_code": (
                    "EXPIRY_SPOT_LIQUIDATION_LATE" if late else "EXPIRY_SPOT_LIQUIDATION_ON_TIME"
                ),
                "reason_message": (
                    f"First fresh spot book arrived {delay}s after expiry; this cycle is "
                    "operationally late and does not qualify the configured exit timing"
                    if late
                    else "Spot liquidation uses the first fresh observed book within the configured window"
                ),
                "last_updated_at": utc_string(now),
            }
        )
        self._commit(
            event_type="dated_expiry_spot_liquidation_timing",
            aggregate_id=f"expiry-spot-timing:{settlement_key}",
            business_payload={
                "settlement_key": settlement_key,
                "late": late,
                "delay_seconds": delay,
                "observed_book_timestamp": utc_string(observed_book_timestamp),
                "threshold_seconds": late_threshold_seconds,
            },
            state=candidate,
        )

    def mark_to_market(
        self,
        *,
        spot_mark: Decimal,
        future_mark: Decimal,
        now: datetime,
        spot_market_timestamp: datetime | None = None,
    ) -> None:
        candidate = dict(self.state)
        spot_qty = state_decimal(candidate, "spot_qty")
        dust_qty = state_decimal(candidate, "spot_dust_qty")
        future_qty = state_decimal(candidate, "future_qty")
        spot_avg = (
            decimal(candidate["spot_avg_entry"], field="spot_avg_entry")
            if spot_qty
            else Decimal("0")
        )
        reference_raw = candidate.get("future_reference_price")
        if future_qty:
            if reference_raw is None:
                raise DatedLedgerError("open future quantity has no reference price")
            future_reference = decimal(reference_raw, field="future_reference_price")
        else:
            future_reference = Decimal("0")
        dust_basis_raw = candidate.get("spot_dust_avg_entry")
        if dust_qty and dust_basis_raw is None:
            raise DatedLedgerError("residual BTC inventory has no cost basis")
        if dust_qty:
            assert dust_basis_raw is not None
            dust_basis = decimal(dust_basis_raw, field="spot_dust_avg_entry")
        else:
            dust_basis = Decimal("0")
        spot_price = decimal(spot_mark)
        unrealized = spot_qty * (spot_price - spot_avg)
        unrealized += dust_qty * (spot_price - dust_basis)
        unrealized += future_qty * (future_reference - decimal(future_mark))
        cash = state_decimal(candidate, "cash_total")
        candidate.update(
            {
                "pnl_unrealized": decimal_string(unrealized),
                "equity": decimal_string(
                    cash
                    + (spot_qty + dust_qty) * spot_price
                    + future_qty * (future_reference - decimal(future_mark))
                ),
                "last_updated_at": utc_string(now),
            }
        )
        if dust_qty:
            candidate["spot_dust_mark"] = decimal_string(spot_price)
            candidate["spot_dust_mark_timestamp"] = utc_string(
                spot_market_timestamp or now
            )
        self.store.save_carry_dated_checkpoint(candidate)
        self.state = candidate

    def record_margin_assessment(
        self,
        assessment: Any,
        *,
        future_mark: Decimal,
        now: datetime,
    ) -> Decimal:
        """Refresh risk liquidity, including adverse unrealized short loss."""

        quantity = state_decimal(self.state, "future_qty")
        if quantity <= 0:
            return Decimal("0")
        mark = decimal(future_mark, field="future_mark")
        reference = decimal(self.state["future_reference_price"], field="future_reference_price")
        unrealized_short_loss = max(Decimal("0"), quantity * (mark - reference))
        cash = state_decimal(self.state, "cash_total")
        initial_margin = decimal(assessment.initial_margin_usdc, field="initial_margin_usdc")
        maintenance_margin = decimal(
            assessment.maintenance_margin_usdc, field="maintenance_margin_usdc"
        )
        stress_buffer = decimal(assessment.stress_cash_buffer_usdc, field="stress_cash_buffer_usdc")
        derivative_cash_after_loss = cash - unrealized_short_loss
        cash_after_stress = derivative_cash_after_loss - initial_margin - stress_buffer
        candidate = dict(self.state)
        candidate.update(
            {
                "reserved_margin": decimal_string(initial_margin),
                "stress_cash_buffer": decimal_string(stress_buffer),
                "initial_margin_rate": decimal_string(assessment.initial_margin_rate),
                "maintenance_margin_rate": decimal_string(assessment.maintenance_margin_rate),
                "maintenance_margin": decimal_string(maintenance_margin),
                "unrealized_short_loss_to_mark": decimal_string(unrealized_short_loss),
                "derivative_cash_after_unrealized_short_loss": decimal_string(
                    derivative_cash_after_loss
                ),
                "cash_after_margin_stress": decimal_string(cash_after_stress),
                "free_cash": decimal_string(
                    derivative_cash_after_loss - initial_margin - stress_buffer
                ),
                "margin_model_version": assessment.model_version,
                "last_risk_assessment_at": utc_string(now),
            }
        )
        self.store.save_carry_dated_checkpoint(candidate)
        self.state = candidate
        return cash_after_stress

    def mark_uncertain(self, *, kind: str, message: str, now: datetime) -> None:
        candidate = dict(self.state)
        candidate.update(
            {
                "status": "ACCOUNTING_UNCERTAIN",
                "accounting_uncertain": True,
                "incident": {"kind": kind, "message": message, "detected_at": utc_string(now)},
                "last_updated_at": utc_string(now),
            }
        )
        self._commit(
            event_type="dated_accounting_uncertain",
            aggregate_id=f"uncertain:{kind}:{utc_string(now)}",
            business_payload={"kind": kind, "message": message, "as_of": utc_string(now)},
            state=candidate,
        )
