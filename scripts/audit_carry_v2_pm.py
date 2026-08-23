"""Build the read-only Carry V2 portfolio-margin mechanics artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from btcquant.research.carry_v2_pm import (
    CURRENT_BTC_LTV,
    CURRENT_HYPE_LTV,
    MIN_BORROW_OFFSET_USDC,
    PM_LIQUIDATABLE_RATIO,
    asdict_finite,
    basis_shock,
    btc_margin_tier,
    entry_capacity,
    maximum_matched_notional,
    minimum_own_cash_for_spot,
    portfolio_margin_components,
    recursive_spot_capacity,
    resolve_ubtc_mapping,
    matched_price_shock,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "audit" / "carry_v2_pm_exact_mechanics.json"
RETRIEVED_AT = "2026-08-23"
DOC_ROOT = "https://hyperliquid.gitbook.io/hyperliquid-docs"
PM_DOC = f"{DOC_ROOT}/trading/portfolio-margin"
PM_FAQ = f"{DOC_ROOT}/support/faq/portfolio-margin"
SPOT_META = "https://api.hyperliquid.xyz/info"
PR33_ARTIFACT = ROOT / "audit" / "carry_v2_financing_feasibility.json"
REPLAY_ARTIFACT = ROOT / "audit" / "carry_v2_real_data_replay.json"

SOURCE_PATHS = (
    "src/btcquant/research/carry_v2_pm.py",
    "scripts/audit_carry_v2_pm.py",
    "tests/test_carry_v2_pm.py",
    "tests/test_carry_v2_pm_provenance.py",
    "src/btcquant/research/carry_v2_financing.py",
    "src/btcquant/research/carry_v2_replay.py",
)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_provenance(source_sha: str) -> dict[str, Any]:
    resolved = _git("rev-parse", f"{source_sha}^{{commit}}")
    if len(source_sha) != 40 or resolved != source_sha:
        raise ValueError("qualification source SHA must be a resolvable full commit")
    if _git("rev-parse", "HEAD") != source_sha:
        raise ValueError("PM audit must run at the exact source commit")
    return {
        "qualification_source_sha": source_sha,
        "qualification_source_tree": _git("rev-parse", f"{source_sha}^{{tree}}"),
        "qualification_source_files": {
            path: _git("rev-parse", f"{source_sha}:{path}") for path in SOURCE_PATHS
        },
    }


def _official_evidence() -> list[dict[str, Any]]:
    """Fresh public evidence, with historical facts kept separate."""

    return [
        {
            "topic": "spot_pair_and_token_metadata",
            "classification": "CURRENT_OFFICIAL_API",
            "source_url": SPOT_META,
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_PUBLIC_API_SNAPSHOT",
            "request": {"type": "spotMeta"},
            "facts": {
                "pair": "@142",
                "pair_tokens": [197, 0],
                "pair_is_canonical": False,
                "base_name": "UBTC",
                "base_full_name": "Unit Bitcoin",
                "base_token_index": 197,
                "base_token_id": "0x8f254b963e8468305d409b33aa137c67",
                "base_evm_contract": "0x9fdbda0a5e284c32744d2f17ee5c74b284993463",
                "base_is_canonical": False,
                "quote_name": "USDC",
                "alternate_btc_spot_token_found": False,
            },
        },
        {
            "topic": "ubtc_collateral_continuity",
            "classification": "HISTORICAL_OFFICIAL_ANNOUNCEMENT",
            "source_url": "https://t.me/s/hyperliquid_announcements?before=522",
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "HISTORICAL_UBTC_COLLATERAL_ENABLEMENT",
            "facts": {
                "UBTC_enabled_as_portfolio_margin_collateral": True,
                "historical_phase": "PRE_ALPHA",
            },
        },
        {
            "topic": "portfolio_margin_current_phase",
            "classification": "CURRENT_OFFICIAL_ANNOUNCEMENT",
            "source_url": "https://t.me/s/hyperliquid_announcements/575",
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_BETA_ANNOUNCEMENT",
            "facts": {
                "status": "BETA",
                "account_value_upper_bound_usd": 25_000_000.0,
                "eligible_collateral": ["BTC", "HYPE"],
                "spot_perp_unified": True,
            },
        },
        {
            "topic": "portfolio_margin_current_contract",
            "classification": "CURRENT_OFFICIAL_DOC",
            "source_url": PM_DOC,
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_BETA_DOC_SNAPSHOT",
            "facts": {
                "account_requirement": ">5M master weighted volume OR account value >10k",
                "account_value_upper_bound_usd": 25_000_000.0,
                "USDC_caps": {
                    "global_supply": 1_000_000_000.0,
                    "global_borrow": 500_000_000.0,
                    "user_supply": 250_000_000.0,
                    "user_borrow": 50_000_000.0,
                },
                "BTC_supply_caps": {"global": 2_000.0, "user": 200.0},
                "HYPE_supply_caps": {"global": 10_000_000.0, "user": 1_000_000.0},
                "BTC_LTV": CURRENT_BTC_LTV,
                "HYPE_LTV": CURRENT_HYPE_LTV,
                "borrow_formula": "token_balance * borrow_oracle_price * LTV",
                "auto_borrow": "insufficient spot/perp balance is borrowed against eligible collateral",
                "carry_example": {
                    "spot": "1 BTC",
                    "perp": "short 1 BTC-USDC perp",
                    "perp_leverage": 10,
                    "interest_basis": "1/10 initial margin",
                    "funding_basis": "full perp notional",
                },
                "borrowable_balance_use": "trading only; not withdrawable or transferable",
            },
        },
        {
            "topic": "portfolio_margin_current_faq",
            "classification": "CURRENT_OFFICIAL_FAQ",
            "source_url": PM_FAQ,
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_FAQ_SNAPSHOT",
            "facts": {
                "eligible_spot_assets": ["HYPE", "BTC"],
                "spot_assets_can_purchase_other_spot": True,
                "auto_borrow_for_insufficient_balance": True,
                "BTC_LTV": 0.50,
                "HYPE_LTV": 0.50,
                "hype_ltv_conflicts_with_current_pm_doc": True,
            },
        },
        {
            "topic": "portfolio_margin_interest_and_liquidation",
            "classification": "CURRENT_OFFICIAL_DOC",
            "source_url": PM_DOC,
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_BETA_DOC_SNAPSHOT",
            "facts": {
                "borrow_oracle": "median(HL spot USDC price, HL perp mark * USDT/USDC oracle, HL perp oracle * USDT/USDC oracle)",
                "portfolio_margin_ratio": "max(maintenance_requirement / liquidation_value)",
                "maintenance_requirement": "min_borrow_offset + sum(cross_maintenance_margin) + borrowed_size_for_maintenance * borrow_oracle_price",
                "liquidation_value": "portfolio_balance + min(borrow_cap, min(portfolio_balance, supply_cap) * borrow_oracle_price * liquidation_threshold)",
                "liquidation_threshold": "0.5 + 0.5 * LTV",
                "min_borrow_offset_usdc": MIN_BORROW_OFFSET_USDC,
                "account_liquidatable_when_pmr_greater_than": PM_LIQUIDATABLE_RATIO,
                "partial_liquidation": "20 percent intervals",
                "borrow_liquidation_backstop": "TWAP half-life 10 minutes",
                "sequence": "not deterministic across oracle update order",
            },
        },
        {
            "topic": "btc_margin_tiers",
            "classification": "CURRENT_OFFICIAL_DOC",
            "source_url": f"{DOC_ROOT}/trading/margin-tiers",
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_MAINNET_BTC_MARGIN_TIER_SNAPSHOT",
            "facts": {
                "BTC": {"notional_range": "0-150M USDC", "max_leverage": 40},
                "BTC_above_150M": {"max_leverage": 20},
                "maintenance_rate": "initial margin rate at max leverage / 2",
            },
        },
    ]


def _stress_tables(
    *,
    entry_notional: float,
    borrow_principal: float,
    ltv: float,
) -> dict[str, Any]:
    price_changes = (-0.50, -0.25, 0.0, 0.25, 0.50, 1.0)
    basis_changes = (-0.01, -0.005, 0.0, 0.005, 0.01)
    return {
        "price_shocks_basis_zero": [
            matched_price_shock(
                entry_notional=entry_notional,
                price_change=change,
                borrow_principal=borrow_principal,
                collateral_ltv=ltv,
            )
            for change in price_changes
        ],
        "basis_shocks_underlying_constant": [
            basis_shock(entry_notional=entry_notional, basis_change=change)
            for change in basis_changes
        ],
        "pm_ratio_status": "N/A_ACCOUNT_BALANCE_ORACLE_INPUTS_UNKNOWN",
    }


def build_artifact(source_sha: str) -> dict[str, Any]:
    provenance = _source_provenance(source_sha)
    mapping = resolve_ubtc_mapping(
        historical_ubtc_collateral=True,
        current_pair="@142",
        current_token="UBTC",
        current_full_name="Unit Bitcoin",
        current_pair_is_canonical=False,
        current_pm_asset="BTC",
        alternate_btc_token_found=False,
    )
    current_v1_10x = entry_capacity(4000.0, 12_000.0, CURRENT_BTC_LTV, 10.0)
    current_v1_40x = entry_capacity(4000.0, 12_000.0, CURRENT_BTC_LTV, 40.0)
    structure_b_10x = entry_capacity(4000.0, 4_000.0, CURRENT_BTC_LTV, 10.0)
    tier_4k = btc_margin_tier(4_000.0)
    tier_8k = btc_margin_tier(8_000.0)
    tier_12k = btc_margin_tier(12_000.0)
    pm_fixture = portfolio_margin_components(
        min_borrow_offset=MIN_BORROW_OFFSET_USDC,
        cross_maintenance_margin=float(tier_12k["maintenance_margin"]),
        borrowed_size_for_maintenance=current_v1_10x.total_quote_borrow,
        borrow_oracle_price=1.0,
        portfolio_balance=12_000.0,
        borrow_cap=50_000_000.0,
        supply_cap=12_000.0,
        collateral_ltv=CURRENT_BTC_LTV,
    )
    source_files = {
        "pr33_financing_artifact": {
            "path": str(PR33_ARTIFACT.relative_to(ROOT)),
            "sha256": _sha256(PR33_ARTIFACT),
        },
        "pr32_replay_artifact": {
            "path": str(REPLAY_ARTIFACT.relative_to(ROOT)),
            "sha256": _sha256(REPLAY_ARTIFACT),
        },
    }
    return asdict_finite(
        {
            "artifact_contract": "TWO_COMMIT_SOURCE_THEN_ARTIFACT",
            **provenance,
            "created_at": RETRIEVED_AT,
            "scope": {
                "production_integration": False,
                "account_queries": False,
                "account_mutation": False,
                "parameter_search": False,
                "policy_mutation": False,
                "portfolio_margin_enablement": False,
            },
            "official_evidence": _official_evidence(),
            "ubtc_mapping": {
                **mapping.__dict__,
                "evidence_chain": [
                    "historical official UBTC collateral enablement",
                    "current spotMeta @142 -> UBTC / Unit Bitcoin",
                    "current PM docs BTC eligible collateral",
                    "no alternate BTC-like spot token in current spotMeta",
                ],
            },
            "current_pm_contract": {
                "phase": "BETA",
                "btc_ltv": CURRENT_BTC_LTV,
                "hype_ltv_current_pm_doc": CURRENT_HYPE_LTV,
                "hype_ltv_faq": 0.50,
                "hype_ltv_status": "CONFLICTING_CURRENT_OFFICIAL_DOCS",
                "borrow_formula": "token_balance * borrow_oracle_price * LTV",
                "perp_margin_source": "AUTO_BORROW_WHEN_NO_SUFFICIENT_QUOTE_BALANCE",
                "carry_example": {
                    "matched_quantity": True,
                    "perp_leverage": 10,
                    "interest_on_initial_margin_fraction": 0.10,
                    "funding_on_full_perp_notional": True,
                    "spot_perp_pnl_offset": True,
                },
                "portfolio_margin_ratio_formula": "max(maintenance_requirement / liquidation_value)",
                "portfolio_maintenance_formula": "min_borrow_offset + sum(cross_maintenance_margin) + borrowed_size_for_maintenance * borrow_oracle_price",
                "portfolio_liquidation_value_formula": "portfolio_balance + min(borrow_cap, min(portfolio_balance, supply_cap) * borrow_oracle_price * liquidation_threshold)",
                "liquidation_threshold_formula": "0.5 + 0.5 * LTV",
                "liquidatable_when_pmr_greater_than": PM_LIQUIDATABLE_RATIO,
                "min_borrow_offset_usdc": MIN_BORROW_OFFSET_USDC,
                "borrow_oracle": "documented composite oracle",
            },
            "current_v1_self_contained": {
                "own_capital": 4000.0,
                "target_spot": 12000.0,
                "target_perp": 12000.0,
                "spot_financing_debt": current_v1_10x.spot_financing_debt,
                "perp_margin_related_borrow_10x": current_v1_10x.perp_margin_related_borrow,
                "total_quote_borrow_10x": current_v1_10x.total_quote_borrow,
                "ltv_borrow_capacity": current_v1_10x.ltv_borrow_capacity,
                "deficit_10x": current_v1_10x.borrow_surplus_or_deficit,
                "spot_only_deficit": 6000.0 - 8000.0,
                "perp_margin_related_borrow_40x": current_v1_40x.perp_margin_related_borrow,
                "total_quote_borrow_40x": current_v1_40x.total_quote_borrow,
                "deficit_40x": current_v1_40x.borrow_surplus_or_deficit,
                "classification": "CURRENT_V1_3X_SELF_CONTAINED_INFEASIBLE_LTV_AND_MARGIN",
                "reason": "8k spot debt already exceeds 6k BTC-LTV capacity; perp margin adds further quote borrowing",
            },
            "recursive_capacity": recursive_spot_capacity(4000.0, CURRENT_BTC_LTV),
            "minimum_own_cash": {
                "target_spot": 12000.0,
                "before_perp_margin": minimum_own_cash_for_spot(12000.0, CURRENT_BTC_LTV),
                "status": "DERIVED_SPOT_ONLY_STRUCTURAL_REQUIREMENT",
            },
            "perp_margin": {
                "cash_funded_spot_short_perp_borrow_zero": "CONDITIONAL",
                "condition": "no free quote balance, eligible collateral, PM eligibility, and borrow capacity",
                "structure_b_10x": asdict_finite(structure_b_10x),
                "structural_bounds": {
                    "10x": maximum_matched_notional(4000.0, CURRENT_BTC_LTV, 10.0),
                    "40x": maximum_matched_notional(4000.0, CURRENT_BTC_LTV, 40.0),
                    "classification": "STRUCTURAL_BOUND_ONLY",
                },
            },
            "capital": {
                "spot_financing_debt": current_v1_10x.spot_financing_debt,
                "perp_margin_cash_asset": current_v1_10x.perp_margin_cash_asset,
                "perp_notional_is_balance_sheet_asset": False,
                "entry_identity_10x": asdict_finite(current_v1_10x),
                "total_capital_required": "UNKNOWN_ACCOUNT_QUALIFICATION",
                "external_collateral": {
                    "BTC_LTV_0.50": {
                        "additional_weighted_collateral_10x": max(
                            0.0, -current_v1_10x.borrow_surplus_or_deficit
                        ),
                        "raw_collateral_value_10x": max(
                            0.0, -current_v1_10x.borrow_surplus_or_deficit
                        )
                        / CURRENT_BTC_LTV,
                        "classification": "STRUCTURAL_REQUIREMENT",
                    },
                    "HYPE": {
                        "value": "N/A",
                        "reason": "current PM doc and FAQ disagree on HYPE LTV",
                        "classification": "UNKNOWN",
                    },
                },
            },
            "btc_margin_tiers": {
                "4000": tier_4k,
                "8000": tier_8k,
                "12000": tier_12k,
            },
            "pm_ratio_formula_fixture": {
                "inputs_are_account_independent_hand_fixture": True,
                **pm_fixture,
            },
            "price_shocks": _stress_tables(
                entry_notional=12000.0,
                borrow_principal=current_v1_10x.total_quote_borrow,
                ltv=CURRENT_BTC_LTV,
            ),
            "historical_data": {
                "historical_borrow": "INCOMPLETE",
                "real_market_inputs_complete": False,
                "account_specific_pm_eligibility": "UNKNOWN",
                "account_specific_liquidation": "NOT_QUALIFIED",
            },
            "source_artifacts": source_files,
            "governance": {
                "candidate_search": {"performed": False},
                "threshold_search": {"performed": False},
                "strategy_leverage_search": {"performed": False},
                "policy_changed": False,
                "governed_selection_permitted": False,
                "adopted": False,
            },
            "decision": {
                "self_contained_current_v1_3x": "CURRENT_V1_3X_SELF_CONTAINED_INFEASIBLE_LTV_AND_MARGIN",
                "account_supported_v1_3x": "UNKNOWN",
                "general_carry_architecture": "SUPPORTED_CONCEPTUALLY",
                "sizing_qualification": "NOT_QUALIFIED",
            },
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    artifact = build_artifact(args.source_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
