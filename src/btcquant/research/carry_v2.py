"""Pure two-leg Carry V2 accounting and research primitives.

This module is deliberately separate from :mod:`btcquant.execution.carry_runner`.
It is a research model, not a broker and not a production state adapter.  The
strict input contract is intentional: unavailable or malformed financial data
must fail closed instead of becoming a zero-valued component.

The model uses positive quantities for both legs.  The spot leg is long and
the perpetual leg is short, so their price PnLs are::

    spot_qty * (spot_current - spot_entry)
    perp_qty * (perp_entry - perp_current)

Basis is reported as a decomposition of this net price PnL.  It is never added
as a second independent PnL component.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

SECONDS_PER_YEAR = 365.25 * 24.0 * 60.0 * 60.0
DEFAULT_SYNC_TOLERANCE = pd.Timedelta("1min")


class CarryV2InputError(ValueError):
    """An economic input cannot satisfy the V2 contract."""


def _utc_timestamp(value: pd.Timestamp | str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise CarryV2InputError("timestamps must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise CarryV2InputError(f"{name} must be finite")
    return number


def _positive(value: float, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise CarryV2InputError(f"{name} must be strictly positive")
    return number


def _non_negative(value: float, name: str) -> float:
    number = _finite(value, name)
    if number < 0:
        raise CarryV2InputError(f"{name} must be non-negative")
    return number


@dataclass(frozen=True)
class PriceObservation:
    """One causal, provenance-carrying spot or perp price observation."""

    timestamp: pd.Timestamp | str
    venue: str
    symbol: str
    price_type: str
    source: str
    price: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc_timestamp(self.timestamp))
        if not self.venue or not self.symbol or not self.price_type or not self.source:
            raise CarryV2InputError("price provenance fields must be non-empty")
        object.__setattr__(self, "price", _positive(self.price, "price"))


@dataclass(frozen=True)
class FundingObservation:
    """One native funding event and its applicable perp reference price."""

    timestamp: pd.Timestamp | str
    native_rate: float
    reference_price: float
    venue: str = ""
    symbol: str = ""
    price_source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc_timestamp(self.timestamp))
        object.__setattr__(self, "native_rate", _finite(self.native_rate, "native_rate"))
        object.__setattr__(
            self,
            "reference_price",
            _positive(self.reference_price, "funding reference price"),
        )
        if not self.venue or not self.symbol or not self.price_source:
            raise CarryV2InputError("funding provenance fields must be non-empty")


@dataclass(frozen=True)
class BorrowObservation:
    """Borrow rate observed for the interval ending at ``timestamp``."""

    timestamp: pd.Timestamp | str
    annualized_rate: float
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc_timestamp(self.timestamp))
        object.__setattr__(
            self, "annualized_rate", _non_negative(self.annualized_rate, "borrow rate")
        )
        if not self.source:
            raise CarryV2InputError("borrow source must be non-empty")


def _ordered(observations: Sequence[Any], *, name: str) -> None:
    if not observations:
        return
    timestamps = [_utc_timestamp(item.timestamp) for item in observations]
    if len(set(timestamps)) != len(timestamps):
        raise CarryV2InputError(f"{name}: duplicate timestamps")
    if any(right <= left for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise CarryV2InputError(f"{name}: out-of-order timestamps")


def validate_synchronized_prices(
    spot: Sequence[PriceObservation],
    perp: Sequence[PriceObservation],
    *,
    tolerance: pd.Timedelta = DEFAULT_SYNC_TOLERANCE,
) -> tuple[tuple[PriceObservation, ...], tuple[PriceObservation, ...]]:
    """Validate paired spot/perp observations without sorting or forward-fill."""

    if tolerance <= pd.Timedelta(0):
        raise CarryV2InputError("synchronization tolerance must be positive")
    spot_values = tuple(spot)
    perp_values = tuple(perp)
    _ordered(spot_values, name="spot prices")
    _ordered(perp_values, name="perp prices")
    if not spot_values or len(spot_values) != len(perp_values):
        raise CarryV2InputError("spot/perp observations must have equal non-zero length")
    for spot_item, perp_item in zip(spot_values, perp_values, strict=True):
        if (
            abs(_utc_timestamp(spot_item.timestamp) - _utc_timestamp(perp_item.timestamp))
            > tolerance
        ):
            raise CarryV2InputError("spot/perp observations exceed synchronization tolerance")
    return spot_values, perp_values


@dataclass(frozen=True)
class ExecutionCostAssumptions:
    """Separate spot/perp fee and slippage assumptions for one round trip."""

    spot_entry_fee_rate: float
    perp_entry_fee_rate: float
    spot_exit_fee_rate: float
    perp_exit_fee_rate: float
    spot_entry_slippage_bps: float
    perp_entry_slippage_bps: float
    spot_exit_slippage_bps: float
    perp_exit_slippage_bps: float
    other_explicit_costs: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "spot_entry_fee_rate",
            "perp_entry_fee_rate",
            "spot_exit_fee_rate",
            "perp_exit_fee_rate",
            "spot_entry_slippage_bps",
            "perp_entry_slippage_bps",
            "spot_exit_slippage_bps",
            "perp_exit_slippage_bps",
            "other_explicit_costs",
        ):
            object.__setattr__(self, name, _non_negative(getattr(self, name), name))

    @classmethod
    def symmetric(
        cls,
        *,
        spot_fee_rate: float,
        perp_fee_rate: float,
        spot_slippage_bps: float,
        perp_slippage_bps: float,
        other_explicit_costs: float = 0.0,
    ) -> ExecutionCostAssumptions:
        return cls(
            spot_entry_fee_rate=spot_fee_rate,
            perp_entry_fee_rate=perp_fee_rate,
            spot_exit_fee_rate=spot_fee_rate,
            perp_exit_fee_rate=perp_fee_rate,
            spot_entry_slippage_bps=spot_slippage_bps,
            perp_entry_slippage_bps=perp_slippage_bps,
            spot_exit_slippage_bps=spot_slippage_bps,
            perp_exit_slippage_bps=perp_slippage_bps,
            other_explicit_costs=other_explicit_costs,
        )


@dataclass(frozen=True)
class CarryLeg:
    """A positive-magnitude leg; the perp leg is interpreted as short."""

    symbol: str
    qty: float
    entry_price: float
    current_price: float

    def __post_init__(self) -> None:
        if not self.symbol:
            raise CarryV2InputError("leg symbol must be non-empty")
        object.__setattr__(self, "qty", _positive(self.qty, f"{self.symbol} qty"))
        object.__setattr__(
            self, "entry_price", _positive(self.entry_price, f"{self.symbol} entry price")
        )
        object.__setattr__(
            self,
            "current_price",
            _positive(self.current_price, f"{self.symbol} current price"),
        )

    @property
    def entry_notional(self) -> float:
        return self.qty * self.entry_price

    @property
    def current_notional(self) -> float:
        return self.qty * self.current_price


@dataclass(frozen=True)
class CarryPosition:
    """Research-only two-leg position at one current mark timestamp."""

    entry_timestamp: pd.Timestamp | str
    mark_timestamp: pd.Timestamp | str
    capital_at_entry: float
    spot: CarryLeg
    perp: CarryLeg
    borrow_principal: float
    costs: ExecutionCostAssumptions

    def __post_init__(self) -> None:
        entry = _utc_timestamp(self.entry_timestamp)
        mark = _utc_timestamp(self.mark_timestamp)
        if mark < entry:
            raise CarryV2InputError("mark timestamp cannot precede entry")
        object.__setattr__(self, "entry_timestamp", entry)
        object.__setattr__(self, "mark_timestamp", mark)
        object.__setattr__(
            self, "capital_at_entry", _positive(self.capital_at_entry, "entry capital")
        )
        object.__setattr__(
            self, "borrow_principal", _non_negative(self.borrow_principal, "borrow principal")
        )

    @property
    def entry_basis(self) -> float:
        return self.perp.entry_price - self.spot.entry_price

    @property
    def current_basis(self) -> float:
        return self.perp.current_price - self.spot.current_price

    @property
    def basis_change(self) -> float:
        return self.current_basis - self.entry_basis

    @property
    def delta_qty(self) -> float:
        return self.spot.qty - self.perp.qty

    @property
    def delta_qty_pct(self) -> float:
        denominator = max(abs(self.spot.qty), abs(self.perp.qty))
        return self.delta_qty / denominator if denominator else 0.0


@dataclass(frozen=True)
class ExecutionCostBreakdown:
    spot_fees: float
    perp_fees: float
    spot_slippage: float
    perp_slippage: float
    other_explicit_costs: float

    @property
    def fees(self) -> float:
        return self.spot_fees + self.perp_fees

    @property
    def slippage(self) -> float:
        return self.spot_slippage + self.perp_slippage

    @property
    def total(self) -> float:
        return self.fees + self.slippage + self.other_explicit_costs


@dataclass(frozen=True)
class CarryAccountingResult:
    spot_price_pnl: float
    perp_price_pnl: float
    net_price_pnl: float
    funding_pnl: float
    borrow_cost: float
    spot_fees: float
    perp_fees: float
    spot_slippage: float
    perp_slippage: float
    other_explicit_costs: float
    total_pnl: float
    equity: float
    entry_basis: float
    current_basis: float
    basis_change: float
    spot_current_notional: float
    perp_current_notional: float
    delta_qty: float
    delta_qty_pct: float

    def as_dict(self) -> dict[str, float]:
        return {
            name: float(value)
            for name, value in self.__dict__.items()
            if isinstance(value, (int, float))
        }


def _validate_events(
    events: Sequence[Any],
    *,
    name: str,
    entry: pd.Timestamp,
    mark: pd.Timestamp,
) -> None:
    _ordered(events, name=name)
    for event in events:
        event_timestamp = _utc_timestamp(event.timestamp)
        if event_timestamp < entry or event_timestamp > mark:
            raise CarryV2InputError(f"{name}: event outside position interval")


def _funding_pnl(position: CarryPosition, events: Sequence[FundingObservation]) -> float:
    return sum(position.perp.qty * event.reference_price * event.native_rate for event in events)


def _borrow_cost(position: CarryPosition, events: Sequence[BorrowObservation]) -> float:
    if not events:
        if position.borrow_principal:
            raise CarryV2InputError(
                "BORROW_COVERAGE_INCOMPLETE: observations required through mark"
            )
        return 0.0
    previous = _utc_timestamp(position.entry_timestamp)
    total = 0.0
    for event in events:
        event_timestamp = _utc_timestamp(event.timestamp)
        elapsed = (event_timestamp - previous).total_seconds()
        if elapsed < 0:
            raise CarryV2InputError("borrow intervals must be non-negative")
        total += position.borrow_principal * event.annualized_rate * elapsed / SECONDS_PER_YEAR
        previous = event_timestamp
    if position.borrow_principal and previous != _utc_timestamp(position.mark_timestamp):
        raise CarryV2InputError(
            "BORROW_COVERAGE_INCOMPLETE: final observation must equal mark timestamp"
        )
    return total


def execution_cost_breakdown(
    position: CarryPosition,
    *,
    include_exit: bool = False,
) -> ExecutionCostBreakdown:
    """Return separate leg fee/slippage amounts, never a hidden residual."""

    costs = position.costs
    spot_fees = position.spot.entry_notional * costs.spot_entry_fee_rate
    perp_fees = position.perp.entry_notional * costs.perp_entry_fee_rate
    spot_slippage = position.spot.entry_notional * costs.spot_entry_slippage_bps / 10_000.0
    perp_slippage = position.perp.entry_notional * costs.perp_entry_slippage_bps / 10_000.0
    if include_exit:
        spot_fees += position.spot.current_notional * costs.spot_exit_fee_rate
        perp_fees += position.perp.current_notional * costs.perp_exit_fee_rate
        spot_slippage += position.spot.current_notional * costs.spot_exit_slippage_bps / 10_000.0
        perp_slippage += position.perp.current_notional * costs.perp_exit_slippage_bps / 10_000.0
    return ExecutionCostBreakdown(
        spot_fees=spot_fees,
        perp_fees=perp_fees,
        spot_slippage=spot_slippage,
        perp_slippage=perp_slippage,
        other_explicit_costs=costs.other_explicit_costs,
    )


def mark_to_market(
    position: CarryPosition,
    *,
    funding_events: Sequence[FundingObservation] | None,
    borrow_events: Sequence[BorrowObservation] | None,
    include_exit_costs: bool = False,
) -> CarryAccountingResult:
    """Compute a complete two-leg accounting identity at the mark timestamp.

    ``None`` means the source is unavailable and is rejected.  An empty tuple
    is an authoritative empty series and is valid when no amount is due.
    """

    if funding_events is None or borrow_events is None:
        raise CarryV2InputError("funding and borrow inputs are required")
    funding = tuple(funding_events)
    _validate_events(
        funding,
        name="funding events",
        entry=_utc_timestamp(position.entry_timestamp),
        mark=_utc_timestamp(position.mark_timestamp),
    )
    borrow = tuple(borrow_events)
    _validate_events(
        borrow,
        name="borrow events",
        entry=_utc_timestamp(position.entry_timestamp),
        mark=_utc_timestamp(position.mark_timestamp),
    )
    spot_price_pnl = position.spot.qty * (position.spot.current_price - position.spot.entry_price)
    perp_price_pnl = position.perp.qty * (position.perp.entry_price - position.perp.current_price)
    net_price_pnl = spot_price_pnl + perp_price_pnl
    funding_pnl = _funding_pnl(position, funding)
    borrow_cost = _borrow_cost(position, borrow)
    cost = execution_cost_breakdown(position, include_exit=include_exit_costs)
    total_pnl = spot_price_pnl + perp_price_pnl + funding_pnl - borrow_cost - cost.total
    return CarryAccountingResult(
        spot_price_pnl=spot_price_pnl,
        perp_price_pnl=perp_price_pnl,
        net_price_pnl=net_price_pnl,
        funding_pnl=funding_pnl,
        borrow_cost=borrow_cost,
        spot_fees=cost.spot_fees,
        perp_fees=cost.perp_fees,
        spot_slippage=cost.spot_slippage,
        perp_slippage=cost.perp_slippage,
        other_explicit_costs=cost.other_explicit_costs,
        total_pnl=total_pnl,
        equity=position.capital_at_entry + total_pnl,
        entry_basis=position.entry_basis,
        current_basis=position.current_basis,
        basis_change=position.basis_change,
        spot_current_notional=position.spot.current_notional,
        perp_current_notional=position.perp.current_notional,
        delta_qty=position.delta_qty,
        delta_qty_pct=position.delta_qty_pct,
    )


def recurring_funding_break_even(leverage: float, borrow_rate_ann: float) -> float:
    """Gross annualized funding required to cover recurring borrow drag."""

    leverage = _positive(leverage, "leverage")
    if leverage < 1.0:
        raise CarryV2InputError("leverage must be at least 1")
    borrow_rate_ann = _non_negative(borrow_rate_ann, "borrow rate")
    return (leverage - 1.0) * borrow_rate_ann / leverage


def round_trip_cost_fraction(
    *,
    leverage: float,
    spot_fee_rate: float,
    perp_fee_rate: float,
    spot_slippage_bps: float,
    perp_slippage_bps: float,
) -> float:
    """Round-trip execution cost as a fraction of entry equity."""

    leverage = _positive(leverage, "leverage")
    rates = (
        _non_negative(spot_fee_rate, "spot fee rate"),
        _non_negative(perp_fee_rate, "perp fee rate"),
        _non_negative(spot_slippage_bps, "spot slippage") / 10_000.0,
        _non_negative(perp_slippage_bps, "perp slippage") / 10_000.0,
    )
    return 2.0 * leverage * sum(rates)


def fully_loaded_funding_break_even(
    *,
    leverage: float,
    borrow_rate_ann: float,
    spot_fee_rate: float,
    perp_fee_rate: float,
    spot_slippage_bps: float,
    perp_slippage_bps: float,
    holding_days: float,
    target_net_return_ann: float = 0.0,
) -> float:
    """Gross funding threshold including annualized round-trip costs."""

    holding_days = _positive(holding_days, "holding days")
    target_net_return_ann = _finite(target_net_return_ann, "target return")
    leverage = _positive(leverage, "leverage")
    recurring_equity_drag = (leverage - 1.0) * _non_negative(borrow_rate_ann, "borrow rate")
    transaction_drag = (
        round_trip_cost_fraction(
            leverage=leverage,
            spot_fee_rate=spot_fee_rate,
            perp_fee_rate=perp_fee_rate,
            spot_slippage_bps=spot_slippage_bps,
            perp_slippage_bps=perp_slippage_bps,
        )
        * 365.0
        / holding_days
    )
    return (recurring_equity_drag + transaction_drag + target_net_return_ann) / leverage


def carry_edge_metrics(
    *,
    gross_funding_ann: float,
    leverage: float,
    borrow_rate_ann: float,
    holding_days: float | None = None,
    round_trip_cost_fraction_value: float = 0.0,
    basis_component_ann: float = 0.0,
) -> dict[str, float | None]:
    """Expose annualized diagnostics with distinct gross/net semantics."""

    gross = _finite(gross_funding_ann, "gross funding")
    leverage = _positive(leverage, "leverage")
    borrow = _non_negative(borrow_rate_ann, "borrow rate")
    basis = _finite(basis_component_ann, "basis component")
    transaction_drag: float | None = None
    if holding_days is not None:
        days = _positive(holding_days, "holding days")
        transaction_drag = (
            _non_negative(round_trip_cost_fraction_value, "round-trip cost fraction") * 365.0 / days
        )
    return {
        "gross_funding_ann": gross,
        "borrow_drag_ann": (leverage - 1.0) * borrow,
        "transaction_cost_drag_ann": transaction_drag,
        "basis_component_ann": basis,
        "expected_net_carry_ann": (
            leverage * gross - (leverage - 1.0) * borrow + basis - (transaction_drag or 0.0)
        ),
    }


def basis_diagnostics(
    spot: Sequence[PriceObservation],
    perp: Sequence[PriceObservation],
    *,
    qty: float = 1.0,
    tolerance: pd.Timedelta = DEFAULT_SYNC_TOLERANCE,
) -> dict[str, Any]:
    """Summarize observed basis and its equal-quantity net price PnL."""

    qty = _positive(qty, "quantity")
    spot_values, perp_values = validate_synchronized_prices(spot, perp, tolerance=tolerance)
    basis = np.asarray(
        [
            perp_item.price - spot_item.price
            for spot_item, perp_item in zip(spot_values, perp_values, strict=True)
        ],
        dtype=float,
    )
    basis_pct = np.asarray(
        [
            perp_item.price / spot_item.price - 1.0
            for spot_item, perp_item in zip(spot_values, perp_values, strict=True)
        ],
        dtype=float,
    )
    net_price_pnl = qty * (
        np.asarray([item.price for item in spot_values])
        - spot_values[0].price
        + perp_values[0].price
        - np.asarray([item.price for item in perp_values])
    )
    basis_change = basis - basis[0]

    def percentiles(values: np.ndarray) -> dict[str, float]:
        return {
            "median": float(np.median(values)),
            "p90": float(np.percentile(values, 90)),
            "p95": float(np.percentile(values, 95)),
            "p99": float(np.percentile(values, 99)),
            "worst": float(np.max(values)),
        }

    return {
        "count": len(basis),
        "start": _utc_timestamp(spot_values[0].timestamp).isoformat(),
        "end": _utc_timestamp(spot_values[-1].timestamp).isoformat(),
        "basis_abs": percentiles(np.abs(basis)),
        "basis_change_abs": percentiles(np.abs(basis_change)),
        "basis_pct": percentiles(np.abs(basis_pct)),
        "net_price_pnl": percentiles(net_price_pnl),
        "basis_pnl_identity_max_abs_error": float(
            np.max(np.abs(net_price_pnl + qty * basis_change))
        ),
    }


def execution_stress(
    *,
    desired_spot_qty: float,
    desired_perp_qty: float,
    spot_fill_ratio: float,
    perp_fill_ratio: float,
    spot_entry_price: float,
    perp_entry_price: float,
    spot_fill_price: float,
    perp_fill_price: float,
) -> dict[str, float | None]:
    """Research-only partial-fill/latency stress diagnostic.

    Margin impact is intentionally ``None``: a venue-specific margin contract
    is required before a number can be called a margin or liquidation result.
    """

    spot_qty = _positive(desired_spot_qty, "desired spot quantity")
    perp_qty = _positive(desired_perp_qty, "desired perp quantity")
    for name, ratio in (("spot fill ratio", spot_fill_ratio), ("perp fill ratio", perp_fill_ratio)):
        ratio = _finite(ratio, name)
        if not 0.0 <= ratio <= 1.0:
            raise CarryV2InputError(f"{name} must be between 0 and 1")
    spot_entry = _positive(spot_entry_price, "spot entry price")
    perp_entry = _positive(perp_entry_price, "perp entry price")
    spot_fill = _positive(spot_fill_price, "spot fill price")
    perp_fill = _positive(perp_fill_price, "perp fill price")
    filled_spot = spot_qty * float(spot_fill_ratio)
    filled_perp = perp_qty * float(perp_fill_ratio)
    return {
        "filled_spot_qty": filled_spot,
        "filled_perp_qty": filled_perp,
        "temporary_delta_qty": filled_spot - filled_perp,
        "spot_price_pnl": filled_spot * (spot_fill - spot_entry),
        "perp_price_pnl": filled_perp * (perp_entry - perp_fill),
        "margin_impact": None,
    }
