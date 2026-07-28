"""Mesure de sensibilité aux frais/slippage, sans simuler de faux carnet maker."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import UTC, datetime
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

OUTPUT = ROOT / "audit" / "btc_execution_costs_research.json"
SCENARIOS = {
    "current": {"fee_rate": 0.0005, "slippage_bps": 5.0},
    "half_slippage": {"fee_rate": 0.0005, "slippage_bps": 2.5},
    "maker_proxy": {"fee_rate": 0.0002, "slippage_bps": 1.0},
    "dynamic_costs_only": {"fee_rate": 0.0, "slippage_bps": 0.0},
}


def _run(cfg: dict, frame: pd.DataFrame, costs: dict):
    scenario_cfg = deepcopy(cfg)
    scenario_cfg["costs"]["slippage_bps"] = costs["slippage_bps"]
    base_risk = risk_from_config(scenario_cfg)
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
        )
        result = BacktestEngine(
            risk=risk,
            funding_rate_8h=float(scenario_cfg["costs"]["funding_rate_8h"]),
            allow_short=True,
            execution_simulator=ExecutionSimulator(
                execution_config_from_config(scenario_cfg, float(costs["fee_rate"]))
            ),
        ).run(strategy, frame)
        curves.append(result.equity)
        trades += len(result.trades)
    return pd.concat(curves, axis=1, sort=True).ffill().dropna().sum(axis=1), trades


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
    results = {}
    for name, costs in SCENARIOS.items():
        equity, trades = _run(cfg, frame, costs)
        results[name] = {
            "assumptions": costs,
            "trades": trades,
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
    current = results["current"]["full"]
    maker = results["maker_proxy"]["full"]
    infrastructure_worth_testing = (
        maker["cagr"] - current["cagr"] >= 0.02
        and maker["sharpe"] - current["sharpe"] >= 0.03
    )
    data_paths = (
        ROOT / "data" / "binance_BTC-USDT_1h.csv",
        ROOT / "data" / "binanceusdm_BTCUSDT_USDT_funding.csv",
    )
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Sensibilité BTC aux coûts; aucun fill maker n'est prétendu",
        "methodology": {
            "scenarios_are_hypothetical": True,
            "requires_orderbook_or_live_shadow_validation": True,
            "infrastructure_worth_testing": infrastructure_worth_testing,
        },
        "provenance": {
            "script_sha256": _sha256(Path(__file__)),
            "config": {"path": CONFIG.relative_to(ROOT).as_posix(), "sha256": _sha256(CONFIG)},
            "data": [
                {"path": path.relative_to(ROOT).as_posix(), "sha256": _sha256(path)}
                for path in data_paths
            ],
        },
        "results": results,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, result in results.items():
        metrics = result["full"]
        print(
            f"{name:18} CAGR {metrics['cagr']:+.1%} | Sharpe {metrics['sharpe']:.2f} "
            f"| DD {metrics['max_drawdown']:+.1%}"
        )
    print(f"Chantier maker à valider en shadow : {infrastructure_worth_testing}")
    print(f"Artefact : {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
