"""Teste une petite poche BTC de retour à la moyenne en régime latéral."""

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
from btcquant.config import execution_config_from_config, load_config, risk_from_config
from btcquant.console import enable_utf8_output
from btcquant.domain import ExecutionSimulator
from btcquant.research.strategies import RangeMeanReversion
from btcquant.risk import RiskConfig
from scripts.research_btc_cost_filter import (
    CONFIG,
    DEVELOPMENT_END,
    VALIDATION_END,
    _period,
    _sha256,
    _summary,
)
from scripts.research_btc_funding_sizing import _run as run_trend

enable_utf8_output()

OUTPUT = ROOT / "audit" / "btc_mean_reversion_research.json"
LOOKBACKS = (20, 40)
Z_ENTRIES = (1.5, 2.0, 2.5)
ADX_MAX_VALUES = (15.0, 20.0)
ATR_MULTIPLIERS = (2.0, 3.0)
MAX_EMA_GAPS_ATR = (0.5, 1.0)
MAX_ANNUAL_VOLS = (0.8, 1.2)
SLEEVE_WEIGHTS = (0.05, 0.10, 0.15, 0.20)


def _run_mean_reversion(cfg: dict, frame: pd.DataFrame, params: dict, profile: str):
    profile_cfg = deepcopy(cfg)
    profile_cfg["execution"]["simulation"]["profile"] = profile
    base_risk = risk_from_config(profile_cfg)
    risk = RiskConfig(**{**base_risk.__dict__, "initial_capital": base_risk.initial_capital})
    fee_rate = float(profile_cfg["costs"]["perp_fee_rate"])
    result = BacktestEngine(
        risk=risk,
        funding_rate_8h=float(profile_cfg["costs"]["funding_rate_8h"]),
        allow_short=True,
        execution_simulator=ExecutionSimulator(execution_config_from_config(profile_cfg, fee_rate)),
    ).run(RangeMeanReversion(**params), frame)
    return result.equity, len(result.trades)


def _candidate(equity: pd.Series, params: dict | None, weight: float, trades: int):
    return {
        "params": params,
        "mean_reversion_weight": weight,
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


def _combine_equity(
    trend_equity: pd.Series,
    mean_reversion_equity: pd.Series,
    weight: float,
) -> pd.Series:
    """Aligne les manches; la poche non encore échauffée reste en cash."""

    initial_capital = float(trend_equity.iloc[0])
    aligned_mean_reversion = (
        mean_reversion_equity.reindex(trend_equity.index).ffill().fillna(initial_capital)
    )
    return (1.0 - weight) * trend_equity + weight * aligned_mean_reversion


def main() -> None:
    from btcquant.research.search_gates import refuse_ungoverned_search

    refuse_ungoverned_search(__file__)
    cfg = load_config(CONFIG)
    from btcquant.carry import add_funding_columns, load_funding
    from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample

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
    trend_equity, trend_trades = run_trend(
        cfg,
        frame,
        threshold=None,
        floor=1.0,
        profile="normal",
    )
    baseline = _candidate(trend_equity, None, 0.0, trend_trades)
    baseline["selection_score"] = _score(baseline)
    candidates = [baseline]
    parameter_grid = product(
        LOOKBACKS,
        Z_ENTRIES,
        ADX_MAX_VALUES,
        ATR_MULTIPLIERS,
        MAX_EMA_GAPS_ATR,
        MAX_ANNUAL_VOLS,
    )
    for (
        lookback,
        z_entry,
        adx_max,
        atr_mult,
        max_ema_gap_atr,
        max_annual_vol,
    ) in parameter_grid:
        params = {
            "lookback": lookback,
            "z_entry": z_entry,
            "adx_max": adx_max,
            "atr_mult": atr_mult,
            "max_ema_gap_atr": max_ema_gap_atr,
            "exit_ema_gap_atr": 1.5 * max_ema_gap_atr,
            "max_annual_vol": max_annual_vol,
        }
        mr_equity, mr_trades = _run_mean_reversion(cfg, frame, params, "normal")
        for weight in SLEEVE_WEIGHTS:
            combined = _combine_equity(trend_equity, mr_equity, weight)
            item = _candidate(
                combined,
                params,
                weight,
                trend_trades + mr_trades,
            )
            item["selection_score"] = _score(item)
            candidates.append(item)
    candidates.sort(key=lambda item: item["selection_score"], reverse=True)
    selected = candidates[0]

    trend_stress, _ = run_trend(
        cfg,
        frame,
        threshold=None,
        floor=1.0,
        profile="stress",
    )
    if selected["params"] is None:
        selected_stress = trend_stress
    else:
        mr_stress, _ = _run_mean_reversion(cfg, frame, selected["params"], "stress")
        weight = float(selected["mean_reversion_weight"])
        selected_stress = _combine_equity(trend_stress, mr_stress, weight)
    selected["stress_full"] = _summary(selected_stress)
    adoption_checks = {
        "mean_reversion_selected": selected["mean_reversion_weight"] > 0,
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
        "purpose": "Test isolé d'une poche BTC de retour à la moyenne",
        "protocol": {
            "candidate_count": len(candidates),
            "sleeve_weights": list(SLEEVE_WEIGHTS),
            "entry_regime_filters": [
                "ADX faible",
                "ecart EMA50/EMA200 borne en multiples d'ATR",
                "volatilite realisee annualisee plafonnee",
            ],
            "immediate_regime_exits": [
                "ADX >= 25",
                "ecart EMA au-dessus du seuil de sortie",
                "volatilite au-dessus du plafond d'entree",
            ],
            "development_end": DEVELOPMENT_END.isoformat(),
            "validation_end": VALIDATION_END.isoformat(),
            "sealed_test_used_for_selection": False,
        },
        "provenance": {
            "script_sha256": _sha256(Path(__file__)),
            "strategy": {
                "path": "src/btcquant/research/strategies/range_mean_reversion.py",
                "sha256": _sha256(
                    ROOT
                    / "src"
                    / "btcquant"
                    / "research"
                    / "strategies"
                    / "range_mean_reversion.py"
                ),
            },
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
        "top_candidates_pre_test": candidates[:10],
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Poche sélectionnée : {selected['mean_reversion_weight']:.0%}, "
        f"paramètres={selected['params']}"
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
