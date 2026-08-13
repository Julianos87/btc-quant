"""Teste isolément un sizing continu selon le funding BTC."""

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
from btcquant.risk import RiskConfig
from btcquant.strategies.trend_ls import TrendLS
from scripts.research_btc_cost_filter import (
    CONFIG,
    DEVELOPMENT_END,
    HORIZONS,
    VALIDATION_END,
    WEIGHTS,
    _period,
    _sha256,
    _summary,
)

enable_utf8_output()

OUTPUT = ROOT / "audit" / "btc_funding_sizing_research.json"
FUNDING_THRESHOLDS = (0.0004, 0.0008, 0.0012)
FUNDING_FLOORS = (0.25, 0.50, 0.75)


def _run(
    cfg: dict,
    frame: pd.DataFrame,
    *,
    threshold: float | None,
    floor: float,
    profile: str,
):
    profile_cfg = deepcopy(cfg)
    profile_cfg["execution"]["simulation"]["profile"] = profile
    base_risk = risk_from_config(profile_cfg)
    curves = []
    trades = 0
    for horizon, weight in zip(HORIZONS, WEIGHTS, strict=True):
        risk = RiskConfig(
            **{**base_risk.__dict__, "initial_capital": base_risk.initial_capital * weight}
        )
        strategy = TrendLS(
            donchian=horizon,
            adx_min=20,
            funding_long_max=0.0008,
            funding_short_min=-0.0008,
            funding_sizing_threshold=threshold,
            funding_sizing_floor=floor,
        )
        fee_rate = float(profile_cfg["costs"]["perp_fee_rate"])
        result = BacktestEngine(
            risk=risk,
            funding_rate_8h=float(profile_cfg["costs"]["funding_rate_8h"]),
            allow_short=True,
            execution_simulator=ExecutionSimulator(
                execution_config_from_config(profile_cfg, fee_rate)
            ),
        ).run(strategy, frame)
        curves.append(result.equity)
        trades += len(result.trades)
    return pd.concat(curves, axis=1, sort=True).ffill().dropna().sum(axis=1), trades


def _candidate(
    equity: pd.Series,
    trades: int,
    threshold: float | None,
    floor: float,
) -> dict:
    return {
        "funding_sizing_threshold": threshold,
        "funding_sizing_floor": floor,
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
    parameter_sets: list[tuple[float | None, float]] = [(None, 1.0)]
    parameter_sets.extend(product(FUNDING_THRESHOLDS, FUNDING_FLOORS))
    candidates = []
    for threshold, floor in parameter_sets:
        equity, trades = _run(
            cfg,
            frame,
            threshold=threshold,
            floor=floor,
            profile="normal",
        )
        item = _candidate(equity, trades, threshold, floor)
        item["selection_score"] = _score(item)
        candidates.append(item)
    candidates.sort(key=lambda item: item["selection_score"], reverse=True)
    selected = candidates[0]
    baseline = next(item for item in candidates if item["funding_sizing_threshold"] is None)
    stress_equity, stress_trades = _run(
        cfg,
        frame,
        threshold=selected["funding_sizing_threshold"],
        floor=float(selected["funding_sizing_floor"]),
        profile="stress",
    )
    selected["stress_full"] = {**_summary(stress_equity), "trades": stress_trades}
    adoption_checks = {
        "continuous_sizing_selected": selected["funding_sizing_threshold"] is not None,
        "full_cagr_above_baseline": selected["full"]["cagr"] > baseline["full"]["cagr"],
        "full_sharpe_above_baseline": selected["full"]["sharpe"] > baseline["full"]["sharpe"],
        "full_drawdown_no_worse": selected["full"]["max_drawdown"]
        >= baseline["full"]["max_drawdown"],
        "sealed_test_cagr_no_worse": selected["sealed_test"]["cagr"]
        >= baseline["sealed_test"]["cagr"],
        "sealed_test_sharpe_no_worse": selected["sealed_test"]["sharpe"]
        >= baseline["sealed_test"]["sharpe"],
    }
    adopted = all(adoption_checks.values())
    data_paths = (
        ROOT / "data" / "binance_BTC-USDT_1h.csv",
        ROOT / "data" / "binanceusdm_BTCUSDT_USDT_funding.csv",
    )
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Test isolé du sizing continu basé sur le funding BTC",
        "protocol": {
            "thresholds": list(FUNDING_THRESHOLDS),
            "floors": list(FUNDING_FLOORS),
            "development_end": DEVELOPMENT_END.isoformat(),
            "validation_end": VALIDATION_END.isoformat(),
            "sealed_test_used_for_selection": False,
        },
        "provenance": {
            "script_sha256": _sha256(Path(__file__)),
            "config": {"path": CONFIG.relative_to(ROOT).as_posix(), "sha256": _sha256(CONFIG)},
            "data": [
                {"path": path.relative_to(ROOT).as_posix(), "sha256": _sha256(path)}
                for path in data_paths
            ],
        },
        "baseline": baseline,
        "selected": selected,
        "adoption_checks": adoption_checks,
        "adopted": adopted,
        "candidates_ranked_pre_test": candidates,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "Sizing sélectionné : "
        f"seuil={selected['funding_sizing_threshold']}, "
        f"plancher={selected['funding_sizing_floor']}"
    )
    for label in ("development", "validation", "sealed_test", "full", "stress_full"):
        metrics = selected[label]
        print(
            f"{label:12} CAGR {metrics['cagr']:+.1%} | Sharpe {metrics['sharpe']:.2f} "
            f"| DD {metrics['max_drawdown']:+.1%}"
        )
    print(f"Adoption : {'OUI' if adopted else 'NON'}")
    print(f"Artefact : {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
