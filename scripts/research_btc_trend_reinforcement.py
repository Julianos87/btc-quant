"""Teste une surcouche de renforcement après confirmation favorable du breakout."""

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

OUTPUT = ROOT / "audit" / "btc_trend_reinforcement_research.json"
HORIZONS = (20, 55, 100)
WEIGHTS = (0.3333, 0.3333, 0.3334)
CONFIRMATIONS_ATR = (0.5, 1.0, 1.5)
OVERLAY_WEIGHTS = (0.10, 0.20, 0.30)


def _ensemble(
    cfg: dict,
    frame: pd.DataFrame,
    *,
    capital: float,
    entry_buffer_atr: float,
    profile: str,
    pyramid_atr_step: float | None = None,
    pyramid_add_fraction: float = 0.30,
) -> tuple[pd.Series, int]:
    scenario = deepcopy(cfg)
    scenario["execution"]["simulation"]["profile"] = profile
    base_risk = risk_from_config(scenario)
    curves = []
    trades = 0
    for horizon, weight in zip(HORIZONS, WEIGHTS, strict=True):
        risk = RiskConfig(
            **{
                **base_risk.__dict__,
                "initial_capital": capital * weight,
            }
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
                adx_min=20,
                funding_long_max=0.0008,
                funding_short_min=-0.0008,
                entry_buffer_atr=entry_buffer_atr,
                pyramid_atr_step=pyramid_atr_step,
                pyramid_add_fraction=pyramid_add_fraction,
            ),
            frame,
        )
        curves.append(result.equity)
        trades += len(result.trades)
    return pd.concat(curves, axis=1).ffill().dropna().sum(axis=1), trades


def _candidate(
    equity: pd.Series,
    trades: int,
    confirmation_atr: float | None,
    overlay_weight: float,
) -> dict:
    return {
        "confirmation_atr": confirmation_atr,
        "overlay_weight": overlay_weight,
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


def _score(item: dict) -> float:
    development = item["development"]
    validation = item["validation"]
    if development["cagr"] <= 0 or validation["cagr"] <= 0:
        return float("-inf")
    return min(development["cagr"], validation["cagr"]) + 0.05 * min(
        development["sharpe"],
        validation["sharpe"],
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
    initial_capital = float(risk_from_config(cfg).initial_capital)
    baseline_equity, baseline_trades = _ensemble(
        cfg,
        frame,
        capital=initial_capital,
        entry_buffer_atr=0.0,
        profile="normal",
    )
    baseline = _candidate(baseline_equity, baseline_trades, None, 0.0)
    baseline["pretest_score"] = _score(baseline)
    candidates = [baseline]
    for confirmation, overlay_weight in product(
        CONFIRMATIONS_ATR,
        OVERLAY_WEIGHTS,
    ):
        overlay_capital = initial_capital * overlay_weight
        overlay_equity, overlay_trades = _ensemble(
            cfg,
            frame,
            capital=overlay_capital,
            entry_buffer_atr=confirmation,
            profile="normal",
        )
        aligned = overlay_equity.reindex(baseline_equity.index).ffill().fillna(overlay_capital)
        combined = baseline_equity + aligned - overlay_capital
        item = _candidate(
            combined,
            baseline_trades + overlay_trades,
            confirmation,
            overlay_weight,
        )
        item["pretest_score"] = _score(item)
        candidates.append(item)
    selected = max(candidates, key=_score)

    baseline_stress, _ = _ensemble(
        cfg,
        frame,
        capital=initial_capital,
        entry_buffer_atr=0.0,
        profile="stress",
    )
    if selected["overlay_weight"] == 0:
        selected_stress = baseline_stress
    else:
        overlay_capital = initial_capital * selected["overlay_weight"]
        overlay_stress, _ = _ensemble(
            cfg,
            frame,
            capital=overlay_capital,
            entry_buffer_atr=selected["confirmation_atr"],
            profile="stress",
        )
        aligned = overlay_stress.reindex(baseline_stress.index).ffill().fillna(overlay_capital)
        selected_stress = baseline_stress + aligned - overlay_capital
    selected["stress_full"] = _summary(selected_stress)
    checks = {
        "overlay_selected": selected["overlay_weight"] > 0,
        "full_cagr_above_baseline": (selected["full"]["cagr"] > baseline["full"]["cagr"]),
        "full_sharpe_no_worse": (selected["full"]["sharpe"] >= baseline["full"]["sharpe"]),
        "sealed_test_cagr_no_worse": (
            selected["sealed_test"]["cagr"] >= baseline["sealed_test"]["cagr"]
        ),
        "stress_drawdown_above_halt": (selected["stress_full"]["max_drawdown"] > -0.60),
    }
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Proxy de pyramiding BTC par surcouche de breakout confirmé",
        "protocol": {
            "candidate_count": len(candidates),
            "selection_uses_sealed_test": False,
            "confirmations_atr": list(CONFIRMATIONS_ATR),
            "overlay_weights": list(OVERLAY_WEIGHTS),
            "limitation": (
                "Surcouche indépendante: valide l'économie du renforcement avant "
                "d'implémenter des ajouts stateful dans les moteurs backtest/live."
            ),
        },
        "provenance": {
            "script_sha256": _sha256(Path(__file__)),
            "strategy_sha256": _sha256(ROOT / "src" / "btcquant" / "strategies" / "trend_ls.py"),
            "config_sha256": _sha256(CONFIG),
        },
        "baseline": baseline,
        "selected": selected,
        "adoption_checks": checks,
        "adopted": all(checks.values()),
        "candidates": sorted(candidates, key=_score, reverse=True),
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Renfort {selected['overlay_weight']:.0%} après "
        f"{selected['confirmation_atr']} ATR | CAGR {selected['full']['cagr']:+.1%} | "
        f"Sharpe {selected['full']['sharpe']:.2f} | "
        f"DD {selected['full']['max_drawdown']:+.1%} | "
        f"test {selected['sealed_test']['cagr']:+.1%}"
    )
    print(f"Adoption : {'OUI' if payload['adopted'] else 'NON'}")
    print(f"Artefact : {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
