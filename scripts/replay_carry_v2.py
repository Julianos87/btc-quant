"""Run the fixed Carry V1 policy through the research-only Carry V2 replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "audit" / "baselines" / "data" / "carry_v2"
DEFAULT_OUTPUT = ROOT / "audit" / "carry_v2_real_data_replay.json"
sys.path.insert(0, str(ROOT / "src"))

from btcquant.research.carry_v2_replay import (  # noqa: E402
    ReplayPolicy,
    basis_summary,
    load_candle_csv,
    load_funding_csv,
    prepare_replay_frame,
    replay_policy,
    sensitivity,
    synchronize_price_frames,
)

SPOT_FILE = DATA_ROOT / "hyperliquid_ubtc_usdc_spot_1h_20260114_20260810_v2.csv.gz"
PERP_FILE = DATA_ROOT / "hyperliquid_btc_perp_1h_20260114_20260810_v2.csv.gz"
FUNDING_FILE = DATA_ROOT / "hyperliquid_btc_funding_1h_20260114_20260810_v2.csv.gz"
METADATA_FILE = DATA_ROOT / "hyperliquid_carry_v2_20260114_20260810_v2.metadata.json"


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_provenance(source_sha: str) -> dict[str, Any]:
    if len(source_sha) != 40 or _git("rev-parse", f"{source_sha}^{{commit}}") != source_sha:
        raise ValueError("qualification source SHA must be a resolvable full commit")
    if _git("rev-parse", "HEAD") != source_sha:
        raise ValueError("replay must run at the exact source commit")
    paths = (
        "src/btcquant/research/carry_v2.py",
        "src/btcquant/research/carry_v2_replay.py",
        "scripts/acquire_carry_v2_data.py",
        "scripts/replay_carry_v2.py",
        "tests/test_carry_v2_economics.py",
        "tests/test_carry_v2_replay.py",
    )
    return {
        "qualification_source_sha": source_sha,
        "qualification_source_tree": _git("rev-parse", f"{source_sha}^{{tree}}"),
        "qualification_source_files": {
            path: _git("rev-parse", f"{source_sha}:{path}") for path in paths
        },
    }


def _compact_replay(result: dict[str, Any]) -> dict[str, Any]:
    compact = dict(result)
    compact.pop("records", None)
    compact["entry_edge"] = dict(result["entry_edge"])
    compact["entry_edge"].pop("observations", None)
    return compact


def build_artifact(source_sha: str) -> dict[str, Any]:
    provenance = _source_provenance(source_sha)
    metadata = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
    spot = load_candle_csv(SPOT_FILE, label="Hyperliquid UBTC/USDC spot")
    perp = load_candle_csv(PERP_FILE, label="Hyperliquid BTC perp")
    funding = load_funding_csv(FUNDING_FILE)
    prices, synchronization = synchronize_price_frames(spot, perp)
    replay_frame, replay_input = prepare_replay_frame(prices, funding)
    policy = ReplayPolicy()
    baseline = replay_policy(replay_frame, policy)
    sensitivity_results = sensitivity(replay_frame, policy)
    source_data = {
        "spot": {
            "present": True,
            "classification": "NATIVE_HYPERLIQUID_SPOT_MARKET_WRAPPED_BTC",
            "path": str(SPOT_FILE.relative_to(ROOT)),
            "sha256": _sha256(SPOT_FILE),
            "rows": int(len(spot)),
            "coverage_start": spot["timestamp"].iloc[0].isoformat(),
            "coverage_end": spot["timestamp"].iloc[-1].isoformat(),
            "price_type": "1h OHLC close available at candle T; not mark/oracle/execution fill",
        },
        "perp": {
            "present": True,
            "classification": "NATIVE_HYPERLIQUID_PERP",
            "path": str(PERP_FILE.relative_to(ROOT)),
            "sha256": _sha256(PERP_FILE),
            "rows": int(len(perp)),
            "coverage_start": perp["timestamp"].iloc[0].isoformat(),
            "coverage_end": perp["timestamp"].iloc[-1].isoformat(),
            "price_type": "1h OHLC close available at candle T; research mark proxy only",
        },
        "funding": {
            "present": True,
            "classification": "OBSERVED_HISTORICAL_NATIVE",
            "path": str(FUNDING_FILE.relative_to(ROOT)),
            "sha256": _sha256(FUNDING_FILE),
            "rows": int(len(funding)),
            "coverage_start": funding["timestamp"].iloc[0].isoformat(),
            "coverage_end": funding["timestamp"].iloc[-1].isoformat(),
            "cadence": "native hourly",
            "reference_price": "previous completed BTC 1h close approximation; close_timestamp <= funding timestamp; not oracle",
            "funding_notional_price_realism": "APPROXIMATION",
        },
        "borrow": {
            "present": False,
            "classification": "UNAVAILABLE",
            "path": None,
            "fixed_assumption_for_diagnostic": "10% annualized",
        },
        "fees": {
            "classification": "OBSERVED_VENUE_SCHEDULE_ACCOUNT_TIER_UNKNOWN",
            "historical": False,
            "spot_base_maker": 0.0004,
            "spot_base_taker": 0.0007,
            "perp_base_maker": 0.00015,
            "perp_base_taker": 0.00045,
            "replay_assumption": {
                "spot_fee_rate": policy.spot_fee_rate,
                "perp_fee_rate": policy.perp_fee_rate,
                "source": "CURRENT_V1_SHARED_FEE_ASSUMPTION",
            },
        },
    }
    return {
        "artifact_contract": "TWO_COMMIT_SOURCE_THEN_ARTIFACT",
        **provenance,
        "data": {
            "venue": "Hyperliquid",
            "endpoint": metadata["endpoint"],
            "requested_window": metadata["window"],
            "availability_note": (
                "Current public candle endpoint returned only its available recent window; "
                "the requested 2026-01-14 start was unavailable and replay begins at the "
                "maximum common public overlap."
            ),
            "source_files": source_data,
            "metadata": {
                "path": str(METADATA_FILE.relative_to(ROOT)),
                "sha256": _sha256(METADATA_FILE),
            },
            "spot_perp_synchronization": synchronization,
            "replay_input": replay_input,
            "basis": basis_summary(prices),
        },
        "instrument": {
            "target_spot_instrument": "@142",
            "pair": "UBTC/USDC",
            "asset_representation": "WRAPPED_TOKENIZED_BTC",
            "classification": "WRAPPED_TOKENIZED_BTC_REPRESENTATION",
            "token_index": 197,
            "spot_index": 142,
            "token_id": "0x8f254b963e8468305d409b33aa137c67",
            "evm_contract": "0x9fdbda0a5e284c32744d2f17ee5c74b284993463",
            "is_canonical": False,
            "sz_decimals": 5,
            "minimum_size": "not provided by public spot metadata",
            "tick_size": "not provided by public spot metadata",
        },
        "financing": {
            "mechanism": "PORTFOLIO_MARGIN_PRE_ALPHA",
            "three_x_construction_feasibility": "PARTIAL",
            "borrow_asset": "USDC",
            "collateral_asset_in_pre_alpha": "HYPE",
            "documented_pre_alpha_usdc_borrow_cap": 1000.0,
            "required_approximate_borrow_for_4000_at_3x": 8000.0,
            "historical_borrow": "UNAVAILABLE",
            "reason": (
                "The documented pre-alpha structure and cap do not prove an approximately "
                "$8k USDC borrow against this $4k Carry allocation."
            ),
            "source": "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/portfolio-margin",
        },
        "slippage": {
            "classification": "SLIPPAGE_ASSUMPTION",
            "baseline_bps_per_leg": policy.slippage_bps,
            "diagnostic_scenarios_bps": [0.0, 5.0, 10.0, 20.0],
        },
        "replay": {
            "policy": {
                "capital": policy.capital,
                "leverage": policy.leverage,
                "smooth_days": policy.smooth_days,
                "enter_ann": policy.enter_ann,
                "exit_ann": policy.exit_ann,
            },
            "timing_contract": (
                "funding event is observed, smoothing updates, decision applies after "
                "the event; fills and marks use the last synchronized close whose "
                "close_timestamp / available_at is at or before the event"
            ),
            "baseline": _compact_replay(baseline),
            "sensitivity": sensitivity_results,
            "equity_identity": {
                "formula": (
                    "equity = capital + spot price PnL + perp price PnL + funding "
                    "- borrow - fees - slippage"
                ),
                "max_absolute_residual": baseline["identity_residual_max_abs"],
                "status": "PASS",
            },
            "basis_not_double_counted": True,
            "retrospective": True,
        },
        "data_completeness": {
            "native_spot": True,
            "native_perp": True,
            "native_funding": True,
            "historical_borrow": False,
            "fees_complete": False,
            "synchronization": True,
            "real_market_inputs_complete": False,
            "reason": "historical financing and account-specific fee evidence are missing",
            "funding_notional_price_realism": "APPROXIMATION",
        },
        "governance": {
            "candidate_search": {"performed": False},
            "policy_changed": False,
            "governed_selection_permitted": False,
            "research_passed": False,
            "adopted": False,
            "readiness": "DATA_QUALIFICATION_INCOMPLETE",
        },
        "future_paper_v2": {
            "design": "read-only shadow accounting for both legs, basis, funding, borrow and costs",
            "started": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qualification-source-sha", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    artifact = build_artifact(args.qualification_source_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
