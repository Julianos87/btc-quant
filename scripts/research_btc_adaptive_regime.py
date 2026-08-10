"""Teste un gouverneur causal de risque sur le profil BTC actuellement deploye.

Les profils sont classes sur 2019-2024. La periode 2025+ est calculee et
publiee seulement apres la selection. Le script ne modifie aucune configuration.
"""

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
from btcquant.provenance import quantitative_source_sha256
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

OUTPUT = ROOT / "audit" / "btc_adaptive_regime_research.json"
COMMON_WARMUP_BARS = 620
PROFILES = (
    {
        "name": "disabled",
        "enabled": False,
        "minimum_multiplier": 1.0,
        "volatility_shock_ratio": 2.0,
    },
    {
        "name": "mild",
        "enabled": True,
        "minimum_multiplier": 0.75,
        "volatility_shock_ratio": 2.5,
    },
    {
        "name": "balanced",
        "enabled": True,
        "minimum_multiplier": 0.50,
        "volatility_shock_ratio": 2.0,
    },
    {
        "name": "defensive",
        "enabled": True,
        "minimum_multiplier": 0.25,
        "volatility_shock_ratio": 1.5,
    },
)


def _strategy(horizon: int, profile: dict) -> TrendLS:
    return TrendLS(
        donchian=horizon,
        adx_min=20,
        funding_long_max=0.0008,
        funding_short_min=-0.0008,
        pyramid_atr_step=0.5,
        pyramid_add_fraction=0.30,
        pyramid_max_adds=1,
        adaptive_regime_enabled=bool(profile["enabled"]),
        adaptive_efficiency_bars=30,
        adaptive_volatility_bars=30,
        adaptive_reference_bars=540,
        adaptive_smoothing_span=12,
        adaptive_min_multiplier=float(profile["minimum_multiplier"]),
        adaptive_max_multiplier=1.0,
        adaptive_volatility_shock_ratio=float(profile["volatility_shock_ratio"]),
    )


def _run(
    cfg: dict,
    frame: pd.DataFrame,
    profile: dict,
    *,
    execution_profile: str,
) -> tuple[pd.Series, int]:
    profile_cfg = deepcopy(cfg)
    profile_cfg["execution"]["simulation"]["profile"] = execution_profile
    base_risk = risk_from_config(profile_cfg)
    no_trade_before = frame.index[COMMON_WARMUP_BARS]
    curves = []
    trades = 0
    for horizon, weight in zip(HORIZONS, WEIGHTS, strict=True):
        risk = RiskConfig(
            **{
                **base_risk.__dict__,
                "initial_capital": base_risk.initial_capital * weight,
            }
        )
        fee_rate = float(profile_cfg["costs"]["perp_fee_rate"])
        result = BacktestEngine(
            risk=risk,
            funding_rate_8h=float(profile_cfg["costs"]["funding_rate_8h"]),
            allow_short=True,
            execution_simulator=ExecutionSimulator(
                execution_config_from_config(profile_cfg, fee_rate)
            ),
        ).run(_strategy(horizon, profile), frame, no_trade_before=no_trade_before)
        curves.append(result.equity[result.equity.index >= no_trade_before])
        trades += len(result.trades)
    equity = pd.concat(curves, axis=1, sort=True).ffill().dropna().sum(axis=1)
    return equity, trades


def _candidate(equity: pd.Series, trades: int, profile: dict) -> dict:
    return {
        "profile": profile,
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


def _score(candidate: dict) -> float:
    development = candidate["development"]
    validation = candidate["validation"]
    if (
        development["sharpe"] <= 0
        or validation["sharpe"] <= 0
        or development["max_drawdown"] < -0.60
        or validation["max_drawdown"] < -0.60
    ):
        return float("-inf")
    return min(development["cagr"], validation["cagr"]) + 0.10 * min(
        development["sharpe"], validation["sharpe"]
    )


def _regime_diagnostics(frame: pd.DataFrame, profile: dict) -> dict:
    if not profile["enabled"]:
        return {"enabled": False}
    prepared = _strategy(55, profile).prepare(frame).iloc[COMMON_WARMUP_BARS:]
    multiplier = prepared["adaptive_risk_multiplier"].dropna()
    counts = prepared.loc[multiplier.index, "adaptive_regime"].value_counts(normalize=True)
    return {
        "enabled": True,
        "mean_multiplier": float(multiplier.mean()),
        "p10_multiplier": float(multiplier.quantile(0.10)),
        "p90_multiplier": float(multiplier.quantile(0.90)),
        "regime_fractions": {str(key): float(value) for key, value in counts.items()},
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
    frame = resample(base, TIMEFRAME_TO_PANDAS["4h"])
    frame = add_funding_columns(frame, funding, TIMEFRAME_TO_PANDAS["4h"])

    candidates = []
    for profile in PROFILES:
        equity, trades = _run(cfg, frame, profile, execution_profile="normal")
        item = _candidate(equity, trades, profile)
        item["selection_score"] = _score(item)
        candidates.append(item)
    candidates.sort(key=lambda item: item["selection_score"], reverse=True)
    selected = candidates[0]
    baseline = next(item for item in candidates if item["profile"]["name"] == "disabled")
    stress_equity, stress_trades = _run(
        cfg,
        frame,
        selected["profile"],
        execution_profile="stress",
    )
    selected["stress_full"] = {**_summary(stress_equity), "trades": stress_trades}
    selected["regime_diagnostics"] = _regime_diagnostics(frame, selected["profile"])

    checks = {
        "adaptive_profile_selected": bool(selected["profile"]["enabled"]),
        "full_cagr_above_baseline": selected["full"]["cagr"] > baseline["full"]["cagr"],
        "full_sharpe_no_worse": selected["full"]["sharpe"] >= baseline["full"]["sharpe"],
        "full_drawdown_no_worse": (
            selected["full"]["max_drawdown"] >= baseline["full"]["max_drawdown"]
        ),
        "sealed_cagr_no_worse": (
            selected["sealed_test"]["cagr"] >= baseline["sealed_test"]["cagr"]
        ),
        "sealed_drawdown_no_worse": (
            selected["sealed_test"]["max_drawdown"] >= baseline["sealed_test"]["max_drawdown"]
        ),
        "stress_drawdown_above_halt": selected["stress_full"]["max_drawdown"] > -0.60,
    }
    adopted = all(checks.values())
    configured_specs = [
        spec
        for spec in cfg["strategies"].values()
        if spec.get("enabled") and spec.get("type") == "trend_ls"
    ]
    configured_adaptive = bool(configured_specs) and all(
        spec.get("params", {}).get("adaptive_regime_enabled") is True for spec in configured_specs
    )
    operator_override = configured_adaptive and not adopted
    data_paths = (
        ROOT / "data" / "binance_BTC-USDT_1h.csv",
        ROOT / "data" / "binanceusdm_BTCUSDT_USDT_funding.csv",
    )
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Gouverneur causal et auto-calibre de risque BTC",
        "protocol": {
            "profiles": list(PROFILES),
            "common_warmup_bars": COMMON_WARMUP_BARS,
            "development_end": DEVELOPMENT_END.isoformat(),
            "validation_end": VALIDATION_END.isoformat(),
            "sealed_test_used_for_selection": False,
            "risk_increase_above_current_profile_allowed": False,
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
        "baseline": baseline,
        "selected": selected,
        "adoption_checks": checks,
        "adopted": adopted,
        "configured_adaptive": configured_adaptive,
        "operator_override": operator_override,
        "activation_status": (
            "OPERATOR_OVERRIDE_PAPER"
            if operator_override
            else ("RESEARCH_ONLY" if not adopted else "FORWARD_CANDIDATE")
        ),
        "activation_warning": (
            "Le profil equilibre a ete active explicitement par l'operateur "
            "malgre la preference pre-test pour le temoin."
            if operator_override
            else None
        ),
        "candidates_ranked_pre_test": candidates,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Profil sélectionné sans consulter 2025+ : {selected['profile']['name']}")
    for label in ("development", "validation", "sealed_test", "full", "stress_full"):
        metrics = selected[label]
        print(
            f"{label:12} CAGR {metrics['cagr']:+.1%} | Sharpe {metrics['sharpe']:.2f} "
            f"| DD {metrics['max_drawdown']:+.1%}"
        )
    print(f"Adoption immédiate : {'OUI' if adopted else 'NON'}")
    print(f"Artefact : {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
