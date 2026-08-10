"""Combine les briques BTC prometteuses sans confondre coûts acquis et hypothétiques."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from btcquant.backtest import BacktestEngine
from btcquant.carry import add_funding_columns, load_funding
from btcquant.config import execution_config_from_config, load_config, risk_from_config
from btcquant.console import enable_utf8_output
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.domain import ExecutionSimulator
from btcquant.provenance import quantitative_source_sha256
from btcquant.risk import RiskConfig
from btcquant.strategies.trend_ls import TrendLS
from scripts.research_btc_cost_filter import (
    CONFIG,
    DEVELOPMENT_END,
    VALIDATION_END,
    _period,
    _sha256,
    _summary,
)

enable_utf8_output()

OUTPUT = ROOT / "audit" / "btc_combined_research.json"
STRUCTURES = {
    "deployed": {
        "horizons": (20, 55, 100),
        "weights": (0.3333, 0.3333, 0.3334),
        "atr_mult": 3.0,
    },
    "fast_candidate": {
        "horizons": (10, 20, 40),
        "weights": (0.50, 0.30, 0.20),
        "atr_mult": 3.5,
    },
}
FUNDING_MODES = {
    "disabled": {"threshold": None, "floor": 1.0},
    "continuous": {"threshold": 0.0004, "floor": 0.25},
}
COST_MODES = {
    "current": {"fee_rate": 0.0005, "slippage_bps": 5.0},
    "half_slippage": {"fee_rate": 0.0005, "slippage_bps": 2.5},
    "maker_proxy": {"fee_rate": 0.0002, "slippage_bps": 1.0},
}


def _run(
    cfg: dict,
    frame: pd.DataFrame,
    *,
    structure: dict,
    funding_mode: dict,
    costs: dict,
    profile: str,
    no_trade_before: pd.Timestamp | None = None,
):
    scenario_cfg = deepcopy(cfg)
    scenario_cfg["execution"]["simulation"]["profile"] = profile
    scenario_cfg["costs"]["slippage_bps"] = costs["slippage_bps"]
    base_risk = risk_from_config(scenario_cfg)
    curves = []
    trades = 0
    for horizon, weight in zip(structure["horizons"], structure["weights"], strict=True):
        risk = RiskConfig(
            **{**base_risk.__dict__, "initial_capital": base_risk.initial_capital * weight}
        )
        strategy = TrendLS(
            donchian=horizon,
            atr_mult=structure["atr_mult"],
            adx_min=20,
            funding_long_max=0.0008,
            funding_short_min=-0.0008,
            pyramid_atr_step=0.5,
            pyramid_add_fraction=0.30,
            pyramid_max_adds=1,
            funding_sizing_threshold=funding_mode["threshold"],
            funding_sizing_floor=funding_mode["floor"],
        )
        result = BacktestEngine(
            risk=risk,
            funding_rate_8h=float(scenario_cfg["costs"]["funding_rate_8h"]),
            allow_short=True,
            execution_simulator=ExecutionSimulator(
                execution_config_from_config(scenario_cfg, float(costs["fee_rate"]))
            ),
        ).run(strategy, frame, no_trade_before=no_trade_before)
        curves.append(result.equity)
        trades += len(result.trades)
    return pd.concat(curves, axis=1, sort=True).ffill().dropna().sum(axis=1), trades


def _candidate(
    equity: pd.Series,
    trades: int,
    structure_name: str,
    funding_name: str,
    cost_name: str,
) -> dict:
    return {
        "structure": structure_name,
        "funding": funding_name,
        "costs": cost_name,
        "trades": trades,
        "development": _period(equity, None, DEVELOPMENT_END),
        "validation": _period(equity, DEVELOPMENT_END + pd.Timedelta(seconds=1), VALIDATION_END),
        "sealed_test": _period(equity, VALIDATION_END + pd.Timedelta(seconds=1), None),
        "full": _summary(equity),
    }


def _score(candidate: dict) -> float:
    development = candidate["development"]
    validation = candidate["validation"]
    if development["sharpe"] <= 0 or validation["sharpe"] <= 0:
        return float("-inf")
    return min(development["cagr"], validation["cagr"]) + 0.10 * min(
        development["sharpe"], validation["sharpe"]
    )


def _walkforward(
    cfg: dict,
    frame: pd.DataFrame,
    candidates: list[dict],
    equities: dict,
    *,
    cost_name: str,
) -> dict:
    eligible = [item for item in candidates if item["costs"] == cost_name]
    selected_segments = []
    baseline_segments = []
    folds = []
    selected_capital = baseline_capital = 1.0
    for year in range(2021, 2027):
        start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        end = pd.Timestamp(f"{year + 1}-01-01", tz="UTC")

        def training_score(item: dict, fold_start: pd.Timestamp = start) -> float:
            key = (item["structure"], item["funding"], item["costs"])
            training = equities[key][equities[key].index < fold_start]
            metrics = _summary(training)
            if metrics["sharpe"] <= 0 or metrics["max_drawdown"] < -0.60:
                return float("-inf")
            return metrics["cagr"] + 0.10 * metrics["sharpe"]

        selected = max(eligible, key=training_score)
        selected_equity, _ = _run(
            cfg,
            frame,
            structure=STRUCTURES[selected["structure"]],
            funding_mode=FUNDING_MODES[selected["funding"]],
            costs=COST_MODES[cost_name],
            profile="normal",
            no_trade_before=start,
        )
        baseline_equity, _ = _run(
            cfg,
            frame,
            structure=STRUCTURES["deployed"],
            funding_mode=FUNDING_MODES["disabled"],
            costs=COST_MODES[cost_name],
            profile="normal",
            no_trade_before=start,
        )
        selected_segment = selected_equity[
            (selected_equity.index >= start) & (selected_equity.index < end)
        ]
        baseline_segment = baseline_equity[
            (baseline_equity.index >= start) & (baseline_equity.index < end)
        ]
        if selected_segment.empty or baseline_segment.empty:
            continue
        selected_segment = selected_capital * selected_segment / selected_segment.iloc[0]
        baseline_segment = baseline_capital * baseline_segment / baseline_segment.iloc[0]
        selected_capital = float(selected_segment.iloc[-1])
        baseline_capital = float(baseline_segment.iloc[-1])
        selected_segments.append(selected_segment)
        baseline_segments.append(baseline_segment)
        folds.append(
            {
                "year": year,
                "selected_structure": selected["structure"],
                "selected_funding": selected["funding"],
                "selected_return": float(selected_segment.iloc[-1] / selected_segment.iloc[0] - 1),
                "baseline_return": float(baseline_segment.iloc[-1] / baseline_segment.iloc[0] - 1),
            }
        )
    selected_oos = pd.concat(selected_segments)
    baseline_oos = pd.concat(baseline_segments)
    return {
        "methodology": (
            "expanding-window annuel, chaque pli redémarre à plat; univers "
            "d'hypothèses informé par la recherche antérieure"
        ),
        "folds": folds,
        "selected": _summary(selected_oos),
        "baseline": _summary(baseline_oos),
    }


def main() -> None:
    cfg = load_config(CONFIG)
    base = load_ohlcv(
        cfg["exchange"],
        cfg["symbol"],
        cfg["data"]["base_timeframe"],
        cfg["data"]["since"],
        data_dir=ROOT / cfg["data"]["dir"],
        refresh=False,
    )
    funding = load_funding(
        f"{cfg['symbol']}:{cfg['quote_currency']}",
        data_dir=ROOT / "data",
        refresh=False,
    )
    frame = add_funding_columns(
        resample(base, TIMEFRAME_TO_PANDAS["4h"]),
        funding,
        TIMEFRAME_TO_PANDAS["4h"],
    )
    candidates = []
    equities = {}
    combinations = product(STRUCTURES, FUNDING_MODES, COST_MODES)
    for structure_name, funding_name, cost_name in combinations:
        equity, trades = _run(
            cfg,
            frame,
            structure=STRUCTURES[structure_name],
            funding_mode=FUNDING_MODES[funding_name],
            costs=COST_MODES[cost_name],
            profile="normal",
        )
        key = (structure_name, funding_name, cost_name)
        equities[key] = equity
        item = _candidate(equity, trades, *key)
        item["selection_score"] = _score(item)
        candidates.append(item)

    baseline = next(
        item
        for item in candidates
        if (item["structure"], item["funding"], item["costs"])
        == ("deployed", "disabled", "current")
    )
    winners_by_cost = {}
    for cost_name in COST_MODES:
        eligible = [item for item in candidates if item["costs"] == cost_name]
        winners_by_cost[cost_name] = max(eligible, key=lambda item: item["selection_score"])
    deployable = winners_by_cost["current"]
    for cost_name, winner in winners_by_cost.items():
        stress_equity, stress_trades = _run(
            cfg,
            frame,
            structure=STRUCTURES[winner["structure"]],
            funding_mode=FUNDING_MODES[winner["funding"]],
            costs=COST_MODES[cost_name],
            profile="stress",
        )
        winner["stress_full"] = {**_summary(stress_equity), "trades": stress_trades}
    walkforward = {
        cost_name: _walkforward(
            cfg,
            frame,
            candidates,
            equities,
            cost_name=cost_name,
        )
        for cost_name in ("current", "half_slippage")
    }
    adoption_checks = {
        "full_cagr_above_baseline": deployable["full"]["cagr"] > baseline["full"]["cagr"],
        "full_sharpe_above_baseline": deployable["full"]["sharpe"] > baseline["full"]["sharpe"],
        "full_drawdown_no_worse": deployable["full"]["max_drawdown"]
        >= baseline["full"]["max_drawdown"],
        "sealed_test_cagr_no_worse": deployable["sealed_test"]["cagr"]
        >= baseline["sealed_test"]["cagr"],
        "sealed_test_sharpe_no_worse": deployable["sealed_test"]["sharpe"]
        >= baseline["sealed_test"]["sharpe"],
    }
    adopted = all(adoption_checks.values())
    conditional_checks = {
        cost_name: {
            "full_cagr_above_current_baseline": winner["full"]["cagr"] > baseline["full"]["cagr"],
            "full_sharpe_above_current_baseline": winner["full"]["sharpe"]
            > baseline["full"]["sharpe"],
            "full_drawdown_no_worse_than_current_baseline": winner["full"]["max_drawdown"]
            >= baseline["full"]["max_drawdown"],
            "sealed_test_cagr_no_worse_than_current_baseline": winner["sealed_test"]["cagr"]
            >= baseline["sealed_test"]["cagr"],
            "sealed_test_sharpe_no_worse_than_current_baseline": winner["sealed_test"]["sharpe"]
            >= baseline["sealed_test"]["sharpe"],
        }
        for cost_name, winner in winners_by_cost.items()
        if cost_name != "current"
    }
    data_paths = (
        ROOT / "data" / "binance_BTC-USDT_1h.csv",
        ROOT / "data" / "binanceusdm_BTCUSDT_USDT_funding.csv",
    )
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Combinaison BTC des briques prometteuses",
        "protocol": {
            "candidate_count": len(candidates),
            "sealed_test_used_for_selection": False,
            "hypothetical_cost_modes": ["half_slippage", "maker_proxy"],
        },
        "provenance": {
            "script_sha256": _sha256(Path(__file__)),
            "source_tree_sha256": quantitative_source_sha256(Path(__file__)),
            "config": {"path": CONFIG.relative_to(ROOT).as_posix(), "sha256": _sha256(CONFIG)},
            "data": [
                {"path": path.relative_to(ROOT).as_posix(), "sha256": _sha256(path)}
                for path in data_paths
            ],
        },
        "baseline": baseline,
        "deployable_current_costs": deployable,
        "winners_by_cost": winners_by_cost,
        "retrospective_walkforward": walkforward,
        "adoption_checks": adoption_checks,
        "conditional_checks": conditional_checks,
        "execution_validation_required": True,
        "adopted": adopted,
        "all_candidates": candidates,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for cost_name, winner in winners_by_cost.items():
        metrics = winner["full"]
        print(
            f"{cost_name:14} {winner['structure']}/{winner['funding']} | "
            f"CAGR {metrics['cagr']:+.1%} | Sharpe {metrics['sharpe']:.2f} "
            f"| DD {metrics['max_drawdown']:+.1%} | "
            f"test {winner['sealed_test']['cagr']:+.1%}"
        )
    for cost_name, result in walkforward.items():
        selected_metrics = result["selected"]
        baseline_metrics = result["baseline"]
        print(
            f"WF {cost_name:11} sélection CAGR {selected_metrics['cagr']:+.1%}, "
            f"Sharpe {selected_metrics['sharpe']:.2f} | baseline "
            f"{baseline_metrics['cagr']:+.1%}, {baseline_metrics['sharpe']:.2f}"
        )
    print(f"Adoption immédiate : {'OUI' if adopted else 'NON'}")
    print(f"Artefact : {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
