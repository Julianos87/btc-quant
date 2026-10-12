"""Frozen entry and margin policy for the Deribit BTC dated-carry PAPER study.

This module is pure: it performs no market or account I/O and submits no order.
It consumes the shared market DTOs and returns the shared ``DatedCarryDecision``.
Public rates are assumptions for PAPER, not evidence of a user's personal fees,
venue eligibility, or fills.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import TypeAlias

from btcquant.domain.carry_dated import (
    BOOK_FRESHNESS_SECONDS,
    BookLevel,
    DatedBook,
    DatedCarryDecision,
    DatedInstrument,
    SpotFeeCurrencyAssumption,
)


POLICY_VERSION = "carry_btc_dated_v1"
FEE_MODEL_VERSION = "deribit_standard_taker_delivery_2026-09-25_v1"
MARGIN_MODEL_VERSION = "deribit_ssm_linear_futures_tier4_2026-09-28_v1"
FEE_SOURCES = (
    "https://support.deribit.com/hc/en-us/articles/25944746248989-Fees",
    "https://support.deribit.com/hc/en-us/articles/31424969480093-Spot-Instruments",
)
MARGIN_SOURCES = (
    "https://support.deribit.com/hc/en-us/articles/25944811528477-Standard-Margin",
    "https://support.deribit.com/hc/en-us/articles/31424954805405-Linear-Futures",
)

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")
FOURTEEN_DAYS = Decimal("14")
NINETY_DAYS = Decimal("90")
MAX_METADATA_AGE = timedelta(hours=24)
MAX_BOOK_SKEW_SECONDS = Decimal("5")
MAX_BOOK_AGE_SECONDS = BOOK_FRESHNESS_SECONDS
ECONOMIC_RESERVE_RATE = Decimal("0.02")
DEFAULT_ADVERSE_BTC_MOVE = Decimal("0.25")


class DatedCarryPolicyError(ValueError):
    """Invalid policy parameters or inconsistent market inputs."""


def _decimal(value: Decimal | int | str, name: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise DatedCarryPolicyError(f"{name} must be a finite Decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DatedCarryPolicyError(f"{name} must be a finite Decimal") from exc
    if not result.is_finite() or (result < ZERO if allow_zero else result <= ZERO):
        qualifier = "non-negative" if allow_zero else "positive"
        raise DatedCarryPolicyError(f"{name} must be {qualifier} and finite")
    return result


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise DatedCarryPolicyError(f"{name} must be a timezone-aware UTC datetime")
    return value.astimezone(UTC)


def _floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    return (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    return (quantity / step).to_integral_value(rounding=ROUND_CEILING) * step


def _book_vwap(levels: tuple[BookLevel, ...], quantity: Decimal) -> Decimal | None:
    """Walk supplied ordered depth; return None when the requested amount is absent."""

    left = quantity
    quote = ZERO
    for level in levels:
        price = _decimal(level.price, "book price")
        available = _decimal(level.quantity, "book quantity")
        fill = min(left, available)
        quote += fill * price
        left -= fill
        if left == ZERO:
            return quote / quantity
    return None


def _levels_sorted(book: DatedBook) -> bool:
    asks = tuple(level.price for level in book.asks)
    bids = tuple(level.price for level in book.bids)
    return all(left <= right for left, right in zip(asks, asks[1:], strict=False)) and all(
        left >= right for left, right in zip(bids, bids[1:], strict=False)
    )


def _common_step(left: Decimal, right: Decimal) -> Decimal:
    """Return the least common multiple of two finite decimal increments."""

    left_exponent = left.as_tuple().exponent
    right_exponent = right.as_tuple().exponent
    if not isinstance(left_exponent, int) or not isinstance(right_exponent, int):
        raise DatedCarryPolicyError("amount steps must be finite decimal values")
    exponent = min(left_exponent, right_exponent)
    scale = Decimal(10) ** (-exponent)
    left_units = int(left * scale)
    right_units = int(right * scale)
    gcd = math.gcd(left_units, right_units)
    return Decimal(math.lcm(left_units, right_units)) / scale if gcd else max(left, right)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Public standard-tier fee assumptions; the routed spot fee asset is unknown."""

    version: str = FEE_MODEL_VERSION
    spot_taker_rate: Decimal = Decimal("0.0005")
    future_taker_rate: Decimal = Decimal("0.00035")
    delivery_rate: Decimal = Decimal("0.00025")
    spot_exit_taker_rate: Decimal = Decimal("0.0005")
    spot_fee_currency_assumption: SpotFeeCurrencyAssumption = "UNKNOWN_WORST_CASE"
    fee_sources: tuple[str, ...] = FEE_SOURCES

    def __post_init__(self) -> None:
        for name in (
            "spot_taker_rate",
            "future_taker_rate",
            "delivery_rate",
            "spot_exit_taker_rate",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, allow_zero=True))
            if getattr(self, name) >= ONE:
                raise DatedCarryPolicyError(f"{name} must be below 1")
        if not self.version or not self.fee_sources:
            raise DatedCarryPolicyError("fee model version and provenance are required")
        if self.spot_fee_currency_assumption not in {"UNKNOWN_WORST_CASE", "BASE", "QUOTE"}:
            raise DatedCarryPolicyError("unsupported spot fee currency assumption")

    @classmethod
    def standard_deribit(cls) -> FeeSchedule:
        """Use documented standard taker fees, with unknown spot debit currency."""

        return cls()


@dataclass(frozen=True, slots=True)
class MarginAssessment:
    quantity_btc: Decimal
    reference_price_usdc: Decimal
    initial_margin_rate: Decimal
    maintenance_margin_rate: Decimal
    initial_margin_usdc: Decimal
    maintenance_margin_usdc: Decimal
    stress_move_rate: Decimal
    stress_price_usdc: Decimal
    stress_short_loss_usdc: Decimal
    stressed_initial_margin_usdc: Decimal
    stress_cash_buffer_usdc: Decimal
    total_stress_collateral_usdc: Decimal
    model_version: str
    source_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarginPolicy:
    """Versioned Deribit S:SM Tier 4 margin approximation for dated BTC futures.

    ``N_max_btc`` is the currently documented public Standard Margin max amount
    (885 BTC, specs effective 2026-09-24). It is not a per-account qualification.
    The stress buffer assumes an adverse +25% index move on the short; spot gains
    are excluded from derivative collateral.
    """

    version: str = MARGIN_MODEL_VERSION
    c1: Decimal = Decimal("25")
    c2: Decimal = Decimal("4")
    c3: Decimal = Decimal("0.03")
    c4: Decimal = Decimal("0.35")
    n_max_btc: Decimal = Decimal("885")
    adverse_move_rate: Decimal = DEFAULT_ADVERSE_BTC_MOVE
    source_urls: tuple[str, ...] = MARGIN_SOURCES

    def __post_init__(self) -> None:
        for name in ("c1", "c2", "n_max_btc"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        for name in ("c3", "c4", "adverse_move_rate"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, allow_zero=True))
        if self.c1 <= self.c2 or self.c3 >= ONE or self.c4 <= ZERO:
            raise DatedCarryPolicyError("invalid Standard Margin Tier 4 parameters")
        if not self.version or not self.source_urls:
            raise DatedCarryPolicyError("margin model version and provenance are required")

    @classmethod
    def deribit_ssm_linear_btc(cls) -> MarginPolicy:
        return cls()

    def initial_margin_rate(self, quantity: Decimal) -> Decimal:
        quantity = _decimal(quantity, "quantity")
        if quantity > self.n_max_btc:
            raise DatedCarryPolicyError("position exceeds documented Standard Margin max amount")
        ratio = quantity / self.n_max_btc
        excess = max(ZERO, (ratio - self.c3) / (ONE - self.c3))
        # The published rule has a fractional exponent; perform that one term
        # in float and immediately convert back to a stable decimal string.
        exponent = math.pow(float(excess), float(self.c4)) if excess > ZERO else 0.0
        leverage = self.c1 * _decimal(
            str(math.pow(float(self.c1 / self.c2), -exponent)), "effective leverage"
        )
        return ONE / leverage

    def maintenance_margin_rate(self, quantity: Decimal) -> Decimal:
        return self.initial_margin_rate(quantity) * Decimal(2) / Decimal(3)

    def initial_margin(self, quantity: Decimal, reference_price: Decimal) -> Decimal:
        quantity = _decimal(quantity, "quantity")
        reference_price = _decimal(reference_price, "reference_price")
        return quantity * reference_price * self.initial_margin_rate(quantity)

    def maintenance_margin(self, quantity: Decimal, reference_price: Decimal) -> Decimal:
        quantity = _decimal(quantity, "quantity")
        reference_price = _decimal(reference_price, "reference_price")
        return quantity * reference_price * self.maintenance_margin_rate(quantity)

    def stress_cash_buffer(self, quantity: Decimal, reference_price: Decimal) -> Decimal:
        return self.assess(quantity, reference_price).stress_cash_buffer_usdc

    def assess(self, quantity: Decimal, reference_price: Decimal) -> MarginAssessment:
        quantity = _decimal(quantity, "quantity")
        reference_price = _decimal(reference_price, "reference_price")
        im_rate = self.initial_margin_rate(quantity)
        mm_rate = im_rate * Decimal(2) / Decimal(3)
        im = quantity * reference_price * im_rate
        mm = quantity * reference_price * mm_rate
        stress_price = reference_price * (ONE + self.adverse_move_rate)
        stress_loss = quantity * (stress_price - reference_price)
        stressed_im = quantity * stress_price * im_rate
        # IM is already segregated. Additional stress cash covers both the
        # short's adverse cash loss and any increase in IM at the shocked mark.
        buffer = max(ZERO, stress_loss + stressed_im - im)
        return MarginAssessment(
            quantity_btc=quantity,
            reference_price_usdc=reference_price,
            initial_margin_rate=im_rate,
            maintenance_margin_rate=mm_rate,
            initial_margin_usdc=im,
            maintenance_margin_usdc=mm,
            stress_move_rate=self.adverse_move_rate,
            stress_price_usdc=stress_price,
            stress_short_loss_usdc=stress_loss,
            stressed_initial_margin_usdc=stressed_im,
            stress_cash_buffer_usdc=buffer,
            total_stress_collateral_usdc=im + buffer,
            model_version=self.version,
            source_urls=self.source_urls,
        )


Candidate: TypeAlias = tuple[DatedInstrument, DatedInstrument, DatedBook, DatedBook]


@dataclass(frozen=True, slots=True)
class _Quote:
    spot_instrument: DatedInstrument
    future_instrument: DatedInstrument
    spot_book: DatedBook
    future_book: DatedBook


@dataclass(frozen=True, slots=True)
class _Sizing:
    quantity: Decimal
    spot_gross_quantity: Decimal
    spot_net_base_fee_quantity: Decimal
    hedge_residual: Decimal
    spot_vwap: Decimal
    future_vwap: Decimal
    spot_exit_vwap_proxy: Decimal
    spot_cash: Decimal
    base_fee_case_cash: Decimal
    margin: MarginAssessment
    spot_fee: Decimal
    future_fee: Decimal
    delivery_fee: Decimal
    exit_fee: Decimal
    fees_total: Decimal
    exit_execution_reserve: Decimal
    total_cash: Decimal


def _quote_id(quote: _Quote) -> tuple[datetime, str]:
    expiry = quote.future_instrument.expiration_utc
    if expiry is None:
        raise DatedCarryPolicyError("dated future must have an expiry")
    return expiry, quote.future_instrument.instrument_id


def _valid_spot(instrument: DatedInstrument) -> bool:
    return (
        instrument.venue.lower() == "deribit"
        and instrument.kind == "spot"
        and instrument.active
        and instrument.instrument_state == "open"
        and instrument.base.upper() == "BTC"
        and instrument.quote.upper() == "USDC"
        and instrument.instrument_id == "BTC_USDC"
        and instrument.expiration_utc is None
    )


def _valid_future(instrument: DatedInstrument) -> bool:
    return (
        instrument.venue.lower() == "deribit"
        and instrument.kind == "dated_future"
        and instrument.active
        and instrument.instrument_state == "open"
        and instrument.base.upper() == "BTC"
        and instrument.quote.upper() == "USDC"
        and instrument.settlement_currency is not None
        and instrument.settlement_currency.upper() == "USDC"
        and instrument.expiration_utc is not None
    )


def _instrument_fresh(instrument: DatedInstrument, now: datetime) -> bool:
    age = now - instrument.metadata_timestamp
    return timedelta(0) <= age <= MAX_METADATA_AGE


def _book_is_eligible(book: DatedBook, now: datetime) -> bool:
    return (
        not book.quality_reasons(now=now, max_age_seconds=MAX_BOOK_AGE_SECONDS)
        and book.received_timestamp <= now
        and bool(book.content_hash)
        and _levels_sorted(book)
    )


def _step_for(spot: DatedInstrument, future: DatedInstrument) -> Decimal:
    return _common_step(spot.amount_step, future.amount_step)


def _size(
    quote: _Quote,
    quantity: Decimal,
    *,
    capital: Decimal,
    fees: FeeSchedule,
    margin_policy: MarginPolicy,
    exclude_spot_fee_cash: bool = False,
) -> _Sizing | None:
    spot = quote.spot_instrument
    future = quote.future_instrument
    spot_book = quote.spot_book
    future_book = quote.future_book
    spot_rate = max(fees.spot_taker_rate, spot.taker_commission or ZERO)
    spot_exit_rate = max(fees.spot_exit_taker_rate, spot.taker_commission or ZERO)
    future_rate = max(fees.future_taker_rate, future.taker_commission or ZERO)

    # The route's fee asset is not public/account-qualified. Size the short
    # against the adverse BASE-fee case, while budget also funds the QUOTE-fee
    # case. Any quote-case long residual is explicit in the returned decision.
    spot_gross = _ceil_to_step(quantity / (ONE - spot_rate), spot.amount_step)
    spot_vwap = _book_vwap(spot_book.asks, spot_gross)
    future_vwap = _book_vwap(future_book.bids, quantity)
    spot_exit_vwap = _book_vwap(spot_book.bids, spot_gross)
    if spot_vwap is None or future_vwap is None or spot_exit_vwap is None:
        return None
    if future_book.mark_price is None:
        return None

    if spot_gross < spot.min_amount or quantity < future.min_amount:
        return None

    # Deribit spot fee currency is unknown: quote debit is the worst cash case;
    # base debit produces the same entry-price USDC-equivalent fee but less BTC.
    spot_notional = spot_gross * spot_vwap
    spot_fee = spot_notional * spot_rate
    future_fee = quantity * future_vwap * future_rate
    delivery_fee = quantity * future_vwap * fees.delivery_rate
    exit_fee = spot_gross * spot_exit_vwap * spot_exit_rate
    all_fees = spot_fee + future_fee + delivery_fee + exit_fee
    margin = margin_policy.assess(quantity, future_book.mark_price)
    spot_cash = spot_notional
    base_fee_case_cash = (
        spot_cash
        + future_fee
        + delivery_fee
        + exit_fee
        + margin.initial_margin_usdc
        + margin.stress_cash_buffer_usdc
    )
    # Exit execution reserve is an immediate-book proxy for future spot
    # liquidation friction. It is not a forecasted future fill or convergence.
    exit_execution_reserve = max(ZERO, spot_gross * (spot_vwap - spot_exit_vwap))
    cash_fees = all_fees - spot_fee if exclude_spot_fee_cash else all_fees
    total_cash = spot_cash + cash_fees + margin.initial_margin_usdc + margin.stress_cash_buffer_usdc
    return _Sizing(
        quantity=quantity,
        spot_gross_quantity=spot_gross,
        spot_net_base_fee_quantity=spot_gross * (ONE - spot_rate),
        hedge_residual=max(
            abs(spot_gross * (ONE - spot_rate) - quantity),
            abs(spot_gross - quantity),
        ),
        spot_vwap=spot_vwap,
        future_vwap=future_vwap,
        spot_exit_vwap_proxy=spot_exit_vwap,
        spot_cash=spot_cash,
        base_fee_case_cash=base_fee_case_cash,
        margin=margin,
        spot_fee=spot_fee,
        future_fee=future_fee,
        delivery_fee=delivery_fee,
        exit_fee=exit_fee,
        fees_total=all_fees,
        exit_execution_reserve=exit_execution_reserve,
        total_cash=total_cash,
    )


def _best_sizing(
    quote: _Quote,
    *,
    capital: Decimal,
    fees: FeeSchedule,
    margin_policy: MarginPolicy,
    exclude_spot_fee_cash: bool = False,
) -> _Sizing | None:
    step = _step_for(quote.spot_instrument, quote.future_instrument)
    base_fee_rate = max(fees.spot_taker_rate, quote.spot_instrument.taker_commission or ZERO)
    spot_cap = sum((level.quantity for level in quote.spot_book.asks), ZERO) * (ONE - base_fee_rate)
    future_cap = sum((level.quantity for level in quote.future_book.bids), ZERO)
    upper = min(spot_cap, future_cap, margin_policy.n_max_btc)
    count = int((upper / step).to_integral_value(rounding=ROUND_FLOOR))
    min_count = max(
        int((quote.spot_instrument.min_amount / step).to_integral_value(rounding=ROUND_CEILING)),
        int((quote.future_instrument.min_amount / step).to_integral_value(rounding=ROUND_CEILING)),
        1,
    )
    if count < min_count:
        return None

    # Every cash obligation is non-decreasing with quantity. Binary search the
    # discrete lot count so depth can be sizeable without iterating 1e-8 steps.
    low, high = min_count, count
    best: _Sizing | None = None
    while low <= high:
        mid = (low + high) // 2
        candidate = _size(
            quote,
            Decimal(mid) * step,
            capital=capital,
            fees=fees,
            margin_policy=margin_policy,
            exclude_spot_fee_cash=exclude_spot_fee_cash,
        )
        if candidate is not None and candidate.total_cash <= capital:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _decision_id(
    quote: _Quote | None,
    reason: str,
    *,
    capital: Decimal,
    fee_schedule: FeeSchedule,
    margin_policy: MarginPolicy,
) -> str:
    if quote is None:
        identity = "none"
    else:
        identity = "|".join(
            (
                quote.spot_instrument.instrument_id,
                quote.future_instrument.instrument_id,
                *sorted(_source_hashes(quote)),
            )
        )
    stable_identity = "|".join(
        (
            POLICY_VERSION,
            identity,
            reason,
            format(capital, "f"),
            fee_schedule.version,
            margin_policy.version,
        )
    )
    return hashlib.sha256(stable_identity.encode()).hexdigest()[:24]


def _wait(
    *,
    now: datetime,
    capital: Decimal,
    reason: str,
    message: str,
    quote: _Quote | None = None,
    fee_schedule: FeeSchedule,
    margin_policy: MarginPolicy,
    gross_premium: Decimal = ZERO,
    spot_depth: Decimal = ZERO,
    future_depth: Decimal = ZERO,
    spot_price: Decimal | None = None,
    future_price: Decimal | None = None,
    spot_time: datetime | None = None,
    future_time: datetime | None = None,
    sizing: _Sizing | None = None,
    net_before: Decimal | None = None,
) -> DatedCarryDecision:
    reserve = capital * ECONOMIC_RESERVE_RATE
    expiry = quote.future_instrument.expiration_utc if quote is not None else None
    return DatedCarryDecision(
        decision_id=_decision_id(
            quote,
            reason,
            capital=capital,
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        ),
        decision_at=now,
        action="WAIT",
        reason_code=reason,
        reason_message=message,
        as_of=now,
        selected_instrument_id=quote.future_instrument.instrument_id if quote else None,
        expiry_utc=expiry,
        quantity=sizing.quantity if sizing else ZERO,
        spot_estimated_price=spot_price,
        future_estimated_price=future_price,
        spot_depth_quantity=spot_depth,
        future_depth_quantity=future_depth,
        capital=capital,
        free_cash=capital,
        spot_cash_required=sizing.spot_cash if sizing else ZERO,
        margin_cash=sizing.margin.initial_margin_usdc if sizing else ZERO,
        stress_cash=sizing.margin.stress_cash_buffer_usdc if sizing else ZERO,
        fees=sizing.fees_total if sizing else None,
        gross_premium=gross_premium,
        net_before_reserve=net_before,
        risk_reserve=reserve,
        net_after_reserve=(net_before - reserve) if net_before is not None else None,
        policy_version=POLICY_VERSION,
        spot_market_timestamp=spot_time,
        future_market_timestamp=future_time,
        spot_fee=sizing.spot_fee if sizing else None,
        future_entry_fee=sizing.future_fee if sizing else None,
        delivery_fee_estimate=sizing.delivery_fee if sizing else None,
        exit_fee_estimate=sizing.exit_fee if sizing else None,
        exit_execution_reserve=sizing.exit_execution_reserve if sizing else ZERO,
        initial_margin_rate=(sizing.margin.initial_margin_rate if sizing else None),
        maintenance_margin_rate=(sizing.margin.maintenance_margin_rate if sizing else None),
        total_cash_required=sizing.total_cash if sizing else ZERO,
        cash_after_entry=(capital - sizing.total_cash) if sizing else capital,
        fee_model_version=fee_schedule.version,
        margin_model_version=margin_policy.version,
        source_hashes=_source_hashes(quote) if quote else (),
        spot_fee_currency_assumption=fee_schedule.spot_fee_currency_assumption,
        spot_gross_quantity=sizing.spot_gross_quantity if sizing else ZERO,
        spot_expected_net_quantity=sizing.spot_net_base_fee_quantity if sizing else ZERO,
        expected_hedge_residual=sizing.hedge_residual if sizing else ZERO,
    )


def _source_hashes(quote: _Quote) -> tuple[str, ...]:
    values = (
        quote.spot_instrument.metadata_hash,
        quote.future_instrument.metadata_hash,
        quote.spot_book.content_hash,
        quote.future_book.content_hash,
    )
    return tuple(value for value in values if value)


def evaluate_entry(
    candidates: Iterable[Candidate],
    *,
    capital: Decimal,
    fee_schedule: FeeSchedule,
    margin_policy: MarginPolicy,
    now: datetime,
) -> DatedCarryDecision:
    """Choose one expiry deterministically and score one fully funded PAPER entry.

    Candidate items are ``(spot_instrument, future_instrument, spot_book,
    future_book)`` tuples of the shared domain types. The shortest eligible
    expiry is selected before examining its premium or executable depth. If that
    selected expiry has bad data/depth, the result is WAIT; the function does
    not fall through to another expiry.
    """

    now = _utc(now, "now")
    capital = _decimal(capital, "capital")
    candidate_list = list(candidates)
    if not candidate_list:
        return _wait(
            now=now,
            capital=capital,
            reason="NO_CANDIDATE",
            message="Aucun instrument spot/future candidat fourni.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        )
    if any(len(item) != 4 for item in candidate_list):
        raise DatedCarryPolicyError(
            "each candidate must contain spot instrument, future instrument, and both books"
        )

    valid_quotes = [
        _Quote(spot_instrument, future_instrument, spot_book, future_book)
        for spot_instrument, future_instrument, spot_book, future_book in candidate_list
        if _valid_spot(spot_instrument) and _valid_future(future_instrument)
    ]
    spot_ids = {item.spot_instrument.instrument_id for item in valid_quotes}
    if len(spot_ids) != 1:
        reason = "NO_ELIGIBLE_SPOT" if not spot_ids else "AMBIGUOUS_SPOT_INSTRUMENT"
        return _wait(
            now=now,
            capital=capital,
            reason=reason,
            message="Il faut exactement un spot BTC_USDC actif compatible.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        )

    eligible = [
        quote
        for quote in valid_quotes
        if quote.future_instrument.expiration_utc is not None
        and FOURTEEN_DAYS
        <= Decimal(str((quote.future_instrument.expiration_utc - now).total_seconds()))
        / Decimal("86400")
        <= NINETY_DAYS
    ]
    if not eligible:
        return _wait(
            now=now,
            capital=capital,
            reason="NO_ELIGIBLE_EXPIRY",
            message="Aucun future BTC_USDC actif n’échoit entre 14 et 90 jours.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        )
    eligible_ids = [quote.future_instrument.instrument_id for quote in eligible]
    if len(set(eligible_ids)) != len(eligible_ids):
        return _wait(
            now=now,
            capital=capital,
            reason="DUPLICATE_FUTURE_CANDIDATE",
            message="Plusieurs snapshots contradictoires existent pour le même future candidat.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        )
    quote = min(eligible, key=_quote_id)

    if (
        quote.spot_book.instrument_id != quote.spot_instrument.instrument_id
        or quote.future_book.instrument_id != quote.future_instrument.instrument_id
    ):
        return _wait(
            now=now,
            capital=capital,
            quote=quote,
            reason="BOOK_INSTRUMENT_MISMATCH",
            message="Un carnet ne correspond pas à l’identité de son instrument.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        )

    if not _instrument_fresh(quote.spot_instrument, now) or not _instrument_fresh(
        quote.future_instrument, now
    ):
        return _wait(
            now=now,
            capital=capital,
            quote=quote,
            reason="METADATA_STALE_OR_FUTURE",
            message="Les métadonnées de l’instrument sont périmées ou datées dans le futur.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        )
    if not _book_is_eligible(quote.spot_book, now) or not _book_is_eligible(quote.future_book, now):
        return _wait(
            now=now,
            capital=capital,
            quote=quote,
            reason="BOOK_INVALID_OR_STALE",
            message="Un des carnets est invalide, périmé, futur ou non trié.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        )
    skew = abs(
        Decimal(
            str(
                (
                    quote.spot_book.market_timestamp - quote.future_book.market_timestamp
                ).total_seconds()
            )
        )
    )
    if skew > MAX_BOOK_SKEW_SECONDS:
        return _wait(
            now=now,
            capital=capital,
            quote=quote,
            reason="BOOKS_DESYNCHRONIZED",
            message="Les horodatages des carnets spot et future diffèrent de plus de 5 secondes.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        )
    if quote.future_book.mark_price is None or quote.future_book.index_price is None:
        return _wait(
            now=now,
            capital=capital,
            quote=quote,
            reason="MISSING_FUTURE_MARK_OR_INDEX",
            message="Le mark et l’index du future sont requis pour calculer la marge.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        )
    if any(
        rate is not None and (rate < ZERO or rate >= ONE)
        for rate in (
            quote.spot_instrument.taker_commission,
            quote.future_instrument.taker_commission,
        )
    ):
        return _wait(
            now=now,
            capital=capital,
            quote=quote,
            reason="INVALID_SPOT_FEE_METADATA",
            message="Un taux de frais instrument fourni par les métadonnées est invalide.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        )

    best = _best_sizing(quote, capital=capital, fees=fee_schedule, margin_policy=margin_policy)
    if best is None:
        base_fee_scenario = _best_sizing(
            quote,
            capital=capital,
            fees=fee_schedule,
            margin_policy=margin_policy,
            exclude_spot_fee_cash=True,
        )
        reason = (
            "WAIT_FEE_CURRENCY_UNCERTAIN"
            if base_fee_scenario is not None
            else "INSUFFICIENT_DEPTH_OR_CASH"
        )
        return _wait(
            now=now,
            capital=capital,
            quote=quote,
            reason=reason,
            message=(
                "Le scénario de débit BASE seul tient le budget, mais la devise inconnue du frais spot empêche de valider le budget prudent."
                if reason == "WAIT_FEE_CURRENCY_UNCERTAIN"
                else "Les tailles minimales, la profondeur et le cash ne permettent pas de financer les deux jambes et le stress de marge."
            ),
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
            spot_depth=sum((level.quantity for level in quote.spot_book.asks), ZERO),
            future_depth=sum((level.quantity for level in quote.future_book.bids), ZERO),
            sizing=base_fee_scenario,
        )
    gross_premium = best.quantity * (best.future_vwap - best.spot_vwap)
    net_before = gross_premium - best.fees_total - best.exit_execution_reserve
    reserve = capital * ECONOMIC_RESERVE_RATE
    net_after = net_before - reserve
    if best.future_vwap <= best.spot_vwap:
        return _wait(
            now=now,
            capital=capital,
            reason="NON_POSITIVE_EXECUTABLE_BASIS",
            message="La prime exécutable future moins spot est non positive.",
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
            quote=quote,
            spot_price=best.spot_vwap,
            future_price=best.future_vwap,
            spot_time=quote.spot_book.market_timestamp,
            future_time=quote.future_book.market_timestamp,
            sizing=best,
            gross_premium=gross_premium,
            spot_depth=sum((level.quantity for level in quote.spot_book.asks), ZERO),
            future_depth=sum((level.quantity for level in quote.future_book.bids), ZERO),
            net_before=net_before,
        )
    if net_after <= ZERO:
        reason = (
            "WAIT_FEE_CURRENCY_UNCERTAIN"
            if best.total_cash > capital
            else "ECONOMIC_HURDLE_NOT_MET"
        )
        return _wait(
            now=now,
            capital=capital,
            reason=reason,
            message=(
                "La prime ne couvre pas les frais, la friction de sortie proxy et le seuil de réserve de 2% du capital."
                if reason == "ECONOMIC_HURDLE_NOT_MET"
                else "Le scénario prudent de monnaie du frais spot ne tient pas dans le capital disponible."
            ),
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
            quote=quote,
            spot_price=best.spot_vwap,
            future_price=best.future_vwap,
            spot_time=quote.spot_book.market_timestamp,
            future_time=quote.future_book.market_timestamp,
            sizing=best,
            gross_premium=gross_premium,
            spot_depth=sum((level.quantity for level in quote.spot_book.asks), ZERO),
            future_depth=sum((level.quantity for level in quote.future_book.bids), ZERO),
            net_before=net_before,
        )

    return DatedCarryDecision(
        decision_id=_decision_id(
            quote,
            "OPEN",
            capital=capital,
            fee_schedule=fee_schedule,
            margin_policy=margin_policy,
        ),
        decision_at=now,
        action="OPEN",
        reason_code="ECONOMIC_AND_CASH_GATES_PASSED_UNDER_PAPER_ASSUMPTIONS",
        reason_message="Les seuils d’entrée PAPER passent sous le modèle de frais/marge versionné; cela ne prouve ni fill réel ni rentabilité.",
        as_of=now,
        selected_instrument_id=quote.future_instrument.instrument_id,
        expiry_utc=quote.future_instrument.expiration_utc,
        quantity=best.quantity,
        spot_estimated_price=best.spot_vwap,
        future_estimated_price=best.future_vwap,
        spot_depth_quantity=sum((level.quantity for level in quote.spot_book.asks), ZERO),
        future_depth_quantity=sum((level.quantity for level in quote.future_book.bids), ZERO),
        capital=capital,
        free_cash=capital,
        spot_cash_required=best.spot_cash,
        margin_cash=best.margin.initial_margin_usdc,
        stress_cash=best.margin.stress_cash_buffer_usdc,
        fees=best.fees_total,
        gross_premium=gross_premium,
        net_before_reserve=net_before,
        risk_reserve=reserve,
        net_after_reserve=net_after,
        policy_version=POLICY_VERSION,
        spot_market_timestamp=quote.spot_book.market_timestamp,
        future_market_timestamp=quote.future_book.market_timestamp,
        spot_fee=best.spot_fee,
        future_entry_fee=best.future_fee,
        delivery_fee_estimate=best.delivery_fee,
        exit_fee_estimate=best.exit_fee,
        exit_execution_reserve=best.exit_execution_reserve,
        initial_margin_rate=best.margin.initial_margin_rate,
        maintenance_margin_rate=best.margin.maintenance_margin_rate,
        total_cash_required=best.total_cash,
        cash_after_entry=capital - best.total_cash,
        fee_model_version=fee_schedule.version,
        margin_model_version=margin_policy.version,
        source_hashes=_source_hashes(quote),
        spot_fee_currency_assumption=fee_schedule.spot_fee_currency_assumption,
        spot_gross_quantity=best.spot_gross_quantity,
        spot_expected_net_quantity=best.spot_net_base_fee_quantity,
        expected_hedge_residual=best.hedge_residual,
    )


__all__ = [
    "Candidate",
    "DatedCarryPolicyError",
    "FeeSchedule",
    "MarginAssessment",
    "MarginPolicy",
    "evaluate_entry",
]
