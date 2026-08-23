"""Pure Hyperliquid portfolio-margin mechanics for Carry V2 research.

This module is deliberately research-only.  It has no broker, account, database,
or production-runner integration.  Account-specific balances and eligibility
remain explicit inputs; missing venue/account evidence fails closed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


UNKNOWN = "UNKNOWN"
NOT_AVAILABLE = "N/A"
PROVEN = "PROVEN"
CURRENT_BTC_LTV = 0.50
CURRENT_HYPE_LTV = 0.65
PM_LIQUIDATABLE_RATIO = 0.95
MIN_BORROW_OFFSET_USDC = 20.0


class PMInputError(ValueError):
    """Raised when a structural PM input is invalid."""


def _finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise PMInputError(f"{name} must be finite")
    return number


def _non_negative(value: float, name: str) -> float:
    number = _finite(value, name)
    if number < 0:
        raise PMInputError(f"{name} must be non-negative")
    return number


def _positive(value: float, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise PMInputError(f"{name} must be strictly positive")
    return number


def _ltv(value: float, name: str = "LTV") -> float:
    ratio = _finite(value, name)
    if not 0 < ratio <= 1:
        raise PMInputError(f"{name} must be in (0, 1]")
    return ratio


@dataclass(frozen=True)
class CollateralMapping:
    """Evidence result for the PM BTC category versus the current spot token."""

    pm_btc_collateral_hypercore_token: str
    pm_btc_collateral_spot_pair: str
    ubtc_at_142_mapping: str
    alternate_btc_token_found: bool
    reason: str


def resolve_ubtc_mapping(
    *,
    historical_ubtc_collateral: bool,
    current_pair: str,
    current_token: str,
    current_full_name: str,
    current_pair_is_canonical: bool,
    current_pm_asset: str,
    alternate_btc_token_found: bool,
) -> CollateralMapping:
    """Resolve UBTC mapping only from an explicit evidence chain.

    Names alone are insufficient.  The historical announcement must identify
    UBTC as collateral, current metadata must identify @142 as Unit Bitcoin,
    current PM evidence must identify BTC as eligible, and no replacement BTC
    spot token may be present.
    """

    fields = (
        current_pair,
        current_token,
        current_full_name,
        current_pm_asset,
    )
    if any(not field for field in fields):
        mapping = UNKNOWN
        reason = "required historical/current mapping evidence is missing"
    elif current_pm_asset != "BTC":
        mapping = "NOT_THE_SAME_ASSET"
        reason = "current PM collateral category is not BTC"
    elif current_pair != "@142" or current_token != "UBTC":
        mapping = "NOT_THE_SAME_ASSET"
        reason = "current @142 metadata does not identify UBTC"
    elif current_full_name != "Unit Bitcoin" or current_pair_is_canonical:
        mapping = UNKNOWN
        reason = "token identity or canonical status is inconsistent"
    elif not historical_ubtc_collateral or alternate_btc_token_found:
        mapping = UNKNOWN
        reason = "historical continuity or replacement-token exclusion is incomplete"
    else:
        mapping = PROVEN
        reason = (
            "official UBTC collateral announcement + current @142 UBTC metadata + "
            "current BTC PM eligibility + no alternate BTC spot token"
        )
    return CollateralMapping(
        pm_btc_collateral_hypercore_token="UBTC",
        pm_btc_collateral_spot_pair="@142",
        ubtc_at_142_mapping=mapping,
        alternate_btc_token_found=alternate_btc_token_found,
        reason=reason,
    )


def borrowable_amount(
    token_balance: float,
    borrow_oracle_price: float,
    ltv: float,
) -> float:
    """Apply the documented PM borrowing formula."""

    balance = _non_negative(token_balance, "token balance")
    price = _positive(borrow_oracle_price, "borrow oracle price")
    return balance * price * _ltv(ltv)


@dataclass(frozen=True)
class EntryCapacity:
    """Entry-time quote borrowing and balance-sheet identity.

    The perp initial margin is represented as a quote asset held as margin and
    an equal borrow liability when no free quote balance remains.  Perp
    notional is intentionally not an asset.
    """

    own_cash: float
    matched_notional: float
    collateral_ltv: float
    perp_leverage: float
    spot_financing_debt: float
    perp_initial_margin_requirement: float
    perp_margin_related_borrow: float
    total_quote_borrow: float
    ltv_borrow_capacity: float
    borrow_surplus_or_deficit: float
    spot_asset_value: float
    perp_margin_cash_asset: float
    gross_balance_sheet_assets: float
    gross_balance_sheet_liabilities: float
    net_account_equity: float
    identity_residual: float
    self_contained_capacity: bool


def entry_capacity(
    own_cash: float,
    matched_notional: float,
    collateral_ltv: float,
    perp_leverage: float,
    *,
    no_free_quote_balance: bool = True,
) -> EntryCapacity:
    """Calculate deterministic entry capacity under the documented PM rules."""

    cash = _positive(own_cash, "own cash")
    notional = _positive(matched_notional, "matched notional")
    ratio = _ltv(collateral_ltv, "collateral LTV")
    leverage = _positive(perp_leverage, "perp leverage")
    spot_debt = max(0.0, notional - cash)
    initial_margin = notional / leverage
    margin_borrow = initial_margin if no_free_quote_balance else 0.0
    total_borrow = spot_debt + margin_borrow
    capacity = borrowable_amount(notional, 1.0, ratio)
    surplus = capacity - total_borrow
    spot_asset = notional
    margin_asset = margin_borrow
    assets = spot_asset + margin_asset
    liabilities = total_borrow
    equity = assets - liabilities
    residual = equity - cash
    return EntryCapacity(
        own_cash=cash,
        matched_notional=notional,
        collateral_ltv=ratio,
        perp_leverage=leverage,
        spot_financing_debt=spot_debt,
        perp_initial_margin_requirement=initial_margin,
        perp_margin_related_borrow=margin_borrow,
        total_quote_borrow=total_borrow,
        ltv_borrow_capacity=capacity,
        borrow_surplus_or_deficit=surplus,
        spot_asset_value=spot_asset,
        perp_margin_cash_asset=margin_asset,
        gross_balance_sheet_assets=assets,
        gross_balance_sheet_liabilities=liabilities,
        net_account_equity=equity,
        identity_residual=residual,
        self_contained_capacity=surplus >= -1e-9,
    )


def recursive_spot_capacity(initial_cash: float, collateral_ltv: float) -> dict[str, float | str]:
    """Return the idealized recursive ceiling, explicitly non-qualified."""

    cash = _positive(initial_cash, "initial cash")
    ratio = _ltv(collateral_ltv, "collateral LTV")
    return {
        "initial_cash": cash,
        "ltv": ratio,
        "max_theoretical_spot_value": cash / (1.0 - ratio),
        "max_theoretical_borrow": cash * ratio / (1.0 - ratio),
        "formula_applicability": "NOT_PROVEN",
        "qualification": "DIAGNOSTIC_ONLY",
    }


def minimum_own_cash_for_spot(target_spot: float, collateral_ltv: float) -> float:
    """Pure final-state cash needed for spot financing, before perp margin."""

    spot = _positive(target_spot, "target spot")
    ratio = _ltv(collateral_ltv, "collateral LTV")
    return spot * (1.0 - ratio)


def maximum_matched_notional(
    own_cash: float,
    collateral_ltv: float,
    perp_leverage: float,
) -> float:
    """Derived self-contained ceiling when perp margin is also auto-borrowed."""

    cash = _positive(own_cash, "own cash")
    ratio = _ltv(collateral_ltv, "collateral LTV")
    leverage = _positive(perp_leverage, "perp leverage")
    denominator = 1.0 - ratio + 1.0 / leverage
    return cash / denominator


def liquidation_threshold(collateral_ltv: float) -> float:
    """Documented borrow-liquidation threshold as a collateral fraction."""

    return 0.5 + 0.5 * _ltv(collateral_ltv, "collateral LTV")


def collateral_liquidation_thresholds(
    collateral_value: float, collateral_ltv: float
) -> dict[str, float]:
    """Return the documented partial/full collateral liquidation thresholds."""

    value = _positive(collateral_value, "collateral value")
    ratio = _ltv(collateral_ltv, "collateral LTV")
    return {
        "partial": value * (ratio + (1.0 - ratio) * 0.5),
        "full": value * (ratio + (1.0 - ratio) * (2.0 / 3.0)),
    }


def btc_margin_tier(notional: float) -> dict[str, float | int]:
    """Return the documented mainnet BTC margin tier around the research size."""

    value = _positive(notional, "BTC perp notional")
    max_leverage = 40 if value <= 150_000_000.0 else 20
    maintenance_rate = 1.0 / (2.0 * max_leverage)
    return {
        "notional_upper_bound": 150_000_000.0 if max_leverage == 40 else math.inf,
        "max_leverage": max_leverage,
        "initial_margin_rate_at_max_leverage": 1.0 / max_leverage,
        "maintenance_margin_rate": maintenance_rate,
        "maintenance_deduction": 0.0,
        "maintenance_margin": value * maintenance_rate,
    }


def perp_initial_margin(notional: float, leverage: float) -> float:
    """Calculate the documented standard initial-margin expression."""

    value = _positive(notional, "perp notional")
    selected = _positive(leverage, "selected perp leverage")
    tier = btc_margin_tier(value)
    if selected > float(tier["max_leverage"]):
        raise PMInputError("selected leverage exceeds the documented BTC maximum")
    return value / selected


def portfolio_margin_ratio(
    *,
    portfolio_maintenance_requirement: float,
    portfolio_liquidation_value: float,
) -> dict[str, float | bool | str]:
    """Evaluate the documented PMR once all account inputs are available."""

    maintenance = _non_negative(
        portfolio_maintenance_requirement, "portfolio maintenance requirement"
    )
    liquidation_value = _positive(portfolio_liquidation_value, "portfolio liquidation value")
    ratio = maintenance / liquidation_value
    return {
        "portfolio_margin_ratio": ratio,
        "liquidatable": ratio > PM_LIQUIDATABLE_RATIO,
        "liquidation_threshold": PM_LIQUIDATABLE_RATIO,
    }


def portfolio_margin_components(
    *,
    min_borrow_offset: float,
    cross_maintenance_margin: float,
    borrowed_size_for_maintenance: float,
    borrow_oracle_price: float,
    portfolio_balance: float,
    borrow_cap: float,
    supply_cap: float,
    collateral_ltv: float,
) -> dict[str, float | bool | str]:
    """Apply the official PM maintenance/liquidation equations.

    The caller must provide account-level values in the units required by the
    venue formula.  This function does not invent an account balance or oracle.
    """

    offset = _non_negative(min_borrow_offset, "minimum borrow offset")
    cross = _non_negative(cross_maintenance_margin, "cross maintenance margin")
    borrowed = _non_negative(borrowed_size_for_maintenance, "borrowed size")
    oracle = _positive(borrow_oracle_price, "borrow oracle price")
    balance = _finite(portfolio_balance, "portfolio balance")
    cap = _non_negative(borrow_cap, "borrow cap")
    supply = _non_negative(supply_cap, "supply cap")
    threshold = liquidation_threshold(collateral_ltv)
    maintenance = offset + cross + borrowed * oracle
    liquidation_value = balance + min(cap, min(balance, supply) * oracle * threshold)
    result: dict[str, float | bool | str] = {
        "min_borrow_offset": offset,
        "cross_maintenance_margin": cross,
        "borrowed_size_for_maintenance": borrowed,
        "borrow_oracle_price": oracle,
        "liquidation_threshold": threshold,
        "portfolio_maintenance_requirement": maintenance,
        "portfolio_liquidation_value": liquidation_value,
    }
    if liquidation_value <= 0:
        result.update(
            {
                "portfolio_margin_ratio": NOT_AVAILABLE,
                "liquidatable": UNKNOWN,
                "status": "INVALID_LIQUIDATION_VALUE",
            }
        )
    else:
        result.update(
            portfolio_margin_ratio(
                portfolio_maintenance_requirement=maintenance,
                portfolio_liquidation_value=liquidation_value,
            )
        )
        result["status"] = "CALCULATED_FROM_EXPLICIT_INPUTS"
    return result


def matched_price_shock(
    *,
    entry_notional: float,
    price_change: float,
    borrow_principal: float,
    collateral_ltv: float,
) -> dict[str, float | str]:
    """Run a zero-basis matched-quantity price shock."""

    notional = _positive(entry_notional, "entry notional")
    shock = _finite(price_change, "price change")
    debt = _non_negative(borrow_principal, "borrow principal")
    ratio = _ltv(collateral_ltv, "collateral LTV")
    current_value = notional * (1.0 + shock)
    if current_value <= 0:
        raise PMInputError("price shock must leave a positive spot value")
    spot_pnl = notional * shock
    perp_pnl = -spot_pnl
    capacity = current_value * ratio
    return {
        "price_change": shock,
        "spot_value": current_value,
        "spot_pnl": spot_pnl,
        "perp_pnl": perp_pnl,
        "net_price_pnl": spot_pnl + perp_pnl,
        "ltv_borrow_capacity": capacity,
        "borrow_principal": debt,
        "borrow_surplus_or_deficit": capacity - debt,
        "portfolio_margin_ratio": NOT_AVAILABLE,
        "liquidatable": UNKNOWN,
        "pm_ratio_status": "ACCOUNT_BALANCE_ORACLE_INPUTS_UNKNOWN",
    }


def basis_shock(
    *,
    entry_notional: float,
    basis_change: float,
) -> dict[str, float]:
    """Stress only perp-vs-spot basis with the underlying level unchanged."""

    notional = _positive(entry_notional, "entry notional")
    basis = _finite(basis_change, "basis change")
    perp_pnl = -notional * basis
    return {
        "basis_change": basis,
        "spot_pnl": 0.0,
        "perp_pnl": perp_pnl,
        "net_price_pnl": perp_pnl,
    }


def asdict_finite(value: Any) -> Any:
    """Recursively convert dataclasses while rejecting non-finite numerics."""

    if hasattr(value, "__dataclass_fields__"):
        return {key: asdict_finite(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {key: asdict_finite(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [asdict_finite(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise PMInputError("research artifact cannot contain non-finite numbers")
    return value
