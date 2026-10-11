"""Fixed offline cash-vs-credit diagnostic; no optimization or adoption claim."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.carry import smooth_funding_events  # noqa: E402
from btcquant.config import (  # noqa: E402
    carry_execution_from_config,
    carry_policy_from_config,
    load_config,
)
from btcquant.execution.carry_paper import FinancingPlan, expected_net_carry  # noqa: E402
from btcquant.research.carry_v2_replay import (  # noqa: E402
    load_candle_csv,
    load_funding_csv,
    prepare_replay_frame,
    synchronize_price_frames,
)
from btcquant.research.governance import DIAGNOSTIC_LABEL  # noqa: E402
from btcquant.research.search_gates import require_diagnostic_label  # noqa: E402


def diagnostic() -> dict:
    require_diagnostic_label(DIAGNOSTIC_LABEL)
    data = ROOT / "audit/baselines/data/carry_v2"
    paths = {
        "spot": data / "hyperliquid_ubtc_usdc_spot_1h_20260114_20260810_v2.csv.gz",
        "perp": data / "hyperliquid_btc_perp_1h_20260114_20260810_v2.csv.gz",
        "funding": data / "hyperliquid_btc_funding_1h_20260114_20260810_v2.csv.gz",
    }
    prices, sync = synchronize_price_frames(
        load_candle_csv(paths["spot"], label="UBTC spot"),
        load_candle_csv(paths["perp"], label="BTC perp"),
    )
    frame, inputs = prepare_replay_frame(prices, load_funding_csv(paths["funding"]))
    funding = pd.Series(frame.native_rate.to_numpy(), index=pd.DatetimeIndex(frame.timestamp))
    signal = smooth_funding_events(funding, smooth_days=14, funding_interval="1h")
    scenarios = {}
    for label, config_name in (("credit_3x", "config.yaml"), ("cash_funded", "carry_cash.yaml")):
        config_path = ROOT / "environments/paper" / config_name
        cfg = load_config(config_path)
        policy = carry_policy_from_config(cfg)
        execution = carry_execution_from_config(cfg)
        observations = []
        for i in range(336, len(frame) - 720, 720):
            entry = frame.iloc[i]
            end = frame.iloc[i + 720]
            following = frame.iloc[i + 1 : i + 721]
            ratio = float(entry.perp_price / entry.spot_price)
            cost_rate = policy.fee_rate + policy.slippage_bps / 10000
            notional = policy.capital * policy.leverage
            if not execution.borrow_enabled:
                notional = (
                    policy.capital
                    * execution.cash_utilization
                    / (1 + float(execution.initial_margin_rate) * ratio + cost_rate * (1 + ratio))
                )
            plan = FinancingPlan.build(
                capital_available=policy.capital,
                target_notional=notional,
                spot_price=float(entry.spot_price),
                perp_mark_price=float(entry.perp_price),
                leverage=policy.leverage,
                max_borrow=execution.max_borrow,
                initial_margin_rate=execution.initial_margin_rate,
                spot_fee_rate=policy.fee_rate,
                perp_fee_rate=policy.fee_rate,
                spot_slippage_bps=policy.slippage_bps,
                perp_slippage_bps=policy.slippage_bps,
            )
            economics = expected_net_carry(
                funding_ann=float(signal.annualized.iloc[i]),
                notional=notional,
                borrow_rate_ann=policy.borrow_rate_ann,
                debt=plan.borrow_principal,
                holding_days=execution.holding_days,
                entry_cost=plan.estimated_entry_cost,
                exit_cost=plan.estimated_entry_cost,
                uncertainty_reserve=notional * execution.uncertainty_reserve_rate,
            )
            qty = plan.spot_qty
            funding_pnl = float((following.native_rate * following.reference_price).sum()) * qty
            basis_pnl = qty * float(
                end.spot_price - entry.spot_price + entry.perp_price - end.perp_price
            )
            years = (pd.Timestamp(end.timestamp) - pd.Timestamp(entry.timestamp)).total_seconds()
            years /= 365.25 * 86400
            borrow_cost = plan.borrow_principal * policy.borrow_rate_ann * years
            exit_cost = qty * float(end.spot_price + end.perp_price) * cost_rate
            # Separate perp account: spot gains are not available short collateral.
            perp_cash = (
                float(plan.perp_initial_margin)
                + qty * (float(entry.perp_price) - following.perp_price)
                + qty * (following.native_rate * following.reference_price).cumsum()
            )
            maintenance = qty * following.perp_price * float(execution.maintenance_margin_rate)
            observations.append(
                {
                    "start": pd.Timestamp(entry.timestamp).isoformat(),
                    "end": pd.Timestamp(end.timestamp).isoformat(),
                    "plan": plan.as_dict(),
                    "causal_funding_ann": float(signal.annualized.iloc[i]),
                    "expected": economics,
                    "entry_eligible": bool(plan.qualified and economics["qualified"])
                    and float(signal.annualized.iloc[i]) > policy.enter_ann,
                    "hypothetical_30d_funding": funding_pnl,
                    "hypothetical_30d_basis": basis_pnl,
                    "hypothetical_30d_borrow": borrow_cost,
                    "hypothetical_round_trip_cost": plan.estimated_entry_cost + exit_cost,
                    "hypothetical_net_before_reserve": funding_pnl
                    + basis_pnl
                    - borrow_cost
                    - plan.estimated_entry_cost
                    - exit_cost,
                    "maintenance_breach_if_held": bool((perp_cash <= maintenance).any()),
                }
            )
        scenarios[label] = {
            "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "windows": observations,
            "eligible_windows": sum(o["entry_eligible"] for o in observations),
        }
    basis = frame.perp_price / frame.spot_price - 1
    adverse = (basis.shift(-720) - basis).dropna()
    return {
        "label": DIAGNOSTIC_LABEL,
        "status": "DIAGNOSTIC_ONLY_NOT_PROFITABILITY_QUALIFICATION",
        "selection_performed": False,
        "parameters_optimized": False,
        "source_files_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "scripts/diagnose_carry_cash.py",
                "src/btcquant/config.py",
                "src/btcquant/execution/carry_paper.py",
                "src/btcquant/execution/carry_runner.py",
                "src/btcquant/carry.py",
                "src/btcquant/research/carry_v2_replay.py",
            )
        },
        "reserve_rate_retained": 0.02,
        "sources": {
            key: {
                "path": str(path.relative_to(ROOT)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for key, path in paths.items()
        },
        "synchronization": sync,
        "inputs": inputs,
        "basis_30d_adverse_change": {
            "p95": float(adverse.quantile(0.95)),
            "p99": float(adverse.quantile(0.99)),
            "max": float(adverse.max()),
            "overlapping_observations": len(adverse),
        },
        "limits": [
            "Previously inspected history, not a blind holdout or future performance proof.",
            "Fixed non-overlapping 30-day hypothetical holds; no executed-strategy return claim.",
            "Hourly closes proxy execution; fee and slippage assumptions lack historical books.",
            "Borrow rate 10% is a scenario assumption; cash-funded principal is zero.",
            "Basis-only quantiles omit funding forecast error, depeg and execution tail risk.",
            "Reserve is an admission cushion, not a realized fee; margins can force early exit.",
        ],
        "scenarios": scenarios,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(diagnostic(), indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
