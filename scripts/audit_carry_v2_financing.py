"""Build the read-only Hyperliquid Carry V2 financing feasibility artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "audit" / "baselines" / "data" / "carry_v2"
DEFAULT_OUTPUT = ROOT / "audit" / "carry_v2_financing_feasibility.json"
RETRIEVED_AT = "2026-08-23"
DOC_ROOT = "https://hyperliquid.gitbook.io/hyperliquid-docs"

SPOT_FILE = DATA_ROOT / "hyperliquid_ubtc_usdc_spot_1h_20260114_20260810_v2.csv.gz"
PERP_FILE = DATA_ROOT / "hyperliquid_btc_perp_1h_20260114_20260810_v2.csv.gz"
FUNDING_FILE = DATA_ROOT / "hyperliquid_btc_funding_1h_20260114_20260810_v2.csv.gz"
METADATA_FILE = DATA_ROOT / "hyperliquid_carry_v2_20260114_20260810_v2.metadata.json"
PR32_ARTIFACT = ROOT / "audit" / "carry_v2_real_data_replay.json"

sys.path.insert(0, str(ROOT / "src"))

from btcquant.research.carry_v2_replay import (  # noqa: E402
    ReplayPolicy,
    load_candle_csv,
    load_funding_csv,
    prepare_replay_frame,
    replay_policy,
    synchronize_price_frames,
)
from btcquant.research.carry_v2_financing import (  # noqa: E402
    FinancingPolicy,
    borrow_sensitivity,
    entry_edge,
    funding_distribution,
    funding_fraction_above,
    structure_scenarios,
    recursive_financing_ceiling,
)


SOURCE_PATHS = (
    "src/btcquant/research/carry_v2_financing.py",
    "scripts/audit_carry_v2_financing.py",
    "tests/test_carry_v2_financing.py",
    "src/btcquant/research/carry_v2.py",
    "src/btcquant/research/carry_v2_replay.py",
    "scripts/replay_carry_v2.py",
    "tests/test_carry_v2_economics.py",
    "tests/test_carry_v2_replay.py",
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
        raise ValueError("financing audit must run at the exact source commit")
    return {
        "qualification_source_sha": source_sha,
        "qualification_source_tree": _git("rev-parse", f"{source_sha}^{{tree}}"),
        "qualification_source_files": {
            path: _git("rev-parse", f"{source_sha}:{path}") for path in SOURCE_PATHS
        },
    }


def _official_evidence() -> list[dict[str, Any]]:
    """Record fresh venue evidence without querying private account state."""

    return [
        {
            "topic": "spot_pair_and_token_metadata",
            "classification": "CURRENT_OFFICIAL_API",
            "source_url": "https://api.hyperliquid.xyz/info",
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_PUBLIC_API_SNAPSHOT",
            "request": {"type": "spotMeta"},
            "facts": {
                "pair": "@142",
                "tokens": [197, 0],
                "pair_is_canonical": False,
                "base_name": "UBTC",
                "base_full_name": "Unit Bitcoin",
                "base_is_canonical": False,
                "quote_name": "USDC",
                "quote_is_canonical": True,
                "pm_btc_collateral_mapping": "UNKNOWN",
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
                "account_value_upper_bound_usd": 25000000.0,
                "eligible_collateral": ["BTC", "HYPE"],
                "limits": "INCREASED_CURRENT_LIMITS",
                "spot_perp_unified": True,
                "carry_trade_described": True,
            },
        },
        {
            "topic": "portfolio_margin_current_caps",
            "classification": "CURRENT_OFFICIAL_DOC",
            "source_url": f"{DOC_ROOT}/trading/portfolio-margin",
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_BETA_DOC_SNAPSHOT_DYNAMIC_UTILIZATION",
            "facts": {
                "account_requirements": {
                    "weighted_volume_or_account_value": ">5M weighted volume OR >10k account value",
                    "account_value_upper_bound_usd": 25000000.0,
                },
                "BTC_LTV": 0.5,
                "HYPE_LTV": 0.5,
                "USDC": {
                    "global_supply_cap": 1000000000.0,
                    "global_borrow_cap": 500000000.0,
                    "user_supply_cap": 250000000.0,
                    "user_borrow_cap": 50000000.0,
                },
                "USDT": {
                    "global_supply_cap": 50000000.0,
                    "global_borrow_cap": 10000000.0,
                    "user_supply_cap": 5000000.0,
                    "user_borrow_cap": 1000000.0,
                },
                "additional_collateral_supply_caps": {
                    "BTC_global": 2000.0,
                    "BTC_user": 200.0,
                    "HYPE_global": 10000000.0,
                    "HYPE_user": 1000000.0,
                },
                "cap_behavior": "UTILIZATION_DEPENDENT_OR_FALLBACK_WHEN_HIT",
            },
        },
        {
            "topic": "portfolio_margin_historical_pre_alpha",
            "classification": "HISTORICAL_OFFICIAL_ANNOUNCEMENT",
            "source_url": "https://t.me/s/hyperliquid_announcements/504",
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "HISTORICAL_PRE_ALPHA_INITIAL_CAP",
            "facts": {
                "initial_usdc_user_borrow_cap": 1000.0,
                "collateral": "HYPE_ONLY",
                "status": "PRE_ALPHA",
            },
        },
        {
            "topic": "stablecoin_borrow_rate",
            "classification": "CURRENT_OFFICIAL_DOC",
            "source_url": f"{DOC_ROOT}/trading/portfolio-margin",
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_BETA_DOC_SNAPSHOT",
            "facts": {
                "formula": "0.05 + 4.75 * max(0, utilization - 0.8) APY",
                "utilization": "total_borrowed_value / total_supplied_value",
                "accrual": "continuous",
                "indexing": "hourly",
                "current_utilization": "NOT_QUERIED",
                "historical_series": "NO_PUBLIC_HISTORY",
            },
        },
        {
            "topic": "portfolio_margin_liquidation",
            "classification": "CURRENT_OFFICIAL_DOC",
            "source_url": f"{DOC_ROOT}/trading/portfolio-margin",
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_BETA_DOC_SNAPSHOT",
            "facts": {
                "portfolio_margin_ratio_liquidation_threshold": 0.95,
                "liquidation_threshold_formula": "0.5 + 0.5 * LTV",
                "partial_liquidation": "20 percent intervals",
                "full_liquidation": "full takeover below full threshold",
                "backstop": "direct backstop takeover with TWAP half-life 10 minutes",
                "account_specific_price": "NOT_CALCULATED",
            },
        },
        {
            "topic": "standard_margining",
            "classification": "CURRENT_OFFICIAL_DOC",
            "source_url": f"{DOC_ROOT}/trading/margining",
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_DOC_SNAPSHOT",
            "facts": {
                "initial_margin_formula": "position_size * mark_price / leverage",
                "portfolio_mode_not_replaced_by_standard_cross_formula": True,
            },
        },
        {
            "topic": "fees",
            "classification": "CURRENT_OFFICIAL_DOC",
            "source_url": f"{DOC_ROOT}/trading/fees",
            "retrieved_at": RETRIEVED_AT,
            "fact_version": "CURRENT_FEE_SCHEDULE_SNAPSHOT",
            "facts": {
                "perp_base_maker": 0.00015,
                "perp_base_taker": 0.00045,
                "spot_base_maker": 0.0004,
                "spot_base_taker": 0.0007,
                "fee_tier_basis": "rolling 14-day weighted volume",
                "account_tier_required_for_exact_fee": True,
                "spot_and_perp_schedules_separate": True,
            },
        },
    ]


def _structure_entries(
    structures: dict[str, Any],
    baseline: dict[str, Any],
    policy: FinancingPolicy,
) -> list[dict[str, Any]]:
    observations = baseline["entry_edge"]["observations"]
    results: list[dict[str, Any]] = []
    for cycle in baseline["cycles"]:
        timestamp = pd.Timestamp(cycle["entry_timestamp"])
        match = min(observations, key=lambda item: abs(item["timestamp"] - timestamp.timestamp()))
        holding_days = float(cycle["holding_seconds"]) / 86_400.0
        structure_entries: dict[str, Any] = {}
        for name, structure in structures.items():
            edge = entry_edge(
                structure,
                smooth_funding_ann=float(match["smooth_ann"]),
                policy=policy,
                holding_days=holding_days,
            )
            if name == "A_CURRENT_V1_3X":
                edge["allocated_equity_model_diagnostic"] = entry_edge(
                    structure,
                    smooth_funding_ann=float(match["smooth_ann"]),
                    policy=policy,
                    holding_days=holding_days,
                    return_capital_base=structure.allocated_equity,
                )
                edge["allocated_equity_model_diagnostic_status"] = (
                    "MODEL_DIAGNOSTIC_NOT_VENUE_QUALIFIED"
                )
            structure_entries[name] = edge
        results.append(
            {
                "entry_timestamp": timestamp.isoformat(),
                "smooth_funding_ann": float(match["smooth_ann"]),
                "completed": bool(cycle["completed"]),
                "holding_days_to_exit_or_final_mark": holding_days,
                "structures": structure_entries,
            }
        )
    return results


def build_artifact(source_sha: str) -> dict[str, Any]:
    provenance = _source_provenance(source_sha)
    metadata = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
    spot = load_candle_csv(SPOT_FILE, label="Hyperliquid UBTC/USDC spot")
    perp = load_candle_csv(PERP_FILE, label="Hyperliquid BTC perp")
    funding = load_funding_csv(FUNDING_FILE)
    prices, synchronization = synchronize_price_frames(spot, perp)
    replay_frame, replay_input = prepare_replay_frame(prices, funding)
    replay_config = ReplayPolicy()
    baseline = replay_policy(replay_frame, replay_config)
    first_cycle = baseline["cycles"][0]
    reference_price = (first_cycle["spot_entry_price"] + first_cycle["perp_entry_price"]) / 2.0
    financing_policy = FinancingPolicy(
        equity=replay_config.capital,
        borrow_rate_ann=replay_config.borrow_rate_ann,
        spot_fee_rate=replay_config.spot_fee_rate,
        perp_fee_rate=replay_config.perp_fee_rate,
        slippage_bps=replay_config.slippage_bps,
    )
    structures = structure_scenarios(
        financing_policy,
        reference_price=reference_price,
        documented_borrow_cap=50_000_000.0,
    )
    annualized_rates = [float(rate) * 365.25 * 24.0 for rate in funding["native_rate"]]
    distribution = funding_distribution(funding["native_rate"])
    distribution.pop("values_annualized", None)
    structure_data = {
        name: structure.as_dict(financing_policy) for name, structure in structures.items()
    }
    fractions = {
        name: funding_fraction_above(annualized_rates, structure.recurring_break_even)
        for name, structure in structures.items()
    }
    source_files = {
        "spot": {
            "path": str(SPOT_FILE.relative_to(ROOT)),
            "sha256": _sha256(SPOT_FILE),
            "rows": int(len(spot)),
            "coverage_start": spot["timestamp"].iloc[0].isoformat(),
            "coverage_end": spot["timestamp"].iloc[-1].isoformat(),
            "classification": "NATIVE_HYPERLIQUID_SPOT_MARKET_WRAPPED_BTC",
        },
        "perp": {
            "path": str(PERP_FILE.relative_to(ROOT)),
            "sha256": _sha256(PERP_FILE),
            "rows": int(len(perp)),
            "coverage_start": perp["timestamp"].iloc[0].isoformat(),
            "coverage_end": perp["timestamp"].iloc[-1].isoformat(),
            "classification": "NATIVE_HYPERLIQUID_PERP",
        },
        "funding": {
            "path": str(FUNDING_FILE.relative_to(ROOT)),
            "sha256": _sha256(FUNDING_FILE),
            "rows": int(len(funding)),
            "coverage_start": funding["timestamp"].iloc[0].isoformat(),
            "coverage_end": funding["timestamp"].iloc[-1].isoformat(),
            "classification": "OBSERVED_HISTORICAL_NATIVE",
            "cadence": "native hourly",
        },
        "metadata": {
            "path": str(METADATA_FILE.relative_to(ROOT)),
            "sha256": _sha256(METADATA_FILE),
        },
    }
    recursive_capacity = recursive_financing_ceiling(financing_policy.equity, 0.5)
    financing_evidence = {
        "mechanism": "PORTFOLIO_MARGIN_BETA",
        "spot_leverage_mechanism": "PORTFOLIO_MARGIN_FINANCING",
        "borrow_asset": "USDC",
        "venue_evidence_status": "CURRENT_BETA_WITH_DYNAMIC_CAPS",
        "collateral": {
            "eligible_assets": ["BTC", "HYPE"],
            "BTC_LTV": 0.5,
            "HYPE_LTV": 0.5,
            "UBTC_at_142_is_PM_BTC_collateral": "UNKNOWN",
        },
        "current_limits": {
            "USDC": {
                "global_supply_cap": 1_000_000_000.0,
                "global_borrow_cap": 500_000_000.0,
                "user_supply_cap": 250_000_000.0,
                "user_borrow_cap": 50_000_000.0,
            },
            "USDT": {
                "global_supply_cap": 50_000_000.0,
                "global_borrow_cap": 10_000_000.0,
                "user_supply_cap": 5_000_000.0,
                "user_borrow_cap": 1_000_000.0,
            },
            "behavior": "UTILIZATION_DEPENDENT_OR_FALLBACK_WHEN_HIT",
        },
        "historical_pre_alpha": {
            "initial_usdc_user_borrow_cap": 1000.0,
            "classification": "HISTORICAL_PRE_ALPHA_INITIAL_CAP",
            "feeds_current_decision": False,
        },
        "account_requirements": {
            "global_requirement": ">5M master weighted volume OR account value >10k; account value <25M",
            "account_specific_eligibility": "UNKNOWN",
            "private_query_performed": False,
        },
        "interest_mechanism": {
            "rate_type": "VARIABLE_UTILIZATION_BASED",
            "formula": "0.05 + 4.75 * max(0, utilization - 0.8) APY",
            "accrual": "continuous",
            "indexing": "hourly",
            "current_utilization": "NOT_QUERIED",
            "historical_series": "NO_PUBLIC_HISTORY",
        },
        "margin": {
            "portfolio_status": "BETA",
            "portfolio_ratio_liquidation_threshold": 0.95,
            "min_borrow_offset_usdc": 20.0,
            "borrow_oracle": "documented composite oracle; account calculation not run",
            "perp_margin_sharing": "DOCUMENTED_CONCEPTUALLY_NOT_ACCOUNT_QUALIFIED",
            "initial_margin": "UNKNOWN_IN_PORTFOLIO_MODE",
            "maintenance_margin": "UNKNOWN_IN_PORTFOLIO_MODE",
        },
        "liquidation": {
            "qualification": False,
            "partial_threshold": "collateral value * (LTV + (1-LTV)*1/2)",
            "full_threshold": "collateral value * (LTV + (1-LTV)*2/3)",
            "sequence": "not deterministic across oracle update order",
            "backstop": "20 percent intervals; TWAP half-life 10 minutes",
            "reason": "account collateral, balances, portfolio ratio, and oracle path not available",
        },
        "recursive_financing_diagnostic": {
            **recursive_capacity,
            "formula": "initial_cash / (1 - LTV)",
            "formula_applicability": "NOT_PROVEN",
            "qualification": "DIAGNOSTIC_ONLY",
            "target_3x_spot_supported": False,
        },
        "historical_borrow_reconstruction": {
            "classification": "NO_PUBLIC_HISTORY",
            "deterministic_reconstruction": False,
            "reason": "no validated public historical utilization/supply/borrow state series in this audit",
        },
        "three_x_feasibility": "CURRENT_3X_CAPITAL_MECHANICS_NOT_QUALIFIED",
        "account_specific_eligibility": "ACCOUNT_SPECIFIC_PM_ELIGIBILITY_UNKNOWN",
        "cap_classification_reason": "current beta caps exceed the 8000 USDC diagnostic requirement, but cap sufficiency does not prove LTV, collateral reuse, perp margin, or account eligibility",
    }
    return {
        "artifact_contract": "TWO_COMMIT_SOURCE_THEN_ARTIFACT",
        **provenance,
        "created_at": RETRIEVED_AT,
        "scope": {
            "production_integration": False,
            "account_queries": False,
            "parameter_search": False,
            "policy_mutation": False,
        },
        "official_evidence": _official_evidence(),
        "current_structure": {
            "equity": financing_policy.equity,
            "allocated_equity": financing_policy.equity,
            "spot_cash_required": 12000.0,
            "borrow_principal": 8000.0,
            "perp_initial_margin_required": "UNKNOWN",
            "additional_collateral_required": "UNKNOWN",
            "total_capital_required": "UNKNOWN",
            "collateral_reuse_status": "UNKNOWN",
            "capital_denominator_qualified": False,
            "feasibility": "CURRENT_3X_CAPITAL_MECHANICS_NOT_QUALIFIED",
            "spot_notional": 12000.0,
            "perp_notional": 12000.0,
            "required_borrow": 8000.0,
            "gross_two_leg_exposure": 24000.0,
            "balance_sheet_statement": "4000 own capital + 8000 borrowed USDC -> 12000 UBTC spot; equal-BTC BTC perp short",
        },
        "instrument": {
            "spot_pair": "@142",
            "pair": "UBTC/USDC",
            "asset_representation": "WRAPPED_TOKENIZED_BTC",
            "is_canonical": False,
            "token_index": 197,
            "token_id": "0x8f254b963e8468305d409b33aa137c67",
            "evm_contract": "0x9fdbda0a5e284c32744d2f17ee5c74b284993463",
            "full_name": "Unit Bitcoin",
            "quote": "USDC",
            "minimum_size": "UNKNOWN",
            "tick_size": "UNKNOWN",
        },
        "financing": financing_evidence,
        "account_specific": {
            "portfolio_margin_eligibility": "UNKNOWN",
            "borrow_allowance": "UNKNOWN",
            "fee_tier": "UNKNOWN",
            "collateral_balance": "NOT_QUERIED",
            "account_reason": "No private/authenticated account query was performed",
        },
        "fees": {
            "spot_base_maker": 0.0004,
            "spot_base_taker": 0.0007,
            "perp_base_maker": 0.00015,
            "perp_base_taker": 0.00045,
            "account_tier": "UNKNOWN",
            "replay_classification": "VENUE_BASE_SCHEDULE",
            "v1_diagnostic_assumption": {
                "spot": replay_config.spot_fee_rate,
                "perp": replay_config.perp_fee_rate,
                "classification": "ASSUMPTION",
            },
        },
        "data": {
            "source_files": source_files,
            "requested_window": metadata["window"],
            "spot_perp_synchronization": synchronization,
            "funding_distribution_annualized": distribution,
            "fraction_funding_above_recurring_break_even": fractions,
            "fraction_funding_above_recurring_break_even_semantics": "DESCRIPTIVE_HOURLY_FUNDING_STATISTIC",
        },
        "structures": structure_data,
        "borrow_sensitivity": {
            name: borrow_sensitivity(structure, financing_policy)
            for name, structure in structures.items()
        },
        "historical_entries": _structure_entries(structures, baseline, financing_policy),
        "replay_reference": {
            "source_artifact": "audit/carry_v2_real_data_replay.json",
            "source_artifact_sha256": _sha256(PR32_ARTIFACT),
            "causal_pairs": int(len(replay_frame[["spot_price", "perp_price"]])),
            "entries": baseline["entries"],
            "exits": baseline["exits"],
            "terminal_position_open": baseline["terminal_position_open"],
            "strategy_mtm_equity": baseline["terminal_valuation"]["strategy_mark_to_market_equity"],
            "total_pnl": baseline["pnl"]["total"],
            "identity_residual": baseline["identity_residual_max_abs"],
            "net_recurring_positive_entries": baseline["entry_edge"]["net_recurring_positive"],
            "net_recurring_non_positive_entries": baseline["entry_edge"][
                "net_recurring_non_positive"
            ],
            "replay_input": replay_input,
        },
        "data_completeness": {
            "native_spot": True,
            "native_perp": True,
            "native_funding": True,
            "historical_borrow": False,
            "account_fees": False,
            "margin_liquidation": False,
            "real_market_inputs_complete": False,
            "reason": "financing account eligibility, collateral, historical borrow, and liquidation evidence are incomplete",
        },
        "decision_tree": {
            "result": "CURRENT_3X_CAPITAL_MECHANICS_NOT_QUALIFIED",
            "structure_a": "CURRENT_3X_CAPITAL_MECHANICS_NOT_QUALIFIED",
            "structure_b": "UNLEVERED_OR_LOWER_FINANCING_CAPITAL_REQUIREMENT_NOT_YET_QUALIFIED",
            "historical_pre_alpha_scenario": "HISTORICAL_PRE_ALPHA_DIAGNOSTIC_ONLY",
            "reason": "current beta removes the historical 1000 USDC blocker; BTC/UBTC mapping, LTV capacity, collateral reuse, perp margin, and account eligibility remain unqualified",
        },
        "blockers": [
            {
                "severity": "MAJOR",
                "finding": "Current 3x is no longer blocked by the historical pre-alpha cap; current LTV, collateral reuse, perp margin, and account eligibility remain unqualified.",
            },
            {
                "severity": "MAJOR",
                "finding": "Account-specific portfolio-margin eligibility, collateral balance, borrow allowance, and UBTC collateral treatment are unknown.",
            },
            {
                "severity": "MAJOR",
                "finding": "No deterministic historical native borrow/utilization series was established.",
            },
            {
                "severity": "INFO",
                "finding": "UBTC is a non-canonical wrapped/tokenized BTC representation, not native BTC.",
            },
        ],
        "governance": {
            "candidate_search": {"performed": False},
            "threshold_search": {"performed": False},
            "policy_changed": False,
            "governed_selection_permitted": False,
            "adopted": False,
        },
    }


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
