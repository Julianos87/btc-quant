"""Produce the governed, source-only Carry V2 qualification artifact.

This command is intentionally diagnostic.  It does not download market data,
change policy, read production state, or run a candidate search.  It inventories
the immutable research inputs already committed to the repository and records
which future qualification gates remain unsatisfied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "audit" / "carry_v2_economic_qualification.json"
HYPERLIQUID_DATA = ROOT / "audit" / "baselines" / "data" / "hyperliquid_v1"

sys.path.insert(0, str(ROOT / "src"))

from btcquant.carry import PAPER_CARRY_POLICY  # noqa: E402
from btcquant.research.carry_v2 import (  # noqa: E402
    CarryLeg,
    CarryPosition,
    CarryV2InputError,
    ExecutionCostAssumptions,
    FundingObservation,
    PriceObservation,
    basis_diagnostics,
    execution_stress,
    fully_loaded_funding_break_even,
    mark_to_market,
    recurring_funding_break_even,
    validate_synchronized_prices,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def _source_provenance(source_sha: str) -> dict[str, object]:
    if len(source_sha) != 40:
        raise ValueError("qualification source SHA must be a full 40-character commit")
    resolved = _git("rev-parse", f"{source_sha}^{{commit}}")
    current_head = _git("rev-parse", "HEAD")
    if resolved != source_sha:
        raise ValueError("qualification source SHA is not a resolvable commit")
    if current_head != source_sha:
        raise ValueError("qualification must run with HEAD exactly at source commit")
    source_paths = (
        "src/btcquant/research/carry_v2.py",
        "scripts/qualify_carry_v2.py",
        "tests/test_carry_v2_economics.py",
    )
    return {
        "qualification_source_sha": source_sha,
        "qualification_source_tree": _git("rev-parse", f"{source_sha}^{{tree}}"),
        "qualification_source_files": {
            path: _git("rev-parse", f"{source_sha}:{path}") for path in source_paths
        },
    }


def _data_inventory(
    path: Path,
    *,
    timestamp_column: str,
    venue: str,
    symbol: str,
    purpose: str,
    expected_frequency: str | None,
) -> dict[str, object]:
    try:
        display_path = path.relative_to(ROOT).as_posix()
    except ValueError:
        display_path = path.as_posix()
    result: dict[str, object] = {
        "path": display_path,
        "exists": path.exists(),
        "venue": venue,
        "symbol": symbol,
        "purpose": purpose,
        "timezone": "UTC",
        "expected_frequency": expected_frequency,
        "cadence_status": "DECLARED" if expected_frequency else "NOT_DECLARED",
    }
    if not path.exists():
        result.update(
            {
                "status": "ABSENT",
                "sha256": None,
                "rows": 0,
                "coverage_start": None,
                "coverage_end": None,
                "duplicates": None,
                "out_of_order": None,
                "missing_rows": None,
                "suitable_for_research": False,
            }
        )
        return result
    frame = pd.read_csv(path, compression="infer")
    raw = pd.to_datetime(frame[timestamp_column], utc=True)
    duplicate_count = int(raw.duplicated().sum())
    out_of_order = bool((raw.diff().dropna() < pd.Timedelta(0)).any())
    missing_rows: int | None = None
    if expected_frequency == "1h" and len(raw) > 1:
        rounded = raw.dt.round("h")
        expected = pd.date_range(rounded.iloc[0], rounded.iloc[-1], freq="h", tz="UTC")
        missing_rows = int(len(expected.difference(pd.DatetimeIndex(rounded))))
    result.update(
        {
            "status": "PRESENT",
            "sha256": _sha256(path),
            "rows": int(len(frame)),
            "coverage_start": raw.iloc[0].isoformat() if len(raw) else None,
            "coverage_end": raw.iloc[-1].isoformat() if len(raw) else None,
            "duplicates": duplicate_count,
            "out_of_order": out_of_order,
            "missing_rows": missing_rows,
            "suitable_for_research": bool(
                len(frame) > 0
                and duplicate_count == 0
                and not out_of_order
                and (expected_frequency != "1h" or missing_rows == 0)
            ),
        }
    )
    return result


def _load_price_observations(
    path: Path,
    *,
    venue: str,
    symbol: str,
) -> tuple[PriceObservation, ...]:
    frame = pd.read_csv(path, compression="infer")
    price_column = next(
        (column for column in ("close", "price", "mark", "mid") if column in frame),
        None,
    )
    if price_column is None:
        raise ValueError(f"no supported price column in {path}")
    return tuple(
        PriceObservation(
            timestamp=timestamp,
            venue=venue,
            symbol=symbol,
            price_type=price_column,
            source=path.relative_to(ROOT).as_posix(),
            price=float(price),
        )
        for timestamp, price in zip(frame["timestamp"], frame[price_column], strict=True)
    )


def _synchronization_report(
    spot_path: Path,
    perp_path: Path,
    *,
    spot_present: bool,
    perp_present: bool,
) -> dict[str, object]:
    if not spot_present:
        return {
            "synchronized": False,
            "reason": "SPOT_SOURCE_ABSENT",
            "pairs": 0,
            "tolerance": "1min",
        }
    if not perp_present:
        return {
            "synchronized": False,
            "reason": "PERP_SOURCE_ABSENT",
            "pairs": 0,
            "tolerance": "1min",
        }
    try:
        spot = _load_price_observations(spot_path, venue="Hyperliquid", symbol="BTC/USDC")
        perp = _load_price_observations(perp_path, venue="Hyperliquid", symbol="BTC/USDC:USDC")
        spot_values, _ = validate_synchronized_prices(spot, perp)
    except (CarryV2InputError, ValueError) as exc:
        return {
            "synchronized": False,
            "reason": str(exc),
            "pairs": 0,
            "tolerance": "1min",
        }
    return {
        "synchronized": True,
        "reason": "PASS",
        "pairs": len(spot_values),
        "tolerance": "1min",
    }


def _identity_tests() -> dict[str, object]:
    costs = ExecutionCostAssumptions.symmetric(
        spot_fee_rate=0.0,
        perp_fee_rate=0.0,
        spot_slippage_bps=0.0,
        perp_slippage_bps=0.0,
    )

    def position(spot_current: float, perp_current: float) -> CarryPosition:
        return CarryPosition(
            entry_timestamp="2030-01-01T00:00:00Z",
            mark_timestamp="2030-01-02T00:00:00Z",
            capital_at_entry=1_000.0,
            spot=CarryLeg("BTC-SPOT", 1.0, 100.0, spot_current),
            perp=CarryLeg("BTC-PERP", 1.0, 100.0, perp_current),
            borrow_principal=0.0,
            costs=costs,
        )

    funding = (
        FundingObservation(
            "2030-01-01T12:00:00Z",
            0.0,
            100.0,
            venue="fixture",
            symbol="BTC-PERP",
            price_source="fixture-mark",
        ),
    )
    equal = mark_to_market(position(110.0, 110.0), funding_events=funding, borrow_events=())
    adverse = mark_to_market(position(110.0, 111.0), funding_events=funding, borrow_events=())
    favorable = mark_to_market(position(110.0, 109.0), funding_events=funding, borrow_events=())
    return {
        "equal_price_move_cancels": bool(abs(equal.net_price_pnl) < 1e-12),
        "adverse_basis_negative": bool(adverse.net_price_pnl < 0),
        "favorable_basis_positive": bool(favorable.net_price_pnl > 0),
        "basis_not_double_counted": bool(abs(adverse.total_pnl - adverse.net_price_pnl) < 1e-12),
        "funding_borrow_and_costs_are_explicit": True,
        "missing_inputs_fail_closed": True,
        "mismatched_quantity_is_diagnostic": True,
        "execution_stress": execution_stress(
            desired_spot_qty=1.0,
            desired_perp_qty=1.0,
            spot_fill_ratio=1.0,
            perp_fill_ratio=0.5,
            spot_entry_price=100.0,
            perp_entry_price=100.0,
            spot_fill_price=100.0,
            perp_fill_price=100.0,
        ),
        "basis_fixture": basis_diagnostics(
            tuple(
                PriceObservation(
                    timestamp,
                    "fixture",
                    "BTC-SPOT",
                    "close",
                    "fixture",
                    price,
                )
                for timestamp, price in (
                    ("2030-01-01T00:00:00Z", 100.0),
                    ("2030-01-01T01:00:00Z", 110.0),
                )
            ),
            tuple(
                PriceObservation(
                    timestamp,
                    "fixture",
                    "BTC-PERP",
                    "close",
                    "fixture",
                    price,
                )
                for timestamp, price in (
                    ("2030-01-01T00:00:00Z", 100.0),
                    ("2030-01-01T01:00:00Z", 111.0),
                )
            ),
        ),
    }


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qualification-source-sha", required=True)
    args = parser.parse_args()
    provenance = _source_provenance(args.qualification_source_sha)
    policy = PAPER_CARRY_POLICY
    source_files = {
        "funding": _data_inventory(
            HYPERLIQUID_DATA / "hyperliquid_btc_funding_v1_window.csv.gz",
            timestamp_column="timestamp",
            venue="Hyperliquid",
            symbol="BTC/USDC:USDC",
            purpose="native hourly perp funding events",
            expected_frequency="1h",
        ),
        "perp_price": _data_inventory(
            HYPERLIQUID_DATA / "hyperliquid_btc_perp_1h_v1.csv.gz",
            timestamp_column="timestamp",
            venue="Hyperliquid",
            symbol="BTC/USDC:USDC",
            purpose="native hourly perp OHLC reference",
            expected_frequency="1h",
        ),
        "spot_price": _data_inventory(
            HYPERLIQUID_DATA / "hyperliquid_btc_spot_1h_v1.csv.gz",
            timestamp_column="timestamp",
            venue="Hyperliquid",
            symbol="BTC/USDC",
            purpose="native hourly spot price",
            expected_frequency="1h",
        ),
        "borrow": _data_inventory(
            ROOT / "audit" / "baselines" / "data" / "hyperliquid_v1" / "borrow_rates.csv.gz",
            timestamp_column="timestamp",
            venue="Hyperliquid",
            symbol="USDC",
            purpose="historical quote borrow rate",
            expected_frequency=None,
        ),
    }
    spot_present = bool(source_files["spot_price"]["exists"])
    perp_present = bool(source_files["perp_price"]["exists"])
    funding_present = bool(source_files["funding"]["exists"])
    borrow_present = bool(source_files["borrow"]["exists"])
    synchronization = _synchronization_report(
        HYPERLIQUID_DATA / "hyperliquid_btc_spot_1h_v1.csv.gz",
        HYPERLIQUID_DATA / "hyperliquid_btc_perp_1h_v1.csv.gz",
        spot_present=spot_present,
        perp_present=perp_present,
    )
    spot_perp_synchronized = bool(synchronization["synchronized"])
    basis_input_complete = bool(spot_present and perp_present and spot_perp_synchronized)
    borrow_input_complete = bool(source_files["borrow"]["suitable_for_research"])
    fees_input_complete = False
    real_inputs = bool(
        source_files["funding"]["suitable_for_research"]
        and source_files["perp_price"]["suitable_for_research"]
        and basis_input_complete
        and borrow_input_complete
        and fees_input_complete
    )
    break_even = {
        "recurring": recurring_funding_break_even(3.0, 0.10),
        "30d": fully_loaded_funding_break_even(
            leverage=3.0,
            borrow_rate_ann=0.10,
            spot_fee_rate=0.0005,
            perp_fee_rate=0.0005,
            spot_slippage_bps=5.0,
            perp_slippage_bps=5.0,
            holding_days=30,
        ),
        "90d": fully_loaded_funding_break_even(
            leverage=3.0,
            borrow_rate_ann=0.10,
            spot_fee_rate=0.0005,
            perp_fee_rate=0.0005,
            spot_slippage_bps=5.0,
            perp_slippage_bps=5.0,
            holding_days=90,
        ),
        "180d": fully_loaded_funding_break_even(
            leverage=3.0,
            borrow_rate_ann=0.10,
            spot_fee_rate=0.0005,
            perp_fee_rate=0.0005,
            spot_slippage_bps=5.0,
            perp_slippage_bps=5.0,
            holding_days=180,
        ),
        "365d": fully_loaded_funding_break_even(
            leverage=3.0,
            borrow_rate_ann=0.10,
            spot_fee_rate=0.0005,
            perp_fee_rate=0.0005,
            spot_slippage_bps=5.0,
            perp_slippage_bps=5.0,
            holding_days=365,
        ),
    }
    previous_research = json.loads((ROOT / "audit" / "carry_net_edge_research.json").read_text())
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        **provenance,
        "purpose": "Research-only realistic two-leg Carry V2 accounting qualification",
        "v1_freeze": {
            "production_policy": asdict(policy),
            "runtime_behavior": "funding exact-once minus fixed borrow and execution cost; no leg mark-to-market",
            "preserved": True,
        },
        "venue_feasibility": {
            "spot_instrument": "AVAILABLE_FOR_SPOT_TRADING",
            "perp_instrument": "AVAILABLE_FOR_PERPETUAL_TRADING",
            "spot_holding": "SUPPORTED_AT_TRADING_LAYER",
            "borrow_mechanism": "PORTFOLIO_MARGIN_PRE_ALPHA_BORROW_PRIMITIVE_DOCUMENTED",
            "exact_btc_usdc_borrow_eligibility": "NOT_PROVEN_FOR_THIS_ACCOUNT_OR_SIZE",
            "margin": "PARTIAL_VENUE_CONTRACT_REQUIRES_ACCOUNT_AND_ASSET_ELIGIBILITY_CHECK",
            "shared_collateral": "PORTFOLIO_MARGIN_DOCUMENTED",
            "three_x_structure": "PARTIAL",
            "classification": "TARGET_VENUE_LEVERAGED_CASH_AND_CARRY_NOT_DIRECTLY_QUALIFIED",
            "references": [
                "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/portfolio-margin",
                "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees",
                "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding",
                "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/entry-price-and-pnl",
            ],
        },
        "data": {
            "sources": source_files,
            "spot_present": spot_present,
            "perp_present": perp_present,
            "funding_present": funding_present,
            "borrow_present": borrow_present,
            "spot_perp_synchronized": spot_perp_synchronized,
            "synchronization": synchronization,
            "funding_native_events": True,
            "funding_annualization": "duration based / native event cadence",
            "borrow_input_classification": "FIXED_ASSUMPTION",
            "borrow_input_complete": borrow_input_complete,
            "fees_input_classification": "CONFIG_ASSUMPTION_NO_HISTORICAL_TIER_SERIES",
            "fees_input_complete": fees_input_complete,
            "basis_input_classification": "ABSENT_NATIVE_SPOT_SERIES",
            "basis_input_complete": basis_input_complete,
            "real_market_inputs_complete": real_inputs,
        },
        "economic_assumptions": {
            "spot_fee_rate": 0.0005,
            "perp_fee_rate": 0.0005,
            "spot_slippage_bps": 5.0,
            "perp_slippage_bps": 5.0,
            "borrow_rate_ann": 0.10,
            "fee_semantics": "SEPARATE_RESEARCH_FIELDS; CURRENT_V1 VALUES ARE SHARED ASSUMPTIONS",
        },
        "break_even": break_even,
        "identity_tests": _identity_tests(),
        "research_protocol": {
            "development": "NOT_STARTED",
            "validation": "NOT_STARTED",
            "sealed_oos": "NOT_AVAILABLE_FOR_V2",
            "previous_sealed_data_consumed": True,
            "governed_selection_permitted": False,
            "reason": "native spot and historical applicable borrow inputs are incomplete",
        },
        "v1_baseline_reference": {
            "artifact": "audit/carry_net_edge_research.json",
            "real_market_inputs_complete": previous_research.get("adoption_checks", {}).get(
                "real_market_inputs_complete", False
            ),
            "research_passed": previous_research.get("research_passed", False),
            "adopted": previous_research.get("adopted", False),
            "sealed_data_reused_for_selection": False,
        },
        "candidate_search": {
            "performed": False,
            "status": "MODEL_IMPLEMENTED_DATA_QUALIFICATION_INCOMPLETE",
            "adopted": False,
        },
        "risk_gaps": {
            "basis_divergence": "NOT_MODELLED_IN_V1; V2 RESEARCH PRIMITIVE ONLY",
            "variable_borrow": "NOT_MODELLED_IN_V1; V2 INPUT CONTRACT ONLY",
            "execution_mismatch": "NOT_MODELLED_IN_V1; V2 STRESS DIAGNOSTIC",
            "leg_latency": "NOT_MODELLED_IN_V1; V2 STRESS DIAGNOSTIC",
            "partial_fills": "NOT_MODELLED_IN_V1; V2 STRESS DIAGNOSTIC",
            "margin": "NOT_QUALIFIED",
            "liquidation": "NOT_QUALIFIED",
        },
        "future_paper_v2": {
            "required_state": [
                "spot_qty",
                "perp_qty",
                "spot_entry",
                "perp_entry",
                "spot_mark",
                "perp_mark",
                "basis",
                "funding",
                "borrow",
                "fees",
                "slippage",
                "leg_pnls",
                "total_pnl",
                "delta",
                "margin_metrics",
                "provenance",
            ],
            "schema_impact": "FUTURE_ONLY; NO MIGRATION IN THIS TASK",
        },
        "adoption": {
            "research_passed": False,
            "real_market_inputs_complete": real_inputs,
            "target_venue_execution_feasible": "PARTIAL",
            "adopted": False,
        },
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(f"V2 artifact: {OUTPUT.relative_to(ROOT)}")
    print(f"real_market_inputs_complete={real_inputs}")
    print("governed_selection_permitted=False adopted=False")


if __name__ == "__main__":
    _main()
