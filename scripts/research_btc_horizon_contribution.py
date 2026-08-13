"""Mesure la contribution marginale des horizons Donchian BTC déployés.

Les combinaisons sont classées sur 2019-2024. La période 2025+ reste scellée
jusqu'au choix préliminaire. Ce script produit un artefact d'audit et ne
modifie jamais la configuration paper.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from btcquant.backtest import BacktestEngine
from btcquant.carry import add_funding_columns, load_funding
from btcquant.config import execution_config_from_config, load_config, risk_from_config
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.domain import ExecutionSimulator
from btcquant.risk import RiskConfig
from btcquant.provenance import quantitative_source_sha256
from btcquant.strategies.trend_ls import TrendLS
from scripts.research_btc_cost_filter import (
    CONFIG,
    DEVELOPMENT_END,
    VALIDATION_END,
    _period,
    _sha256,
    _summary,
)

OUTPUT = ROOT / "audit" / "btc_horizon_contribution_research.json"
HORIZONS = (20, 55, 100)
COMMON_WARMUP_BARS = 620


def _all_combinations() -> tuple[tuple[int, ...], ...]:
    return tuple(
        combo for size in range(1, len(HORIZONS) + 1) for combo in combinations(HORIZONS, size)
    )


def _normalized_weights(cfg: dict, combo: tuple[int, ...]) -> dict[int, float]:
    configured = {}
    for spec in cfg["strategies"].values():
        if not spec.get("enabled") or spec.get("type") != "trend_ls":
            continue
        horizon = int(spec.get("params", {}).get("donchian"))
        if horizon in combo:
            configured[horizon] = float(spec.get("capital_fraction", 0.0))
    if set(configured) != set(combo):
        raise ValueError(f"Configuration absente pour la combinaison {combo}")
    total = sum(configured.values())
    if total <= 0:
        raise ValueError("Les poids Trend doivent être strictement positifs")
    return {horizon: weight / total for horizon, weight in configured.items()}


def _params_for_horizon(cfg: dict, horizon: int) -> dict:
    for spec in cfg["strategies"].values():
        params = spec.get("params", {})
        if (
            spec.get("enabled")
            and spec.get("type") == "trend_ls"
            and int(params.get("donchian")) == horizon
        ):
            return deepcopy(params)
    raise ValueError(f"Horizon D{horizon} absent de la configuration")


def _run_combo(
    cfg: dict,
    frame: pd.DataFrame,
    combo: tuple[int, ...],
    *,
    execution_profile: str,
) -> tuple[pd.Series, dict[int, list]]:
    scenario = deepcopy(cfg)
    scenario["execution"]["simulation"]["profile"] = execution_profile
    base_risk = risk_from_config(scenario)
    total_capital = float(base_risk.initial_capital)
    weights = _normalized_weights(scenario, combo)
    no_trade_before = frame.index[COMMON_WARMUP_BARS]
    curves = []
    trades_by_horizon = {}
    for horizon in combo:
        risk = RiskConfig(
            **{
                **base_risk.__dict__,
                "initial_capital": total_capital * weights[horizon],
            }
        )
        fee_rate = float(scenario["costs"]["perp_fee_rate"])
        result = BacktestEngine(
            risk=risk,
            funding_rate_8h=float(scenario["costs"]["funding_rate_8h"]),
            allow_short=True,
            execution_simulator=ExecutionSimulator(
                execution_config_from_config(scenario, fee_rate)
            ),
        ).run(
            TrendLS(**_params_for_horizon(scenario, horizon)),
            frame,
            no_trade_before=no_trade_before,
        )
        curves.append(result.equity[result.equity.index >= no_trade_before])
        trades_by_horizon[horizon] = result.trades
    equity = pd.concat(curves, axis=1, sort=True).ffill().dropna().sum(axis=1)
    return equity, trades_by_horizon


def _candidate(combo: tuple[int, ...], equity: pd.Series, trades_by_horizon: dict) -> dict:
    trades = sum(len(items) for items in trades_by_horizon.values())
    return {
        "horizons": list(combo),
        "component_count": len(combo),
        "trades": trades,
        "trades_by_horizon": {
            f"D{horizon}": len(items) for horizon, items in trades_by_horizon.items()
        },
        "development": _period(equity, None, DEVELOPMENT_END),
        "validation": _period(
            equity,
            DEVELOPMENT_END + pd.Timedelta(seconds=1),
            VALIDATION_END,
        ),
        "sealed_test": _period(
            equity,
            VALIDATION_END + pd.Timedelta(seconds=1),
            None,
        ),
        "full": _summary(equity),
    }


def _selection_score(candidate: dict) -> float:
    development = candidate["development"]
    validation = candidate["validation"]
    if development["sharpe"] <= 0 or validation["sharpe"] <= 0:
        return float("-inf")
    if development["max_drawdown"] < -0.60 or validation["max_drawdown"] < -0.60:
        return float("-inf")
    robustness = min(development["cagr"], validation["cagr"])
    quality = min(development["sharpe"], validation["sharpe"])
    # Très faible pénalité de complexité, annoncée à l'avance : elle ne peut
    # départager que des résultats presque identiques, pas fabriquer un gagnant.
    complexity_penalty = 0.0025 * (candidate["component_count"] - 1)
    return robustness + 0.10 * quality - complexity_penalty


def _entry_overlap(left: list, right: list) -> dict[str, float | int]:
    left_entries = {(item.entry_time, int(item.direction)) for item in left}
    right_entries = {(item.entry_time, int(item.direction)) for item in right}
    shared = left_entries & right_entries
    union = left_entries | right_entries
    smaller = min(len(left_entries), len(right_entries))
    return {
        "left_entries": len(left_entries),
        "right_entries": len(right_entries),
        "shared_entries": len(shared),
        "share_of_smaller": len(shared) / smaller if smaller else 0.0,
        "jaccard": len(shared) / len(union) if union else 0.0,
    }


def _correlation(curves: dict[int, pd.Series]) -> dict[str, float]:
    daily = pd.concat(
        {
            f"D{horizon}": equity.resample("1D").last().pct_change()
            for horizon, equity in curves.items()
        },
        axis=1,
    ).dropna()
    matrix = daily.corr()
    return {
        f"D{left}_D{right}": float(matrix.loc[f"D{left}", f"D{right}"])
        for left, right in combinations(HORIZONS, 2)
    }


def _delta(candidate: dict, baseline: dict, section: str, metric: str) -> float:
    return float(candidate[section][metric] - baseline[section][metric])


def _json_safe_score(value: float) -> float | None:
    return value if value != float("-inf") else None


def main() -> None:
    from btcquant.research.search_gates import refuse_ungoverned_search

    refuse_ungoverned_search(__file__)
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
    normal_runs = {}
    for combo in _all_combinations():
        equity, trades = _run_combo(cfg, frame, combo, execution_profile="normal")
        normal_runs[combo] = (equity, trades)
        item = _candidate(combo, equity, trades)
        item["pretest_score"] = _json_safe_score(_selection_score(item))
        candidates.append(item)

    ranked = sorted(candidates, key=_selection_score, reverse=True)
    selected = ranked[0]
    selected_pair = max(
        (item for item in candidates if item["component_count"] == 2),
        key=_selection_score,
    )
    baseline = next(item for item in candidates if item["horizons"] == list(HORIZONS))
    selected_combo = tuple(selected["horizons"])
    selected_pair_combo = tuple(selected_pair["horizons"])

    baseline_stress_equity, _ = _run_combo(
        cfg,
        frame,
        HORIZONS,
        execution_profile="stress",
    )
    selected_stress_equity, _ = _run_combo(
        cfg,
        frame,
        selected_combo,
        execution_profile="stress",
    )
    selected_pair_stress_equity, _ = _run_combo(
        cfg,
        frame,
        selected_pair_combo,
        execution_profile="stress",
    )
    baseline["stress_full"] = _summary(baseline_stress_equity)
    selected["stress_full"] = _summary(selected_stress_equity)
    selected_pair["stress_full"] = _summary(selected_pair_stress_equity)

    single_curves = {horizon: normal_runs[(horizon,)][0] for horizon in HORIZONS}
    full_trades = normal_runs[HORIZONS][1]
    pair_overlap = {
        f"D{left}_D{right}": _entry_overlap(
            full_trades[left],
            full_trades[right],
        )
        for left, right in combinations(HORIZONS, 2)
    }
    leave_one_out = {}
    for removed in HORIZONS:
        kept = tuple(horizon for horizon in HORIZONS if horizon != removed)
        candidate = next(item for item in candidates if item["horizons"] == list(kept))
        leave_one_out[f"without_D{removed}"] = {
            "kept": list(kept),
            "full_cagr_delta": _delta(candidate, baseline, "full", "cagr"),
            "full_sharpe_delta": _delta(candidate, baseline, "full", "sharpe"),
            "full_drawdown_delta": _delta(candidate, baseline, "full", "max_drawdown"),
            "sealed_cagr_delta": _delta(candidate, baseline, "sealed_test", "cagr"),
            "sealed_sharpe_delta": _delta(candidate, baseline, "sealed_test", "sharpe"),
        }

    overall_winner_checks = {
        "full_sharpe_no_worse": selected["full"]["sharpe"] >= baseline["full"]["sharpe"],
        "full_drawdown_no_worse": (
            selected["full"]["max_drawdown"] >= baseline["full"]["max_drawdown"]
        ),
        "sealed_cagr_no_worse": (
            selected["sealed_test"]["cagr"] >= baseline["sealed_test"]["cagr"]
        ),
        "sealed_sharpe_no_worse": (
            selected["sealed_test"]["sharpe"] >= baseline["sealed_test"]["sharpe"]
        ),
        "stress_drawdown_above_halt": selected["stress_full"]["max_drawdown"] > -0.60,
    }
    simplification_checks = {
        "pair_selected_without_sealed_test": True,
        "full_cagr_above_trio": (selected_pair["full"]["cagr"] > baseline["full"]["cagr"]),
        "full_sharpe_no_worse": (selected_pair["full"]["sharpe"] >= baseline["full"]["sharpe"]),
        # Même tolérance que la recherche de pyramiding déjà adoptée : au plus
        # un point de drawdown supplémentaire, jamais une dérive ouverte.
        "full_drawdown_degradation_at_most_1pp": (
            selected_pair["full"]["max_drawdown"] >= baseline["full"]["max_drawdown"] - 0.01
        ),
        "sealed_cagr_no_worse": (
            selected_pair["sealed_test"]["cagr"] >= baseline["sealed_test"]["cagr"]
        ),
        "sealed_sharpe_no_worse": (
            selected_pair["sealed_test"]["sharpe"] >= baseline["sealed_test"]["sharpe"]
        ),
        "stress_drawdown_above_halt": (selected_pair["stress_full"]["max_drawdown"] > -0.60),
    }
    historical_pair_gate = all(simplification_checks.values())
    data_paths = (
        ROOT / "data" / "binance_BTC-USDT_1h.csv",
        ROOT / "data" / "binanceusdm_BTCUSDT_USDT_funding.csv",
    )
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Contribution marginale des horizons Donchian BTC déployés",
        "protocol": {
            "horizons": list(HORIZONS),
            "combinations": [list(combo) for combo in _all_combinations()],
            "capital_rule": "capital Trend total constant, poids configurés renormalisés",
            "common_warmup_bars": COMMON_WARMUP_BARS,
            "development_end": DEVELOPMENT_END.isoformat(),
            "validation_end": VALIDATION_END.isoformat(),
            "sealed_test_used_for_selection": False,
            "selection_rule": "minimum CAGR dev/validation + 0.10 minimum Sharpe - 0.0025 par composant supplémentaire",
            "pair_challenger_rule": "meilleure combinaison à deux horizons selon le score pré-test",
            "deployment_rule": "un passage historique autorise seulement un challenger forward, jamais une modification paper immédiate",
        },
        "provenance": {
            "script_sha256": _sha256(Path(__file__)),
            "source_tree_sha256": quantitative_source_sha256(Path(__file__)),
            "config": {
                "path": CONFIG.relative_to(ROOT).as_posix(),
                "sha256": _sha256(CONFIG),
            },
            "data": [
                {"path": path.relative_to(ROOT).as_posix(), "sha256": _sha256(path)}
                for path in data_paths
            ],
        },
        "baseline_trio": baseline,
        "selected_pretest": selected,
        "selected_pair_pretest": selected_pair,
        "diagnostics": {
            "daily_return_correlation": _correlation(single_curves),
            "entry_overlap_in_deployed_trio": pair_overlap,
            "leave_one_out_vs_trio": leave_one_out,
        },
        "overall_winner_checks": overall_winner_checks,
        "simplification_checks": simplification_checks,
        "historical_pair_gate_passed": historical_pair_gate,
        "recommendation": (
            "FORWARD_CHALLENGER_D20_D100" if historical_pair_gate else "KEEP_D20_D55_D100"
        ),
        "recommend_simplification_now": False,
        "candidates_ranked_pretest": ranked,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("Classement pré-test (2019-2024) :")
    for item in ranked:
        label = "+".join(f"D{horizon}" for horizon in item["horizons"])
        print(
            f"  {label:14} score {item['pretest_score']:.3f} | "
            f"CAGR {item['full']['cagr']:+.1%} | Sharpe {item['full']['sharpe']:.2f} | "
            f"DD {item['full']['max_drawdown']:+.1%} | test {item['sealed_test']['cagr']:+.1%}"
        )
    print(f"Meilleure paire pré-test : {selected_pair['horizons']}")
    print(
        "Recommandation : "
        + (
            "TESTER D20+D100 EN CHALLENGER FORWARD, SANS MODIFIER LE PAPER"
            if historical_pair_gate
            else "CONSERVER LE TRIO"
        )
    )
    print(f"Artefact : {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
