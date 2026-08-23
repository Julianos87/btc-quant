"""Pure structural financing diagnostics for Carry V2 research.

This module does not connect to a broker, account, database, or production
runner.  It describes balance-sheet scenarios and analytical funding
thresholds only.  Unknown venue/account mechanics remain explicitly unknown.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any
from collections.abc import Iterable

import numpy as np


SECONDS_PER_YEAR = 365.25 * 24.0 * 60.0 * 60.0
HOURS_PER_YEAR = 365.25 * 24.0
UNKNOWN = "UNKNOWN"
NOT_AVAILABLE = "N/A"


class FinancingInputError(ValueError):
    """A structural financing input is invalid."""


def _finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise FinancingInputError(f"{name} must be finite")
    return number


def _positive(value: float, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise FinancingInputError(f"{name} must be strictly positive")
    return number


def _non_negative(value: float, name: str) -> float:
    number = _finite(value, name)
    if number < 0:
        raise FinancingInputError(f"{name} must be non-negative")
    return number


@dataclass(frozen=True)
class FinancingPolicy:
    """Existing V1 cost assumptions used for diagnostics, not selection."""

    equity: float = 4000.0
    borrow_rate_ann: float = 0.10
    spot_fee_rate: float = 0.0005
    perp_fee_rate: float = 0.0005
    slippage_bps: float = 5.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "equity", _positive(self.equity, "equity"))
        object.__setattr__(
            self, "borrow_rate_ann", _non_negative(self.borrow_rate_ann, "borrow rate")
        )
        for name in ("spot_fee_rate", "perp_fee_rate", "slippage_bps"):
            object.__setattr__(self, name, _non_negative(getattr(self, name), name))


@dataclass(frozen=True)
class FinancingStructure:
    """A quantity-neutral spot-long/perp-short architecture."""

    name: str
    equity: float
    reference_price: float
    spot_notional: float
    borrow_principal: float
    borrow_rate_ann: float
    feasibility: str
    capital_mechanics: str
    documented_borrow_cap: float | None = None
    perp_collateral_requirement: str = UNKNOWN
    perp_initial_margin_required: float | str = UNKNOWN
    additional_collateral_required: float | str = UNKNOWN
    total_capital_required: float | str = UNKNOWN
    collateral_reuse_status: str = UNKNOWN
    capital_denominator_qualified: bool = False
    capital_denominator_reason: str = "Total capital requirement is not qualified"

    def __post_init__(self) -> None:
        if not self.name:
            raise FinancingInputError("structure name must be non-empty")
        object.__setattr__(self, "equity", _positive(self.equity, "equity"))
        object.__setattr__(
            self, "reference_price", _positive(self.reference_price, "reference price")
        )
        object.__setattr__(self, "spot_notional", _positive(self.spot_notional, "spot notional"))
        object.__setattr__(
            self,
            "borrow_principal",
            _non_negative(self.borrow_principal, "borrow principal"),
        )
        object.__setattr__(
            self, "borrow_rate_ann", _non_negative(self.borrow_rate_ann, "borrow rate")
        )
        if self.borrow_principal > self.spot_notional:
            raise FinancingInputError("borrow cannot exceed spot notional")
        if self.documented_borrow_cap is not None:
            object.__setattr__(
                self,
                "documented_borrow_cap",
                _non_negative(self.documented_borrow_cap, "documented borrow cap"),
            )
        if self.capital_denominator_qualified and self.total_capital_required in {
            UNKNOWN,
            NOT_AVAILABLE,
        }:
            raise FinancingInputError("qualified capital denominator requires known total capital")

    @property
    def allocated_equity(self) -> float:
        return self.equity

    @property
    def spot_cash_required(self) -> float:
        return self.spot_notional

    @property
    def spot_qty(self) -> float:
        return self.spot_notional / self.reference_price

    @property
    def perp_qty(self) -> float:
        return self.spot_qty

    @property
    def perp_notional(self) -> float:
        return self.perp_qty * self.reference_price

    @property
    def quantity_delta(self) -> float:
        return self.spot_qty - self.perp_qty

    @property
    def effective_leverage(self) -> float:
        return self.spot_notional / self.equity

    @property
    def gross_exposure(self) -> float:
        return self.spot_notional + self.perp_notional

    def recurring_break_even_at(self, borrow_rate_ann: float) -> float:
        rate = _non_negative(borrow_rate_ann, "borrow rate")
        return self.borrow_principal * rate / self.perp_notional

    @property
    def recurring_break_even(self) -> float:
        """Funding annualized rate needed to cover this structure borrow."""

        return self.recurring_break_even_at(self.borrow_rate_ann)

    def annual_borrow_cost(self, policy: FinancingPolicy | None = None) -> float:
        """Use the structure rate as the sole authority."""

        del policy
        return self.borrow_principal * self.borrow_rate_ann

    def round_trip_cost(self, policy: FinancingPolicy) -> float:
        per_leg_rate = policy.spot_fee_rate + policy.slippage_bps / 10_000.0
        per_perp_rate = policy.perp_fee_rate + policy.slippage_bps / 10_000.0
        return 2.0 * (self.spot_notional * per_leg_rate + self.perp_notional * per_perp_rate)

    def fully_loaded_break_even(
        self,
        policy: FinancingPolicy,
        holding_days: float,
        *,
        target_return_on_equity: float = 0.0,
        return_capital_base: float | None = None,
    ) -> float | str:
        days = _positive(holding_days, "holding days")
        target = _finite(target_return_on_equity, "target return")
        if return_capital_base is None:
            return NOT_AVAILABLE
        capital = _positive(return_capital_base, "return capital base")
        years = days / 365.25
        return self.recurring_break_even + (self.round_trip_cost(policy) + target * capital) / (
            self.perp_notional * years
        )

    def balance_sheet(self) -> dict[str, float | str]:
        """Known spot/borrow identity; derivative collateral stays explicit."""

        gross_assets = self.spot_notional
        gross_liabilities = self.borrow_principal
        net_equity = gross_assets - gross_liabilities
        return {
            "gross_assets": gross_assets,
            "gross_liabilities": gross_liabilities,
            "net_equity": net_equity,
            "spot_net_equity": net_equity,
            "identity_residual": net_equity - self.equity,
            "perp_collateral_requirement": self.perp_collateral_requirement,
            "perp_initial_margin_required": self.perp_initial_margin_required,
            "additional_collateral_required": self.additional_collateral_required,
            "total_capital_required": self.total_capital_required,
            "collateral_reuse_status": self.collateral_reuse_status,
            "capital_denominator_qualified": self.capital_denominator_qualified,
            "free_collateral": UNKNOWN,
            "collateral_reuse": self.collateral_reuse_status,
        }

    def as_dict(self, policy: FinancingPolicy) -> dict[str, Any]:
        result = asdict(self)
        qualified_base = (
            self.total_capital_required
            if self.capital_denominator_qualified
            and isinstance(self.total_capital_required, (int, float))
            else None
        )
        result.update(
            {
                "allocated_equity": self.allocated_equity,
                "spot_cash_required": self.spot_cash_required,
                "spot_qty": self.spot_qty,
                "perp_qty": self.perp_qty,
                "perp_notional": self.perp_notional,
                "quantity_delta": self.quantity_delta,
                "effective_leverage": self.effective_leverage,
                "gross_exposure": self.gross_exposure,
                "recurring_break_even": self.recurring_break_even,
                "annual_borrow_cost": self.annual_borrow_cost(),
                "round_trip_cost": self.round_trip_cost(policy),
                "balance_sheet": self.balance_sheet(),
                "fully_loaded_break_even": {
                    str(days): {
                        "target_0pct": self.fully_loaded_break_even(
                            policy, days, return_capital_base=qualified_base
                        ),
                        "target_3pct": self.fully_loaded_break_even(
                            policy,
                            days,
                            target_return_on_equity=0.03,
                            return_capital_base=qualified_base,
                        ),
                    }
                    for days in (30, 90, 180, 365)
                },
                "fully_loaded_break_even_status": (
                    "QUALIFIED"
                    if self.capital_denominator_qualified
                    else "N/A_CAPITAL_DENOMINATOR_UNKNOWN"
                ),
            }
        )
        if self.documented_borrow_cap is not None:
            result["documented_cap_exceeded"] = self.borrow_principal > self.documented_borrow_cap
            result["borrow_to_documented_cap_ratio"] = (
                self.borrow_principal / self.documented_borrow_cap
                if self.documented_borrow_cap > 0
                else NOT_AVAILABLE
            )
        return result


def structure_scenarios(
    policy: FinancingPolicy,
    *,
    reference_price: float,
    documented_borrow_cap: float = 1000.0,
) -> dict[str, FinancingStructure]:
    """Build A/B/C without selecting or optimizing any trading policy."""

    cap = _non_negative(documented_borrow_cap, "documented borrow cap")
    return {
        "A_CURRENT_V1_3X": FinancingStructure(
            name="A_CURRENT_V1_3X",
            equity=policy.equity,
            reference_price=reference_price,
            spot_notional=policy.equity * 3.0,
            borrow_principal=policy.equity * 2.0,
            borrow_rate_ann=policy.borrow_rate_ann,
            feasibility="NOT_EXECUTABLE_UNDER_DOCUMENTED_PRE_ALPHA_CAP",
            capital_mechanics="current V1 requires 8000 USDC financing versus the documented 1000 USDC cap; perp collateral reuse remains unproven",
            documented_borrow_cap=cap,
            capital_denominator_reason="Current 3x is over the documented pre-alpha cap and total venue capital is unqualified",
        ),
        "B_THEORETICAL_CASH_FUNDED_SPOT": FinancingStructure(
            name="B_THEORETICAL_CASH_FUNDED_SPOT",
            equity=policy.equity,
            reference_price=reference_price,
            spot_notional=policy.equity,
            borrow_principal=0.0,
            borrow_rate_ann=policy.borrow_rate_ann,
            feasibility="CONDITIONAL_NON_QUALIFIED",
            capital_mechanics="cash-funded spot leg is theoretical; perp initial margin, buffer, and collateral reuse are unknown",
            capital_denominator_reason="4000 allocated equity cannot also be assumed available for perp collateral",
        ),
        "C_DOCUMENTED_CAP_SCENARIO": FinancingStructure(
            name="C_DOCUMENTED_CAP_SCENARIO",
            equity=policy.equity,
            reference_price=reference_price,
            spot_notional=policy.equity + cap,
            borrow_principal=cap,
            borrow_rate_ann=policy.borrow_rate_ann,
            feasibility="STRUCTURAL_SCENARIO_ONLY",
            capital_mechanics="uses the documented cap as a diagnostic upper bound only; perp collateral and total capital remain unknown",
            documented_borrow_cap=cap,
            capital_denominator_reason="Borrow cap does not establish total capital or collateral reuse",
        ),
    }


def borrow_sensitivity(
    structure: FinancingStructure,
    policy: FinancingPolicy,
    rates: Iterable[float] = (0.05, 0.10, 0.15, 0.20),
) -> dict[str, Any]:
    """Return non-optimizing borrow diagnostics for financed structures."""

    if structure.borrow_principal == 0:
        return {"status": "N/A_NO_BORROW", "scenarios": []}
    scenarios = []
    for rate in rates:
        validated = _non_negative(rate, "sensitivity borrow rate")
        scenarios.append(
            {
                "borrow_rate_ann": validated,
                "recurring_break_even": structure.recurring_break_even_at(validated),
                "annual_borrow_cost": structure.borrow_principal * validated,
                "fully_loaded_break_even_target_0pct": structure.fully_loaded_break_even(
                    policy, 180, return_capital_base=None
                ),
                "capital_denominator_status": (
                    "QUALIFIED"
                    if structure.capital_denominator_qualified
                    else "N/A_CAPITAL_DENOMINATOR_UNKNOWN"
                ),
            }
        )
    return {"status": "FIXED_BORROW_SENSITIVITY", "scenarios": scenarios}


def _percentiles(values: Iterable[float]) -> dict[str, float]:
    array = np.asarray(tuple(float(value) for value in values), dtype=float)
    if array.size == 0 or not np.isfinite(array).all():
        raise FinancingInputError("funding observations must be finite and non-empty")
    return {
        "median": float(np.percentile(array, 50)),
        "p75": float(np.percentile(array, 75)),
        "p90": float(np.percentile(array, 90)),
        "p95": float(np.percentile(array, 95)),
        "p99": float(np.percentile(array, 99)),
        "max": float(np.max(array)),
    }


def funding_distribution(native_hourly_rates: Iterable[float]) -> dict[str, Any]:
    """Summarize native hourly rates as annualized diagnostics."""

    annualized = tuple(float(rate) * HOURS_PER_YEAR for rate in native_hourly_rates)
    result: dict[str, Any] = dict(_percentiles(annualized))
    result["count"] = len(annualized)
    result["annualization"] = "native hourly rate * 365.25 * 24"
    result["statistic_classification"] = "DESCRIPTIVE_HOURLY_FUNDING_STATISTIC"
    result["values_annualized"] = annualized
    return result


def funding_fraction_above(annualized_rates: Iterable[float], threshold: float) -> float:
    values = tuple(float(value) for value in annualized_rates)
    if not values:
        raise FinancingInputError("funding observations must be non-empty")
    _finite(threshold, "threshold")
    return float(sum(value > threshold for value in values) / len(values))


def entry_edge(
    structure: FinancingStructure,
    *,
    smooth_funding_ann: float,
    policy: FinancingPolicy,
    holding_days: float | None = None,
    return_capital_base: float | None = None,
) -> dict[str, Any]:
    """Calculate leg economics and optional denominator-qualified returns."""

    smooth = _finite(smooth_funding_ann, "smooth funding")
    annual_funding_income = structure.perp_notional * smooth
    annual_borrow_cost = structure.annual_borrow_cost()
    annual_recurring_net = annual_funding_income - annual_borrow_cost
    result: dict[str, Any] = {
        "smooth_funding_ann": smooth,
        "annual_funding_income": annual_funding_income,
        "annual_borrow_cost": annual_borrow_cost,
        "annual_recurring_net": annual_recurring_net,
        "round_trip_cost": structure.round_trip_cost(policy),
        "capital_denominator_status": (
            "QUALIFIED" if return_capital_base is not None else "N/A_UNKNOWN"
        ),
        "capital_denominator_reason": (
            "explicit qualified return capital base"
            if return_capital_base is not None
            else structure.capital_denominator_reason
        ),
        "funding_income_on_equity": NOT_AVAILABLE,
        "borrow_drag_on_equity": NOT_AVAILABLE,
        "recurring_net_on_equity": NOT_AVAILABLE,
        "cost_loaded_net_on_equity": NOT_AVAILABLE,
    }
    if return_capital_base is not None:
        capital = _positive(return_capital_base, "return capital base")
        funding_income_on_equity = annual_funding_income / capital
        borrow_drag_on_equity = annual_borrow_cost / capital
        recurring_net = funding_income_on_equity - borrow_drag_on_equity
        cost_loaded: float | None = None
        if holding_days is not None:
            years = _positive(holding_days, "holding days") / 365.25
            cost_loaded = recurring_net - structure.round_trip_cost(policy) / capital / years
        result.update(
            {
                "funding_income_on_equity": funding_income_on_equity,
                "borrow_drag_on_equity": borrow_drag_on_equity,
                "recurring_net_on_equity": recurring_net,
                "cost_loaded_net_on_equity": cost_loaded,
            }
        )
    return result


def balance_sheet_identity(
    *, gross_assets: float, gross_liabilities: float, equity: float, tolerance: float = 1e-9
) -> dict[str, float | bool]:
    """Validate assets minus liabilities equals equity."""

    assets = _finite(gross_assets, "gross assets")
    liabilities = _finite(gross_liabilities, "gross liabilities")
    net = _finite(equity, "equity")
    residual = assets - liabilities - net
    return {
        "gross_assets": assets,
        "gross_liabilities": liabilities,
        "equity": net,
        "residual": residual,
        "status": abs(residual) <= tolerance,
    }
