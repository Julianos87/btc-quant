"""Recherche orientée rendement sur la stratégie BTC existante."""

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
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.domain import ExecutionSimulator
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

OUTPUT = ROOT / "audit" / "btc_return_boost_research.json"
STRUCTURES = {
    "deployed": ((20, 55, 100), (0.3333, 0.3333, 0.3334), 3.0),
    "fast": ((10, 20, 40), (0.50, 0.30, 0.20), 3.5),
}
RISK_SCALES = (1.0, 1.125, 1.25, 1.50)
STRONG_TRENDS = (
    (None, None),
    (25.0, 3.5),
    (25.0, 4.0),
    (25.0, 4.5),
    (30.0, 3.5),
    (30.0, 4.0),
    (30.0, 4.5),
    (35.0, 3.5),
    (35.0, 4.0),
    (35.0, 4.5),
)


def _scaled_risk(base: RiskConfig, initial_capital: float, scale: float) -> RiskConfig:
    return RiskConfig(
        **{
            **base.__dict__,
            "initial_capital": initial_capital,
            "risk_per_trade": base.risk_per_trade * scale,
            "vol_target_annual": (
                base.vol_target_annual * scale
                if base.vol_target_annual is not None
                else None
            ),
            "max_leverage": base.max_leverage * scale,
        }
    )


def _run(
    cfg: dict,
    frame: pd.DataFrame,
    *,
    structure_name: str,
    risk_scale: float,
    strong_adx: float | None,
    strong_atr: float | None,
    profile: str = "normal",
) -> tuple[pd.Series, int]:
    scenario = deepcopy(cfg)
    scenario["execution"]["simulation"]["profile"] = profile
    base_risk = risk_from_config(scenario)
    horizons, weights, atr_mult = STRUCTURES[structure_name]
    curves = []
    trades = 0
    for horizon, weight in zip(horizons, weights, strict=True):
        risk = _scaled_risk(
            base_risk,
            base_risk.initial_capital * weight,
            risk_scale,
        )
        result = BacktestEngine(
            risk=risk,
            funding_rate_8h=float(scenario["costs"]["funding_rate_8h"]),
            allow_short=True,
            execution_simulator=ExecutionSimulator(
                execution_config_from_config(
                    scenario,
                    float(scenario["costs"]["perp_fee_rate"]),
                )
            ),
        ).run(
            TrendLS(
                donchian=horizon,
                atr_mult=atr_mult,
                adx_min=20,
                funding_long_max=0.0008,
                funding_short_min=-0.0008,
                strong_trend_adx=strong_adx,
                strong_trend_atr_mult=strong_atr,
            ),
            frame,
        )
        curves.append(result.equity)
        trades += len(result.trades)
    return pd.concat(curves, axis=1).ffill().dropna().sum(axis=1), trades


def _candidate(
    equity: pd.Series,
    trades: int,
    structure: str,
    risk_scale: float,
    strong_adx: float | None,
    strong_atr: float | None,
) -> dict:
    return {
        "structure": structure,
        "risk_scale": risk_scale,
        "strong_trend_adx": strong_adx,
        "strong_trend_atr_mult": strong_atr,
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


def _pretest_score(candidate: dict) -> float:
    development = candidate["development"]
    validation = candidate["validation"]
    if development["cagr"] <= 0 or validation["cagr"] <= 0:
        return float("-inf")
    if development["max_drawdown"] < -0.60 or validation["max_drawdown"] < -0.60:
        return float("-inf")
    return min(development["cagr"], validation["cagr"]) + 0.05 * min(
        development["sharpe"],
        validation["sharpe"],
    )


def _winner(candidates: list[dict], predicate) -> dict:
    return max(
        (item for item in candidates if predicate(item)),
        key=_pretest_score,
    )


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
    for structure, risk_scale, (strong_adx, strong_atr) in product(
        STRUCTURES,
        RISK_SCALES,
        STRONG_TRENDS,
    ):
        equity, trades = _run(
            cfg,
            frame,
            structure_name=structure,
            risk_scale=risk_scale,
            strong_adx=strong_adx,
            strong_atr=strong_atr,
        )
        item = _candidate(
            equity,
            trades,
            structure,
            risk_scale,
            strong_adx,
            strong_atr,
        )
        item["pretest_score"] = _pretest_score(item)
        candidates.append(item)

    baseline = next(
        item
        for item in candidates
        if item["structure"] == "deployed"
        and item["risk_scale"] == 1.0
        and item["strong_trend_adx"] is None
    )
    winners = {
        "risk_only": _winner(
            candidates,
            lambda item: (
                item["structure"] == "deployed"
                and item["strong_trend_adx"] is None
            ),
        ),
        "adaptive_trailing_only": _winner(
            candidates,
            lambda item: (
                item["structure"] == "deployed"
                and item["risk_scale"] == 1.0
            ),
        ),
        "fast_structure_only": _winner(
            candidates,
            lambda item: (
                item["structure"] == "fast"
                and item["risk_scale"] == 1.0
                and item["strong_trend_adx"] is None
            ),
        ),
        "combined": max(candidates, key=_pretest_score),
    }
    selected = winners["combined"]
    stress_equity, _ = _run(
        cfg,
        frame,
        structure_name=selected["structure"],
        risk_scale=selected["risk_scale"],
        strong_adx=selected["strong_trend_adx"],
        strong_atr=selected["strong_trend_atr_mult"],
        profile="stress",
    )
    selected["stress_full"] = _summary(stress_equity)
    adoption_checks = {
        "full_cagr_gain_at_least_5pp": (
            selected["full"]["cagr"] >= baseline["full"]["cagr"] + 0.05
        ),
        "full_sharpe_no_worse": (
            selected["full"]["sharpe"] >= baseline["full"]["sharpe"]
        ),
        "sealed_test_cagr_no_worse": (
            selected["sealed_test"]["cagr"] >= baseline["sealed_test"]["cagr"]
        ),
        "sealed_test_drawdown_no_worse": (
            selected["sealed_test"]["max_drawdown"]
            >= baseline["sealed_test"]["max_drawdown"]
        ),
        "stress_drawdown_above_halt": (
            selected["stress_full"]["max_drawdown"] > -0.60
        ),
    }
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Recherche BTC orientée hausse du rendement de la stratégie existante",
        "protocol": {
            "candidate_count": len(candidates),
            "selection_uses_sealed_test": False,
            "development_end": DEVELOPMENT_END.isoformat(),
            "validation_end": VALIDATION_END.isoformat(),
            "tested_levers": [
                "risque, cible de volatilité et plafond de levier mis à l'échelle ensemble",
                "stop suiveur élargi uniquement lorsque l'ADX confirme une tendance forte",
                "ensemble Donchian plus rapide",
                "combinaisons des trois",
            ],
        },
        "provenance": {
            "script_sha256": _sha256(Path(__file__)),
            "strategy_sha256": _sha256(
                ROOT / "src" / "btcquant" / "strategies" / "trend_ls.py"
            ),
            "config_sha256": _sha256(CONFIG),
        },
        "baseline": baseline,
        "category_winners": winners,
        "selected_pretest": selected,
        "adoption_checks": adoption_checks,
        "adopted": all(adoption_checks.values()),
        "top_candidates_pretest": sorted(
            candidates,
            key=_pretest_score,
            reverse=True,
        )[:15],
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("Témoin :", baseline["full"])
    for name, item in winners.items():
        print(
            f"{name:24} {item['structure']} risk×{item['risk_scale']:.3g} "
            f"ADX={item['strong_trend_adx']}/{item['strong_trend_atr_mult']} | "
            f"CAGR {item['full']['cagr']:+.1%} | Sharpe {item['full']['sharpe']:.2f} | "
            f"DD {item['full']['max_drawdown']:+.1%} | "
            f"test {item['sealed_test']['cagr']:+.1%}"
        )
    print(f"Adoption : {'OUI' if payload['adopted'] else 'NON'}")
    print(f"Artefact : {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
