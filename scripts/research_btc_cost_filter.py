"""Évalue isolément un filtre de cassure couvrant les coûts sur le profil BTC.

Le seuil est sélectionné sur 2019-2024. La période 2025+ reste scellée jusqu'à
la sélection finale et aucune configuration paper n'est modifiée.
"""

from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.backtest import BacktestEngine
from btcquant.backtest.metrics import compute_metrics
from btcquant.carry import add_funding_columns, load_funding
from btcquant.console import enable_utf8_output
from btcquant.config import execution_config_from_config, load_config, risk_from_config
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.domain import ExecutionSimulator
from btcquant.indicators import bars_per_year
from btcquant.risk import RiskConfig
from btcquant.strategies.trend_ls import TrendLS

enable_utf8_output()

CONFIG = ROOT / "environments" / "paper" / "config.yaml"
OUTPUT = ROOT / "audit" / "btc_cost_filter_research.json"
TIMEFRAME = "4h"
BPY = bars_per_year(TIMEFRAME)
HORIZONS = (20, 55, 100)
WEIGHTS = (0.3333, 0.3333, 0.3334)
THRESHOLDS_BPS = (0, 10, 20, 30, 40, 60)
DEVELOPMENT_END = pd.Timestamp("2022-12-31 23:59:59", tz="UTC")
VALIDATION_END = pd.Timestamp("2024-12-31 23:59:59", tz="UTC")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _summary(equity: pd.Series) -> dict[str, float]:
    metrics = compute_metrics(equity, [], BPY)
    return {key: float(metrics[key]) for key in ("cagr", "sharpe", "max_drawdown", "total_return")}


def _period(equity: pd.Series, start: pd.Timestamp | None, end: pd.Timestamp | None):
    selected = equity
    if start is not None:
        selected = selected[selected.index >= start]
    if end is not None:
        selected = selected[selected.index <= end]
    return _summary(selected)


def _run_candidate(
    cfg: dict,
    frame: pd.DataFrame,
    *,
    threshold_bps: int,
    profile: str,
) -> tuple[pd.Series, int]:
    profile_cfg = deepcopy(cfg)
    profile_cfg["execution"]["simulation"]["profile"] = profile
    base_risk = risk_from_config(profile_cfg)
    curves = []
    trades = 0
    for horizon, weight in zip(HORIZONS, WEIGHTS, strict=True):
        risk = RiskConfig(
            **{
                **base_risk.__dict__,
                "initial_capital": base_risk.initial_capital * weight,
            }
        )
        strategy = TrendLS(
            donchian=horizon,
            adx_min=20,
            funding_long_max=0.0008,
            funding_short_min=-0.0008,
            entry_buffer_bps=threshold_bps,
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


def _payload(equity: pd.Series, trades: int, threshold_bps: int) -> dict:
    return {
        "threshold_bps": threshold_bps,
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


def _selection_score(candidate: dict) -> float:
    development = candidate["development"]
    validation = candidate["validation"]
    if development["sharpe"] <= 0 or validation["sharpe"] <= 0:
        return float("-inf")
    return min(development["cagr"], validation["cagr"]) + 0.10 * min(
        development["sharpe"], validation["sharpe"]
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
    frame = resample(base, TIMEFRAME_TO_PANDAS[TIMEFRAME])
    frame = add_funding_columns(frame, funding, TIMEFRAME_TO_PANDAS[TIMEFRAME])

    candidates = []
    for threshold in THRESHOLDS_BPS:
        equity, trades = _run_candidate(
            cfg,
            frame,
            threshold_bps=threshold,
            profile="normal",
        )
        candidate = _payload(equity, trades, threshold)
        candidate["selection_score"] = _selection_score(candidate)
        candidates.append(candidate)
    candidates.sort(key=lambda item: item["selection_score"], reverse=True)
    selected = candidates[0]
    baseline = next(item for item in candidates if item["threshold_bps"] == 0)

    stress_equity, stress_trades = _run_candidate(
        cfg,
        frame,
        threshold_bps=int(selected["threshold_bps"]),
        profile="stress",
    )
    selected["stress_full"] = {
        **_summary(stress_equity),
        "trades": stress_trades,
    }

    adoption_checks = {
        "non_zero_filter_selected": selected["threshold_bps"] > 0,
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
    output = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Test isolé d'un filtre de cassure couvrant les coûts",
        "protocol": {
            "thresholds_bps": list(THRESHOLDS_BPS),
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
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Seuil sélectionné sans consulter 2025+ : {selected['threshold_bps']} bps")
    for label in ("development", "validation", "sealed_test", "full", "stress_full"):
        metrics = selected[label]
        print(
            f"{label:12} CAGR {metrics['cagr']:+.1%} | Sharpe {metrics['sharpe']:.2f} "
            f"| DD {metrics['max_drawdown']:+.1%}"
        )
    print(
        f"Trades : {baseline['trades']} baseline → {selected['trades']} filtrés | "
        f"adoption {'OUI' if adopted else 'NON'}"
    )
    print(f"Artefact : {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
