"""Deterministic, expiry-only replay for dated BTC cash-and-carry PAPER.

The replay consumes explicit PAPER fill evidence and official settlement DTOs.
It never substitutes marks, mids, bars, or index prices for spot fills. A
mechanical replay is not economic evidence; this module therefore labels every
result ``INSUFFICIENT_EVIDENCE`` until an external governed study qualifies it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from btcquant.domain.carry_dated import (
    BOOK_FRESHNESS_SECONDS,
    BookLevel,
    DatedBook,
    DatedCarryDecision,
    DatedSettlement,
)
from btcquant.domain.carry_dated_policy import FeeSchedule, MarginAssessment, MarginPolicy

ZERO = Decimal("0")
ONE = Decimal("1")
FIVE_PERCENT = Decimal("0.05")
MAX_EXIT_BOOK_DELAY = timedelta(minutes=5)
MAX_SETTLEMENT_INTERVAL_DRIFT = timedelta(seconds=1)
FeeCurrencyScenario = Literal["BASE", "QUOTE"]


@dataclass(frozen=True, slots=True)
class SettlementCashFlow:
    settlement_key: str
    kind: str
    period_end: datetime
    reference_before: Decimal
    official_index_price: Decimal
    cash_pnl: Decimal
    content_hash: str


@dataclass(frozen=True, slots=True)
class DatedCarryReplayResult:
    status: str
    reason_code: str
    reason_message: str
    economic_status: Literal["INSUFFICIENT_EVIDENCE"]
    execution_label: str
    instrument_id: str
    quantity_future_btc: Decimal
    spot_gross_quantity_btc: Decimal
    spot_net_quantity_btc: Decimal
    hedge_residual_btc: Decimal
    spot_fee_currency_scenario: FeeCurrencyScenario
    spot_exit_fee_currency_scenario: FeeCurrencyScenario
    spot_entry_fee_btc: Decimal
    spot_entry_fee_quote_equivalent: Decimal
    spot_entry_fee_quote_charged: Decimal
    future_entry_fee_usdc: Decimal
    delivery_fee_usdc: Decimal
    spot_exit_fee_usdc: Decimal
    spot_exit_fee_btc: Decimal
    spot_exit_fee_quote_charged_usdc: Decimal
    spot_exit_trade_quantity_btc: Decimal
    spot_residual_quantity_btc: Decimal
    settlement_flows: tuple[SettlementCashFlow, ...]
    future_settlement_pnl_usdc: Decimal
    spot_realized_pnl_usdc: Decimal | None
    net_pnl_usdc: Decimal | None
    allocated_capital_usdc: Decimal
    admission_reserve_usdc: Decimal
    initial_margin_cash_usdc: Decimal
    stress_cash_buffer_usdc: Decimal
    margin_is_cost: bool
    terminal_basis_stress_rate: Decimal
    terminal_basis_stress_loss_usdc: Decimal | None
    net_pnl_under_terminal_basis_stress_usdc: Decimal | None
    terminal_basis_stress_covered_by_reserve: bool
    terminal_spot_exit_price_usdc: Decimal | None
    source_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProtectiveObservation:
    observed_at: datetime
    last_settlement_reference_price: Decimal
    # Cash before the current short's unrealized mark-to-market PnL is applied.
    derivative_cash_available_usdc: Decimal
    future_book: DatedBook
    spot_book: DatedBook


@dataclass(frozen=True, slots=True)
class ProtectiveCloseResult:
    status: str
    reason_code: str
    triggered: bool
    available_derivative_cash_usdc: Decimal
    realized_short_loss_since_settlement_usdc: Decimal
    cash_after_unrealized_loss_usdc: Decimal
    required_stress_collateral_usdc: Decimal
    future_quantity_closed_btc: Decimal
    future_quantity_remaining_btc: Decimal
    future_cover_vwap_usdc: Decimal | None
    spot_quantity_sold_btc: Decimal
    spot_quantity_remaining_btc: Decimal
    spot_sale_vwap_usdc: Decimal | None
    future_close_cash_pnl_usdc: Decimal
    spot_realized_pnl_usdc: Decimal
    fees_usdc: Decimal
    net_realized_pnl_usdc: Decimal
    source_hashes: tuple[str, ...]
    spot_fee_currency_scenario: FeeCurrencyScenario = "BASE"
    spot_exit_fee_currency_scenario: FeeCurrencyScenario = "BASE"
    spot_entry_fee_quote_equivalent_usdc: Decimal = ZERO
    spot_exit_fee_btc: Decimal = ZERO
    spot_exit_fee_quote_charged_usdc: Decimal = ZERO
    spot_exit_fee_quote_equivalent_usdc: Decimal = ZERO


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    return value.astimezone(UTC)


def _positive(value: Decimal, name: str) -> Decimal:
    if not value.is_finite() or value <= ZERO:
        raise ValueError(f"{name} must be positive and finite")
    return value


def _walk_vwap(levels: tuple[BookLevel, ...], quantity: Decimal) -> Decimal | None:
    remaining = quantity
    notional = ZERO
    for level in levels:
        take = min(remaining, level.quantity)
        notional += take * level.price
        remaining -= take
        if remaining == ZERO:
            return notional / quantity
    return None


def _walk_partial(
    levels: tuple[BookLevel, ...], quantity: Decimal
) -> tuple[Decimal, Decimal | None]:
    remaining = quantity
    filled = ZERO
    notional = ZERO
    for level in levels:
        take = min(remaining, level.quantity)
        filled += take
        notional += take * level.price
        remaining -= take
        if remaining == ZERO:
            break
    return filled, (notional / filled if filled > ZERO else None)


def _floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    return (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _hashes(*values: str | None) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))


def _daily_settlement_times(entry_at: datetime, expiry_at: datetime) -> tuple[datetime, ...]:
    next_time = entry_at.replace(hour=8, minute=0, second=0, microsecond=0)
    if next_time <= entry_at:
        next_time += timedelta(days=1)
    result: list[datetime] = []
    while next_time < expiry_at:
        result.append(next_time)
        next_time += timedelta(days=1)
    return tuple(result)


def _settlement_validation(
    settlements: Iterable[DatedSettlement],
    *,
    venue: str,
    instrument_id: str,
    entry_at: datetime,
    expiry_at: datetime,
) -> tuple[tuple[DatedSettlement, ...], str | None]:
    input_events = tuple(settlements)
    unique: dict[str, DatedSettlement] = {}
    for event in input_events:
        if event.venue.lower() != venue.lower() or event.instrument_id != instrument_id:
            return (), "SETTLEMENT_INSTRUMENT_MISMATCH"
        if event.received_at < event.event_timestamp:
            return (), "SETTLEMENT_RECEIVED_BEFORE_EVENT"
        prior = unique.get(event.settlement_key)
        if prior is not None:
            if (
                prior.content_hash != event.content_hash
                or prior.correction_version != event.correction_version
                or prior.official_index_price != event.official_index_price
            ):
                return (), "SETTLEMENT_CORRECTION_REQUIRES_REPLAY"
            continue
        unique[event.settlement_key] = event

    ordered = tuple(
        sorted(unique.values(), key=lambda event: (event.period_end, event.settlement_key))
    )
    expiry_events = tuple(event for event in ordered if event.kind == "expiry")
    if len(expiry_events) != 1:
        return ordered, "EXPIRY_SETTLEMENT_MISSING_OR_AMBIGUOUS"
    expiry_event = expiry_events[0]
    if expiry_event.period_end != expiry_at:
        return ordered, "EXPIRY_SETTLEMENT_TIME_MISMATCH"
    daily_events = tuple(event for event in ordered if event.kind == "daily")
    if any(event.period_end >= expiry_at for event in daily_events):
        return ordered, "DAILY_SETTLEMENT_AFTER_EXPIRY"
    expected_daily = _daily_settlement_times(entry_at, expiry_at)
    observed_daily = tuple(event.period_end for event in daily_events)
    if len(expected_daily) != len(observed_daily):
        return ordered, "SETTLEMENT_BACKLOG"
    for expected, observed in zip(expected_daily, observed_daily, strict=True):
        if abs(expected - observed) > MAX_SETTLEMENT_INTERVAL_DRIFT:
            return ordered, "SETTLEMENT_BACKLOG"
    if tuple(event.period_end for event in ordered) != tuple(
        sorted(event.period_end for event in ordered)
    ):
        return ordered, "SETTLEMENT_ORDER_INVALID"
    return ordered, None


def _incomplete_result(
    *,
    decision: DatedCarryDecision,
    execution_label: str,
    scenario: FeeCurrencyScenario,
    capital: Decimal,
    margin_assessment: MarginAssessment,
    reason: str,
    exit_scenario: FeeCurrencyScenario | None = None,
    flows: tuple[SettlementCashFlow, ...] = (),
    future_pnl: Decimal = ZERO,
    spot_fee_btc: Decimal = ZERO,
    spot_fee_equiv: Decimal = ZERO,
    spot_fee_quote: Decimal = ZERO,
    future_fee: Decimal = ZERO,
    spot_net_quantity: Decimal = ZERO,
    hedge_residual: Decimal = ZERO,
    source_hashes: tuple[str, ...] = (),
) -> DatedCarryReplayResult:
    return DatedCarryReplayResult(
        status="PENDING_ACCOUNTING",
        reason_code=reason,
        reason_message="A required fill, official settlement, or observable liquidation book is missing or ambiguous.",
        economic_status="INSUFFICIENT_EVIDENCE",
        execution_label=execution_label,
        instrument_id=decision.selected_instrument_id or "",
        quantity_future_btc=decision.quantity,
        spot_gross_quantity_btc=decision.spot_gross_quantity,
        spot_net_quantity_btc=spot_net_quantity,
        hedge_residual_btc=hedge_residual,
        spot_fee_currency_scenario=scenario,
        spot_exit_fee_currency_scenario=exit_scenario or scenario,
        spot_entry_fee_btc=spot_fee_btc,
        spot_entry_fee_quote_equivalent=spot_fee_equiv,
        spot_entry_fee_quote_charged=spot_fee_quote,
        future_entry_fee_usdc=future_fee,
        delivery_fee_usdc=ZERO,
        spot_exit_fee_usdc=ZERO,
        spot_exit_fee_btc=ZERO,
        spot_exit_fee_quote_charged_usdc=ZERO,
        spot_exit_trade_quantity_btc=ZERO,
        spot_residual_quantity_btc=spot_net_quantity,
        settlement_flows=flows,
        future_settlement_pnl_usdc=future_pnl,
        spot_realized_pnl_usdc=None,
        net_pnl_usdc=None,
        allocated_capital_usdc=capital,
        admission_reserve_usdc=capital * Decimal("0.02"),
        initial_margin_cash_usdc=margin_assessment.initial_margin_usdc,
        stress_cash_buffer_usdc=margin_assessment.stress_cash_buffer_usdc,
        margin_is_cost=False,
        terminal_basis_stress_rate=FIVE_PERCENT,
        terminal_basis_stress_loss_usdc=None,
        net_pnl_under_terminal_basis_stress_usdc=None,
        terminal_basis_stress_covered_by_reserve=False,
        terminal_spot_exit_price_usdc=None,
        source_hashes=source_hashes,
    )


def replay_expiry_cycle(
    *,
    decision: DatedCarryDecision,
    entry_filled_at: datetime,
    spot_entry_fill_quantity: Decimal,
    spot_entry_fill_vwap: Decimal,
    future_entry_fill_quantity: Decimal,
    future_entry_fill_vwap: Decimal,
    entry_margin_reference_price: Decimal,
    settlements: Iterable[DatedSettlement],
    expiry_spot_book: DatedBook,
    spot_fee_currency_scenario: FeeCurrencyScenario,
    spot_exit_fee_currency_scenario: FeeCurrencyScenario,
    spot_amount_step: Decimal,
    execution_label: Literal["FIXTURE", "PAPER_FILL_EVIDENCE"],
    capital: Decimal,
    fee_schedule: FeeSchedule,
    margin_policy: MarginPolicy,
    spot_instrument_id: str = "BTC_USDC",
    terminal_basis_stress_rate: Decimal = FIVE_PERCENT,
) -> DatedCarryReplayResult:
    """Replay one expiry-only cycle with exact-once daily cash settlements.

    ``spot_entry_fill_quantity`` is the gross BTC actually bought. In BASE fee
    mode the taker fee reduces BTC received; in QUOTE mode it reduces cash.
    ``expiry_spot_book`` must be a fresh, hashed, observed BTC_USDC bid book
    timestamped in the five-minute window after official expiry settlement.
    Entry and exit spot fee currency are separate scenarios because the public
    route does not establish either debit asset. BASE exit fees consume BTC in
    addition to sold BTC; quantities are rounded down to the supplied step.
    """

    entry_filled_at = _utc(entry_filled_at, "entry_filled_at")
    if (
        decision.action != "OPEN"
        or decision.expiry_utc is None
        or not decision.selected_instrument_id
    ):
        raise ValueError("replay requires a frozen OPEN decision for a dated instrument")
    if spot_fee_currency_scenario not in ("BASE", "QUOTE"):
        raise ValueError("fee currency scenario must be BASE or QUOTE; unknown is replayed twice")
    exit_scenario = spot_exit_fee_currency_scenario
    if exit_scenario not in ("BASE", "QUOTE"):
        raise ValueError("spot exit fee currency scenario must be BASE or QUOTE")
    spot_amount_step = _positive(spot_amount_step, "spot_amount_step")
    if not execution_label:
        raise ValueError("execution evidence label is required")
    capital = _positive(capital, "capital")
    spot_entry_fill_quantity = _positive(spot_entry_fill_quantity, "spot_entry_fill_quantity")
    spot_entry_fill_vwap = _positive(spot_entry_fill_vwap, "spot_entry_fill_vwap")
    future_entry_fill_quantity = _positive(future_entry_fill_quantity, "future_entry_fill_quantity")
    future_entry_fill_vwap = _positive(future_entry_fill_vwap, "future_entry_fill_vwap")
    entry_margin_reference_price = _positive(
        entry_margin_reference_price, "entry_margin_reference_price"
    )
    terminal_basis_stress_rate = _positive(terminal_basis_stress_rate, "terminal_basis_stress_rate")
    expiry_at = decision.expiry_utc
    if entry_filled_at >= expiry_at:
        raise ValueError("entry fill must occur before expiry")

    margin_assessment = margin_policy.assess(
        future_entry_fill_quantity, entry_margin_reference_price
    )
    spot_rate = fee_schedule.spot_taker_rate
    spot_fee_equiv = spot_entry_fill_quantity * spot_entry_fill_vwap * spot_rate
    spot_fee_btc = (
        spot_entry_fill_quantity * spot_rate if spot_fee_currency_scenario == "BASE" else ZERO
    )
    spot_net_quantity = spot_entry_fill_quantity - spot_fee_btc
    spot_fee_quote_charged = spot_fee_equiv if spot_fee_currency_scenario == "QUOTE" else ZERO
    quantity_tolerance = Decimal("0.00000001")
    hedge_residual = spot_net_quantity - future_entry_fill_quantity
    if future_entry_fill_quantity > decision.quantity + quantity_tolerance:
        return _incomplete_result(
            decision=decision,
            execution_label=execution_label,
            scenario=spot_fee_currency_scenario,
            exit_scenario=exit_scenario,
            capital=capital,
            margin_assessment=margin_assessment,
            reason="FUTURE_ENTRY_OVERFILL",
            spot_fee_btc=spot_fee_btc,
            spot_fee_equiv=spot_fee_equiv,
            spot_fee_quote=spot_fee_quote_charged,
            spot_net_quantity=spot_net_quantity,
            hedge_residual=hedge_residual,
            source_hashes=decision.source_hashes,
        )
    if expiry_spot_book.instrument_id != spot_instrument_id:
        return _incomplete_result(
            decision=decision,
            execution_label=execution_label,
            scenario=spot_fee_currency_scenario,
            exit_scenario=exit_scenario,
            capital=capital,
            margin_assessment=margin_assessment,
            reason="EXPIRY_SPOT_BOOK_INSTRUMENT_MISMATCH",
            spot_fee_btc=spot_fee_btc,
            spot_fee_equiv=spot_fee_equiv,
            spot_fee_quote=spot_fee_quote_charged,
            spot_net_quantity=spot_net_quantity,
            hedge_residual=hedge_residual,
            source_hashes=decision.source_hashes,
        )

    ordered, settlement_error = _settlement_validation(
        settlements,
        venue="deribit",
        instrument_id=decision.selected_instrument_id,
        entry_at=entry_filled_at,
        expiry_at=expiry_at,
    )
    if settlement_error is not None:
        return _incomplete_result(
            decision=decision,
            execution_label=execution_label,
            scenario=spot_fee_currency_scenario,
            exit_scenario=exit_scenario,
            capital=capital,
            margin_assessment=margin_assessment,
            reason=settlement_error,
            spot_fee_btc=spot_fee_btc,
            spot_fee_equiv=spot_fee_equiv,
            spot_fee_quote=spot_fee_quote_charged,
            spot_net_quantity=spot_net_quantity,
            hedge_residual=hedge_residual,
            source_hashes=_hashes(
                *decision.source_hashes, *(event.content_hash for event in ordered)
            ),
        )

    reference = future_entry_fill_vwap
    flows: list[SettlementCashFlow] = []
    future_pnl = ZERO
    for event in ordered:
        cash_pnl = future_entry_fill_quantity * (reference - event.official_index_price)
        flows.append(
            SettlementCashFlow(
                settlement_key=event.settlement_key,
                kind=event.kind,
                period_end=event.period_end,
                reference_before=reference,
                official_index_price=event.official_index_price,
                cash_pnl=cash_pnl,
                content_hash=event.content_hash,
            )
        )
        future_pnl += cash_pnl
        reference = event.official_index_price
    final_settlement = ordered[-1]

    if (
        final_settlement.received_at > final_settlement.period_end + MAX_EXIT_BOOK_DELAY
        or expiry_spot_book.market_timestamp < final_settlement.received_at
    ):
        return _incomplete_result(
            decision=decision,
            execution_label=execution_label,
            scenario=spot_fee_currency_scenario,
            exit_scenario=exit_scenario,
            capital=capital,
            margin_assessment=margin_assessment,
            reason="EXPIRY_SETTLEMENT_RECEIVED_TOO_LATE",
            flows=tuple(flows),
            future_pnl=future_pnl,
            spot_fee_btc=spot_fee_btc,
            spot_fee_equiv=spot_fee_equiv,
            spot_fee_quote=spot_fee_quote_charged,
            future_fee=future_entry_fill_quantity
            * future_entry_fill_vwap
            * fee_schedule.future_taker_rate,
            spot_net_quantity=spot_net_quantity,
            hedge_residual=hedge_residual,
            source_hashes=_hashes(
                *decision.source_hashes, *(event.content_hash for event in ordered)
            ),
        )

    exit_book_reasons = expiry_spot_book.quality_reasons(
        now=expiry_spot_book.received_timestamp,
        max_age_seconds=BOOK_FRESHNESS_SECONDS,
    )
    if (
        not expiry_spot_book.content_hash
        or exit_book_reasons
        or expiry_spot_book.received_timestamp > final_settlement.period_end + MAX_EXIT_BOOK_DELAY
        or not final_settlement.period_end
        <= expiry_spot_book.market_timestamp
        <= final_settlement.period_end + MAX_EXIT_BOOK_DELAY
    ):
        return _incomplete_result(
            decision=decision,
            execution_label=execution_label,
            scenario=spot_fee_currency_scenario,
            exit_scenario=exit_scenario,
            capital=capital,
            margin_assessment=margin_assessment,
            reason="EXPIRY_SPOT_EXIT_BOOK_INVALID_OR_LATE",
            flows=tuple(flows),
            future_pnl=future_pnl,
            spot_fee_btc=spot_fee_btc,
            spot_fee_equiv=spot_fee_equiv,
            spot_fee_quote=spot_fee_quote_charged,
            future_fee=future_entry_fill_quantity
            * future_entry_fill_vwap
            * fee_schedule.future_taker_rate,
            spot_net_quantity=spot_net_quantity,
            hedge_residual=hedge_residual,
            source_hashes=_hashes(
                *decision.source_hashes,
                *(event.content_hash for event in ordered),
                expiry_spot_book.content_hash,
            ),
        )
    if expiry_spot_book.bids and any(
        left.price < right.price
        for left, right in zip(expiry_spot_book.bids, expiry_spot_book.bids[1:], strict=False)
    ):
        return _incomplete_result(
            decision=decision,
            execution_label=execution_label,
            scenario=spot_fee_currency_scenario,
            exit_scenario=exit_scenario,
            capital=capital,
            margin_assessment=margin_assessment,
            reason="EXPIRY_SPOT_EXIT_BOOK_UNSORTED",
            flows=tuple(flows),
            future_pnl=future_pnl,
            spot_net_quantity=spot_net_quantity,
            hedge_residual=hedge_residual,
            source_hashes=decision.source_hashes,
        )

    spot_exit_rate = fee_schedule.spot_exit_taker_rate
    if exit_scenario == "BASE":
        target_sale_quantity = _floor_to_step(
            spot_net_quantity / (ONE + spot_exit_rate), spot_amount_step
        )
    else:
        target_sale_quantity = _floor_to_step(spot_net_quantity, spot_amount_step)
    spot_exit_trade_quantity, spot_exit_vwap = _walk_partial(
        expiry_spot_book.bids, target_sale_quantity
    )
    if spot_exit_trade_quantity <= ZERO or spot_exit_vwap is None:
        return _incomplete_result(
            decision=decision,
            execution_label=execution_label,
            scenario=spot_fee_currency_scenario,
            exit_scenario=exit_scenario,
            capital=capital,
            margin_assessment=margin_assessment,
            reason="EXPIRY_SPOT_EXIT_DEPTH_INSUFFICIENT",
            flows=tuple(flows),
            future_pnl=future_pnl,
            spot_net_quantity=spot_net_quantity,
            hedge_residual=hedge_residual,
            source_hashes=decision.source_hashes,
        )

    future_entry_fee = (
        future_entry_fill_quantity * future_entry_fill_vwap * fee_schedule.future_taker_rate
    )
    delivery_fee = (
        future_entry_fill_quantity
        * final_settlement.official_index_price
        * fee_schedule.delivery_rate
    )
    spot_exit_fee_btc = (
        spot_exit_trade_quantity * spot_exit_rate if exit_scenario == "BASE" else ZERO
    )
    spot_exit_fee_quote = (
        spot_exit_trade_quantity * spot_exit_vwap * spot_exit_rate
        if exit_scenario == "QUOTE"
        else ZERO
    )
    spot_exit_fee_equiv = (
        spot_exit_fee_btc * spot_exit_vwap if exit_scenario == "BASE" else spot_exit_fee_quote
    )
    spot_residual = max(ZERO, spot_net_quantity - spot_exit_trade_quantity - spot_exit_fee_btc)
    spot_pnl = (
        spot_exit_trade_quantity * spot_exit_vwap
        - (spot_exit_trade_quantity + spot_exit_fee_btc) * spot_entry_fill_vwap
        - spot_fee_equiv
        - spot_exit_fee_quote
    )
    future_pnl_after_fees = future_pnl - future_entry_fee - delivery_fee
    net_pnl = spot_pnl + future_pnl_after_fees
    terminal_basis_loss = (
        spot_net_quantity * final_settlement.official_index_price * terminal_basis_stress_rate
    )
    residual_state = (
        "COMPLETED"
        if abs(hedge_residual) <= quantity_tolerance and spot_residual == ZERO
        else "COMPLETED_UNBALANCED"
    )
    return DatedCarryReplayResult(
        status=residual_state,
        reason_code="OFFICIAL_EXPIRY_AND_OBSERVED_SPOT_LIQUIDATION_REPLAYED",
        reason_message=(
            "Résultat mécanique PAPER; l’hypothèse de frais spot d’entrée et de sortie est explicitée. "
            "Les frais BASE consomment du BTC, les frais QUOTE débitent les USDC. "
            "Tout résiduel reste visible; le stress terminal est distinct de la réserve d’admission."
        ),
        economic_status="INSUFFICIENT_EVIDENCE",
        execution_label=execution_label,
        instrument_id=decision.selected_instrument_id,
        quantity_future_btc=future_entry_fill_quantity,
        spot_gross_quantity_btc=spot_entry_fill_quantity,
        spot_net_quantity_btc=spot_net_quantity,
        hedge_residual_btc=hedge_residual,
        spot_fee_currency_scenario=spot_fee_currency_scenario,
        spot_exit_fee_currency_scenario=exit_scenario,
        spot_entry_fee_btc=spot_fee_btc,
        spot_entry_fee_quote_equivalent=spot_fee_equiv,
        spot_entry_fee_quote_charged=spot_fee_quote_charged,
        future_entry_fee_usdc=future_entry_fee,
        delivery_fee_usdc=delivery_fee,
        spot_exit_fee_usdc=spot_exit_fee_equiv,
        spot_exit_fee_btc=spot_exit_fee_btc,
        spot_exit_fee_quote_charged_usdc=spot_exit_fee_quote,
        spot_exit_trade_quantity_btc=spot_exit_trade_quantity,
        spot_residual_quantity_btc=spot_residual,
        settlement_flows=tuple(flows),
        future_settlement_pnl_usdc=future_pnl,
        spot_realized_pnl_usdc=spot_pnl,
        net_pnl_usdc=net_pnl,
        allocated_capital_usdc=capital,
        admission_reserve_usdc=capital * Decimal("0.02"),
        initial_margin_cash_usdc=margin_assessment.initial_margin_usdc,
        stress_cash_buffer_usdc=margin_assessment.stress_cash_buffer_usdc,
        margin_is_cost=False,
        terminal_basis_stress_rate=terminal_basis_stress_rate,
        terminal_basis_stress_loss_usdc=terminal_basis_loss,
        net_pnl_under_terminal_basis_stress_usdc=net_pnl - terminal_basis_loss,
        terminal_basis_stress_covered_by_reserve=False,
        terminal_spot_exit_price_usdc=spot_exit_vwap,
        source_hashes=_hashes(
            *decision.source_hashes,
            *(event.content_hash for event in ordered),
            expiry_spot_book.content_hash,
        ),
    )


def compare_spot_fee_currency_scenarios(
    **kwargs,
) -> tuple[
    tuple[FeeCurrencyScenario, FeeCurrencyScenario, DatedCarryReplayResult],
    ...,
]:
    """Replay identical fills across BASE/QUOTE entry and exit combinations."""

    return tuple(
        (
            entry_scenario,
            exit_scenario,
            replay_expiry_cycle(
                **kwargs,
                spot_fee_currency_scenario=entry_scenario,
                spot_exit_fee_currency_scenario=exit_scenario,
            ),
        )
        for entry_scenario in ("BASE", "QUOTE")
        for exit_scenario in ("BASE", "QUOTE")
    )


def protective_exit_required(
    *,
    available_derivative_cash: Decimal,
    quantity_btc: Decimal,
    mark_price: Decimal,
    last_settlement_reference_price: Decimal,
    margin_policy: MarginPolicy,
) -> bool:
    """Apply short unrealized losses, never gains, before the cash trigger."""

    available_derivative_cash = _finite_signed(
        available_derivative_cash, "available_derivative_cash"
    )
    last_settlement_reference_price = _positive(
        last_settlement_reference_price, "last_settlement_reference_price"
    )
    mark_price = _positive(mark_price, "mark_price")
    quantity_btc = _positive(quantity_btc, "quantity_btc")
    unrealized_short_loss = max(ZERO, quantity_btc * (mark_price - last_settlement_reference_price))
    cash_after_unrealized_loss = available_derivative_cash - unrealized_short_loss
    requirement = margin_policy.assess(quantity_btc, mark_price).total_stress_collateral_usdc
    return cash_after_unrealized_loss < requirement


def _positive_or_zero(value: Decimal, name: str) -> Decimal:
    if not value.is_finite() or value < ZERO:
        raise ValueError(f"{name} must be non-negative and finite")
    return value


def _finite_signed(value: Decimal, name: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def protective_observation_is_actionable(
    observation: ProtectiveObservation,
    *,
    quantity_btc: Decimal,
    margin_policy: MarginPolicy,
) -> bool:
    """Stale/mismatched books cannot authorize a protective close."""

    observed_at = _utc(observation.observed_at, "observed_at")
    future = observation.future_book
    spot = observation.spot_book
    if (
        not future.content_hash
        or not spot.content_hash
        or future.received_timestamp > observed_at
        or spot.received_timestamp > observed_at
        or future.instrument_state != "open"
        or spot.instrument_state != "open"
        or future.quality_reasons(now=observed_at, max_age_seconds=BOOK_FRESHNESS_SECONDS)
        or spot.quality_reasons(now=observed_at, max_age_seconds=BOOK_FRESHNESS_SECONDS)
        or abs(future.market_timestamp - spot.market_timestamp) > timedelta(seconds=5)
    ):
        return False
    if future.mark_price is None:
        return False
    return protective_exit_required(
        available_derivative_cash=observation.derivative_cash_available_usdc,
        quantity_btc=quantity_btc,
        mark_price=future.mark_price,
        last_settlement_reference_price=observation.last_settlement_reference_price,
        margin_policy=margin_policy,
    )


def replay_protective_close(
    *,
    observation: ProtectiveObservation,
    quantity_future_btc: Decimal,
    spot_net_quantity_btc: Decimal,
    spot_entry_gross_quantity_btc: Decimal,
    spot_entry_fill_vwap_usdc: Decimal,
    last_settlement_reference_price_usdc: Decimal,
    accumulated_future_settlement_pnl_usdc: Decimal,
    future_entry_fee_usdc: Decimal,
    spot_fee_currency_scenario: FeeCurrencyScenario,
    spot_exit_fee_currency_scenario: FeeCurrencyScenario,
    spot_amount_step: Decimal,
    fee_schedule: FeeSchedule,
    margin_policy: MarginPolicy,
) -> ProtectiveCloseResult:
    """Replay the frozen margin-only exit: future cover first, spot sale second.

    An unavailable/stale snapshot does not authorize automatic liquidation. A
    partial future cover leaves spot untouched; after a full cover, only the
    depth actually available on spot bids is sold and any residual is exposed.
    """

    observed_at = _utc(observation.observed_at, "observed_at")
    quantity_future_btc = _positive(quantity_future_btc, "quantity_future_btc")
    spot_net_quantity_btc = _positive_or_zero(spot_net_quantity_btc, "spot_net_quantity_btc")
    spot_entry_gross_quantity_btc = _positive(
        spot_entry_gross_quantity_btc, "spot_entry_gross_quantity_btc"
    )
    spot_entry_fill_vwap_usdc = _positive(spot_entry_fill_vwap_usdc, "spot_entry_fill_vwap_usdc")
    last_settlement_reference_price_usdc = _positive(
        last_settlement_reference_price_usdc, "last_settlement_reference_price_usdc"
    )
    available_cash = _finite_signed(
        observation.derivative_cash_available_usdc, "derivative_cash_available_usdc"
    )
    if spot_fee_currency_scenario not in ("BASE", "QUOTE"):
        raise ValueError("fee currency scenario must be BASE or QUOTE")
    if spot_exit_fee_currency_scenario not in ("BASE", "QUOTE"):
        raise ValueError("spot exit fee currency scenario must be BASE or QUOTE")
    spot_amount_step = _positive(spot_amount_step, "spot_amount_step")
    future = observation.future_book
    spot = observation.spot_book
    market_valid = (
        future.content_hash is not None
        and spot.content_hash is not None
        and future.received_timestamp <= observed_at
        and spot.received_timestamp <= observed_at
        and future.instrument_state == "open"
        and spot.instrument_state == "open"
        and not future.quality_reasons(now=observed_at, max_age_seconds=BOOK_FRESHNESS_SECONDS)
        and not spot.quality_reasons(now=observed_at, max_age_seconds=BOOK_FRESHNESS_SECONDS)
        and abs(future.market_timestamp - spot.market_timestamp) <= timedelta(seconds=5)
        and future.mark_price is not None
    )
    mark = future.mark_price or ZERO
    unrealized_loss = max(
        ZERO,
        quantity_future_btc * (mark - last_settlement_reference_price_usdc),
    )
    cash_after_loss = available_cash - unrealized_loss
    assessment = margin_policy.assess(quantity_future_btc, mark) if mark > ZERO else None
    required = assessment.total_stress_collateral_usdc if assessment is not None else ZERO
    hashes = _hashes(future.content_hash, spot.content_hash)
    if not market_valid:
        return ProtectiveCloseResult(
            status="PENDING_RISK_ACTION",
            reason_code="STALE_OR_UNVERIFIED_MARKET_DATA_NO_AUTO_LIQUIDATION",
            triggered=False,
            available_derivative_cash_usdc=available_cash,
            realized_short_loss_since_settlement_usdc=unrealized_loss,
            cash_after_unrealized_loss_usdc=cash_after_loss,
            required_stress_collateral_usdc=required,
            future_quantity_closed_btc=ZERO,
            future_quantity_remaining_btc=quantity_future_btc,
            future_cover_vwap_usdc=None,
            spot_quantity_sold_btc=ZERO,
            spot_quantity_remaining_btc=spot_net_quantity_btc,
            spot_sale_vwap_usdc=None,
            future_close_cash_pnl_usdc=ZERO,
            spot_realized_pnl_usdc=ZERO,
            fees_usdc=ZERO,
            net_realized_pnl_usdc=ZERO,
            source_hashes=hashes,
            spot_fee_currency_scenario=spot_fee_currency_scenario,
            spot_exit_fee_currency_scenario=spot_exit_fee_currency_scenario,
        )
    assert assessment is not None
    triggered = cash_after_loss < required
    if not triggered:
        return ProtectiveCloseResult(
            status="POSITION_REMAINS_OPEN",
            reason_code="PROTECTIVE_COLLATERAL_THRESHOLD_NOT_BREACHED",
            triggered=False,
            available_derivative_cash_usdc=available_cash,
            realized_short_loss_since_settlement_usdc=unrealized_loss,
            cash_after_unrealized_loss_usdc=cash_after_loss,
            required_stress_collateral_usdc=required,
            future_quantity_closed_btc=ZERO,
            future_quantity_remaining_btc=quantity_future_btc,
            future_cover_vwap_usdc=None,
            spot_quantity_sold_btc=ZERO,
            spot_quantity_remaining_btc=spot_net_quantity_btc,
            spot_sale_vwap_usdc=None,
            future_close_cash_pnl_usdc=ZERO,
            spot_realized_pnl_usdc=ZERO,
            fees_usdc=ZERO,
            net_realized_pnl_usdc=ZERO,
            source_hashes=hashes,
            spot_fee_currency_scenario=spot_fee_currency_scenario,
            spot_exit_fee_currency_scenario=spot_exit_fee_currency_scenario,
        )

    future_closed, cover_vwap = _walk_partial(future.asks, quantity_future_btc)
    if future_closed < quantity_future_btc:
        future_fee = future_closed * (cover_vwap or ZERO) * fee_schedule.future_taker_rate
        close_cash_pnl = future_closed * (
            last_settlement_reference_price_usdc - (cover_vwap or ZERO)
        )
        spot_entry_fee_equiv = (
            spot_entry_gross_quantity_btc * spot_entry_fill_vwap_usdc * fee_schedule.spot_taker_rate
        )
        net = (
            accumulated_future_settlement_pnl_usdc
            + close_cash_pnl
            - future_entry_fee_usdc
            - future_fee
            - spot_entry_fee_equiv
        )
        return ProtectiveCloseResult(
            status="UNBALANCED_PROTECTIVE_CLOSE",
            reason_code="FUTURE_COVER_DEPTH_INSUFFICIENT_SPOT_LEFT_OPEN",
            triggered=True,
            available_derivative_cash_usdc=available_cash,
            realized_short_loss_since_settlement_usdc=unrealized_loss,
            cash_after_unrealized_loss_usdc=cash_after_loss,
            required_stress_collateral_usdc=required,
            future_quantity_closed_btc=future_closed,
            future_quantity_remaining_btc=quantity_future_btc - future_closed,
            future_cover_vwap_usdc=cover_vwap,
            spot_quantity_sold_btc=ZERO,
            spot_quantity_remaining_btc=spot_net_quantity_btc,
            spot_sale_vwap_usdc=None,
            future_close_cash_pnl_usdc=close_cash_pnl,
            spot_realized_pnl_usdc=-spot_entry_fee_equiv,
            fees_usdc=future_entry_fee_usdc + future_fee + spot_entry_fee_equiv,
            net_realized_pnl_usdc=net,
            source_hashes=hashes,
            spot_fee_currency_scenario=spot_fee_currency_scenario,
            spot_exit_fee_currency_scenario=spot_exit_fee_currency_scenario,
            spot_entry_fee_quote_equivalent_usdc=spot_entry_fee_equiv,
        )

    spot_rate = fee_schedule.spot_exit_taker_rate
    if spot_exit_fee_currency_scenario == "BASE":
        target_spot_sale = _floor_to_step(
            spot_net_quantity_btc / (ONE + spot_rate), spot_amount_step
        )
    else:
        target_spot_sale = _floor_to_step(spot_net_quantity_btc, spot_amount_step)
    spot_sold, spot_vwap = _walk_partial(spot.bids, target_spot_sale)
    future_close_fee = quantity_future_btc * (cover_vwap or ZERO) * fee_schedule.future_taker_rate
    spot_exit_fee_quote_equiv = spot_sold * (spot_vwap or ZERO) * spot_rate
    spot_exit_fee_btc = spot_sold * spot_rate if spot_exit_fee_currency_scenario == "BASE" else ZERO
    spot_exit_fee_quote_charged = (
        spot_exit_fee_quote_equiv if spot_exit_fee_currency_scenario == "QUOTE" else ZERO
    )
    spot_entry_fee_equiv = (
        spot_entry_gross_quantity_btc * spot_entry_fill_vwap_usdc * fee_schedule.spot_taker_rate
    )
    spot_base_consumed = spot_sold + spot_exit_fee_btc
    spot_pnl = (
        spot_sold * (spot_vwap or ZERO)
        - spot_base_consumed * spot_entry_fill_vwap_usdc
        - spot_entry_fee_equiv
        - spot_exit_fee_quote_charged
    )
    close_cash_pnl = quantity_future_btc * (
        last_settlement_reference_price_usdc - (cover_vwap or ZERO)
    )
    net = (
        accumulated_future_settlement_pnl_usdc
        + close_cash_pnl
        + spot_pnl
        - future_entry_fee_usdc
        - future_close_fee
    )
    spot_remaining = max(ZERO, spot_net_quantity_btc - spot_base_consumed)
    status = "PROTECTIVE_EXIT_COMPLETE" if spot_remaining == ZERO else "UNBALANCED_PROTECTIVE_CLOSE"
    return ProtectiveCloseResult(
        status=status,
        reason_code=(
            "STRESS_COLLATERAL_BREACH_CLOSED_BOTH_LEGS"
            if status == "PROTECTIVE_EXIT_COMPLETE"
            else "SPOT_EXIT_DEPTH_INSUFFICIENT_AFTER_FUTURE_COVER"
        ),
        triggered=True,
        available_derivative_cash_usdc=available_cash,
        realized_short_loss_since_settlement_usdc=unrealized_loss,
        cash_after_unrealized_loss_usdc=cash_after_loss,
        required_stress_collateral_usdc=required,
        future_quantity_closed_btc=future_closed,
        future_quantity_remaining_btc=ZERO,
        future_cover_vwap_usdc=cover_vwap,
        spot_quantity_sold_btc=spot_sold,
        spot_quantity_remaining_btc=spot_remaining,
        spot_sale_vwap_usdc=spot_vwap,
        future_close_cash_pnl_usdc=close_cash_pnl,
        spot_realized_pnl_usdc=spot_pnl,
        fees_usdc=(
            future_entry_fee_usdc
            + future_close_fee
            + spot_entry_fee_equiv
            + spot_exit_fee_quote_equiv
        ),
        net_realized_pnl_usdc=net,
        source_hashes=hashes,
        spot_fee_currency_scenario=spot_fee_currency_scenario,
        spot_exit_fee_currency_scenario=spot_exit_fee_currency_scenario,
        spot_entry_fee_quote_equivalent_usdc=spot_entry_fee_equiv,
        spot_exit_fee_btc=spot_exit_fee_btc,
        spot_exit_fee_quote_charged_usdc=spot_exit_fee_quote_charged,
        spot_exit_fee_quote_equivalent_usdc=spot_exit_fee_quote_equiv,
    )


__all__ = [
    "DatedCarryReplayResult",
    "ProtectiveObservation",
    "ProtectiveCloseResult",
    "SettlementCashFlow",
    "compare_spot_fee_currency_scenarios",
    "protective_exit_required",
    "protective_observation_is_actionable",
    "replay_protective_close",
    "replay_expiry_cycle",
]
