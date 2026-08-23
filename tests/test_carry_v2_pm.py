from __future__ import annotations

import pytest

from btcquant.research.carry_v2_pm import (
    CURRENT_BTC_LTV,
    CURRENT_HYPE_LTV,
    PMInputError,
    basis_shock,
    borrowable_amount,
    btc_margin_tier,
    collateral_liquidation_thresholds,
    entry_capacity,
    maximum_matched_notional,
    minimum_own_cash_for_spot,
    perp_initial_margin,
    portfolio_margin_components,
    recursive_spot_capacity,
    resolve_ubtc_mapping,
)


def test_ubtc_mapping_requires_the_full_evidence_chain() -> None:
    mapping = resolve_ubtc_mapping(
        historical_ubtc_collateral=True,
        current_pair="@142",
        current_token="UBTC",
        current_full_name="Unit Bitcoin",
        current_pair_is_canonical=False,
        current_pm_asset="BTC",
        alternate_btc_token_found=False,
    )
    assert mapping.ubtc_at_142_mapping == "PROVEN"
    assert mapping.pm_btc_collateral_hypercore_token == "UBTC"


def test_ubtc_mapping_fails_closed_for_replacement_or_missing_history() -> None:
    replacement = resolve_ubtc_mapping(
        historical_ubtc_collateral=True,
        current_pair="@142",
        current_token="UBTC",
        current_full_name="Unit Bitcoin",
        current_pair_is_canonical=False,
        current_pm_asset="BTC",
        alternate_btc_token_found=True,
    )
    missing_history = resolve_ubtc_mapping(
        historical_ubtc_collateral=False,
        current_pair="@142",
        current_token="UBTC",
        current_full_name="Unit Bitcoin",
        current_pair_is_canonical=False,
        current_pm_asset="BTC",
        alternate_btc_token_found=False,
    )
    assert replacement.ubtc_at_142_mapping == "UNKNOWN"
    assert missing_history.ubtc_at_142_mapping == "UNKNOWN"


def test_borrowable_amount_uses_documented_ltv_formula() -> None:
    assert borrowable_amount(12_000.0, 1.0, CURRENT_BTC_LTV) == pytest.approx(6_000.0)


def test_current_v1_target_fails_ltv_and_perp_margin_at_10x() -> None:
    capacity = entry_capacity(4_000.0, 12_000.0, CURRENT_BTC_LTV, 10.0)
    assert capacity.spot_financing_debt == pytest.approx(8_000.0)
    assert capacity.perp_initial_margin_requirement == pytest.approx(1_200.0)
    assert capacity.perp_margin_related_borrow == pytest.approx(1_200.0)
    assert capacity.total_quote_borrow == pytest.approx(9_200.0)
    assert capacity.ltv_borrow_capacity == pytest.approx(6_000.0)
    assert capacity.borrow_surplus_or_deficit == pytest.approx(-3_200.0)
    assert capacity.self_contained_capacity is False
    assert capacity.identity_residual == pytest.approx(0.0)


def test_current_v1_target_fails_ltv_even_at_documented_max_btc_leverage() -> None:
    capacity = entry_capacity(4_000.0, 12_000.0, CURRENT_BTC_LTV, 40.0)
    assert capacity.perp_margin_related_borrow == pytest.approx(300.0)
    assert capacity.total_quote_borrow == pytest.approx(8_300.0)
    assert capacity.borrow_surplus_or_deficit == pytest.approx(-2_300.0)
    assert capacity.self_contained_capacity is False


def test_cash_funded_spot_does_not_mean_zero_perp_margin_borrow() -> None:
    capacity = entry_capacity(4_000.0, 4_000.0, CURRENT_BTC_LTV, 10.0)
    assert capacity.spot_financing_debt == pytest.approx(0.0)
    assert capacity.perp_margin_related_borrow == pytest.approx(400.0)
    assert capacity.total_quote_borrow == pytest.approx(400.0)
    assert capacity.self_contained_capacity is True
    assert capacity.identity_residual == pytest.approx(0.0)


def test_perp_margin_borrow_can_be_disabled_only_when_free_quote_exists() -> None:
    capacity = entry_capacity(4_000.0, 4_000.0, CURRENT_BTC_LTV, 10.0, no_free_quote_balance=False)
    assert capacity.perp_margin_related_borrow == pytest.approx(0.0)
    assert capacity.total_quote_borrow == pytest.approx(0.0)


def test_recursive_capacity_and_minimum_spot_cash_are_structural_only() -> None:
    recursive = recursive_spot_capacity(4_000.0, CURRENT_BTC_LTV)
    assert recursive["max_theoretical_spot_value"] == pytest.approx(8_000.0)
    assert recursive["max_theoretical_borrow"] == pytest.approx(4_000.0)
    assert recursive["formula_applicability"] == "NOT_PROVEN"
    assert minimum_own_cash_for_spot(12_000.0, CURRENT_BTC_LTV) == pytest.approx(6_000.0)
    assert maximum_matched_notional(4_000.0, CURRENT_BTC_LTV, 10.0) == pytest.approx(4000.0 / 0.6)
    assert maximum_matched_notional(4_000.0, CURRENT_BTC_LTV, 40.0) == pytest.approx(4000.0 / 0.525)


def test_hype_ltv_is_kept_separate_from_btc_ltv() -> None:
    assert CURRENT_BTC_LTV == pytest.approx(0.50)
    assert CURRENT_HYPE_LTV == pytest.approx(0.65)


def test_btc_margin_tiers_and_initial_margin_are_deterministic() -> None:
    tier = btc_margin_tier(12_000.0)
    assert tier["max_leverage"] == 40
    assert tier["maintenance_margin_rate"] == pytest.approx(0.0125)
    assert tier["maintenance_margin"] == pytest.approx(150.0)
    assert perp_initial_margin(12_000.0, 10.0) == pytest.approx(1_200.0)
    assert perp_initial_margin(12_000.0, 40.0) == pytest.approx(300.0)
    with pytest.raises(PMInputError):
        perp_initial_margin(12_000.0, 41.0)


def test_portfolio_margin_formula_fixture_is_explicit() -> None:
    result = portfolio_margin_components(
        min_borrow_offset=20.0,
        cross_maintenance_margin=150.0,
        borrowed_size_for_maintenance=9_200.0,
        borrow_oracle_price=1.0,
        portfolio_balance=12_000.0,
        borrow_cap=50_000_000.0,
        supply_cap=12_000.0,
        collateral_ltv=0.50,
    )
    assert result["portfolio_maintenance_requirement"] == pytest.approx(9_370.0)
    assert result["portfolio_liquidation_value"] == pytest.approx(21_000.0)
    assert result["portfolio_margin_ratio"] == pytest.approx(9_370.0 / 21_000.0)
    assert result["liquidatable"] is False


def test_portfolio_margin_formula_fails_closed_for_invalid_liquidation_value() -> None:
    result = portfolio_margin_components(
        min_borrow_offset=20.0,
        cross_maintenance_margin=0.0,
        borrowed_size_for_maintenance=0.0,
        borrow_oracle_price=1.0,
        portfolio_balance=-100.0,
        borrow_cap=50_000_000.0,
        supply_cap=12_000.0,
        collateral_ltv=0.50,
    )
    assert result["portfolio_margin_ratio"] == "N/A"
    assert result["liquidatable"] == "UNKNOWN"
    assert result["status"] == "INVALID_LIQUIDATION_VALUE"


def test_liquidation_thresholds_are_not_pm_ratio() -> None:
    thresholds = collateral_liquidation_thresholds(12_000.0, CURRENT_BTC_LTV)
    assert thresholds["partial"] == pytest.approx(9_000.0)
    assert thresholds["full"] == pytest.approx(10_000.0)


def test_price_and_basis_shocks_keep_leg_pnl_separate() -> None:
    price = __import__(
        "btcquant.research.carry_v2_pm", fromlist=["matched_price_shock"]
    ).matched_price_shock(
        entry_notional=12_000.0,
        price_change=-0.50,
        borrow_principal=9_200.0,
        collateral_ltv=0.50,
    )
    basis = basis_shock(entry_notional=12_000.0, basis_change=0.01)
    assert price["spot_pnl"] == pytest.approx(-6_000.0)
    assert price["perp_pnl"] == pytest.approx(6_000.0)
    assert price["net_price_pnl"] == pytest.approx(0.0)
    assert price["portfolio_margin_ratio"] == "N/A"
    assert basis["spot_pnl"] == pytest.approx(0.0)
    assert basis["perp_pnl"] == pytest.approx(-120.0)
    assert basis["net_price_pnl"] == pytest.approx(-120.0)
