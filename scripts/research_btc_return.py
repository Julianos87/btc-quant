"""Recherche BTC-only orientée CAGR avec sélection hors échantillon.

Le test final (2025+) n'intervient jamais dans le classement des candidats.
Ce script est un outil de recherche : il ne modifie aucune configuration.
"""

from __future__ import annotations

import hashlib
import itertools
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
from btcquant.config import execution_config_from_config, load_config, risk_from_config
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.domain import ExecutionSimulator
from btcquant.indicators import bars_per_year
from btcquant.risk import RiskConfig
from btcquant.strategies.trend_ls import TrendLS

CONFIG = ROOT / "environments" / "paper" / "config.yaml"
OUTPUT = ROOT / "audit" / "btc_return_research.json"
TIMEFRAME = "4h"
BPY = bars_per_year(TIMEFRAME)
HORIZON_SETS = (
    (10, 20, 40),
    (20, 40, 75),
    (20, 55, 100),
    (20, 75, 150),
    (40, 75, 150),
    (55, 100, 150),
)
WEIGHT_SETS = {
    "equal": (1 / 3, 1 / 3, 1 / 3),
    "fast_heavy": (0.50, 0.30, 0.20),
    "slow_heavy": (0.20, 0.30, 0.50),
}
ATR_MULTIPLIERS = (2.5, 3.0, 3.5)
SHORT_MULTIPLIERS = (0.75, 1.0)
DEVELOPMENT_END = pd.Timestamp("2022-12-31 23:59:59", tz="UTC")
VALIDATION_END = pd.Timestamp("2024-12-31 23:59:59", tz="UTC")
BASELINE = {
    "horizons": (20, 55, 100),
    "weights": (0.3333, 0.3333, 0.3334),
    "atr_mult": 3.0,
    "short_mult": 1.0,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _summary(equity: pd.Series) -> dict[str, float]:
    metrics = compute_metrics(equity, [], BPY)
    return {key: float(metrics[key]) for key in ("cagr", "sharpe", "max_drawdown", "total_return")}


def _slice(equity: pd.Series, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.Series:
    selected = equity
    if start is not None:
        selected = selected[selected.index >= start]
    if end is not None:
        selected = selected[selected.index <= end]
    return selected


def _atomic_curve(
    cfg: dict,
    frame: pd.DataFrame,
    *,
    horizon: int,
    atr_mult: float,
    short_mult: float,
    profile: str,
) -> pd.Series:
    profile_cfg = deepcopy(cfg)
    profile_cfg["execution"]["simulation"]["profile"] = profile
    base_risk = risk_from_config(profile_cfg)
    unit_risk = RiskConfig(**{**base_risk.__dict__, "initial_capital": 1.0})
    strategy = TrendLS(
        donchian=horizon,
        atr_mult=atr_mult,
        adx_min=20,
        funding_long_max=0.0008,
        funding_short_min=-0.0008,
    )
    fee_rate = float(profile_cfg["costs"]["perp_fee_rate"])
    return (
        BacktestEngine(
            risk=unit_risk,
            funding_rate_8h=float(profile_cfg["costs"]["funding_rate_8h"]),
            allow_short=True,
            short_size_mult=short_mult,
            execution_simulator=ExecutionSimulator(
                execution_config_from_config(profile_cfg, fee_rate)
            ),
        )
        .run(strategy, frame)
        .equity
    )


def _combine(
    curves: dict[tuple[int, float, float, str], pd.Series],
    horizons: tuple[int, int, int],
    weights: tuple[float, float, float],
    atr_mult: float,
    short_mult: float,
    profile: str,
) -> pd.Series:
    sleeves = [
        curves[(horizon, atr_mult, short_mult, profile)] * weight
        for horizon, weight in zip(horizons, weights, strict=True)
    ]
    return pd.concat(sleeves, axis=1, sort=True).ffill().dropna().sum(axis=1)


def _candidate_payload(
    equity: pd.Series,
    *,
    horizons: tuple[int, int, int],
    weights_name: str,
    weights: tuple[float, float, float],
    atr_mult: float,
    short_mult: float,
) -> dict:
    development = _slice(equity, None, DEVELOPMENT_END)
    validation = _slice(
        equity,
        DEVELOPMENT_END + pd.Timedelta(seconds=1),
        VALIDATION_END,
    )
    sealed_test = _slice(
        equity,
        VALIDATION_END + pd.Timedelta(seconds=1),
        None,
    )
    return {
        "params": {
            "horizons": list(horizons),
            "weights": list(weights),
            "weights_name": weights_name,
            "atr_mult": atr_mult,
            "short_mult": short_mult,
        },
        "development": _summary(development),
        "validation": _summary(validation),
        "sealed_test": _summary(sealed_test),
        "full": _summary(equity),
    }


def _selection_score(candidate: dict) -> float:
    """Score calculé sans le test scellé, pénalisant les résultats fragiles."""

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
    frame = resample(base, TIMEFRAME_TO_PANDAS[TIMEFRAME])
    frame = add_funding_columns(frame, funding, TIMEFRAME_TO_PANDAS[TIMEFRAME])

    required_horizons = sorted({value for values in HORIZON_SETS for value in values})
    curves: dict[tuple[int, float, float, str], pd.Series] = {}
    for horizon, atr_mult, short_mult in itertools.product(
        required_horizons,
        ATR_MULTIPLIERS,
        SHORT_MULTIPLIERS,
    ):
        curves[(horizon, atr_mult, short_mult, "normal")] = _atomic_curve(
            cfg,
            frame,
            horizon=horizon,
            atr_mult=atr_mult,
            short_mult=short_mult,
            profile="normal",
        )

    candidates = []
    for horizons, (weights_name, weights), atr_mult, short_mult in itertools.product(
        HORIZON_SETS,
        WEIGHT_SETS.items(),
        ATR_MULTIPLIERS,
        SHORT_MULTIPLIERS,
    ):
        equity = _combine(
            curves,
            horizons,
            weights,
            atr_mult,
            short_mult,
            "normal",
        )
        candidate = _candidate_payload(
            equity,
            horizons=horizons,
            weights_name=weights_name,
            weights=weights,
            atr_mult=atr_mult,
            short_mult=short_mult,
        )
        candidate["selection_score"] = _selection_score(candidate)
        candidates.append(candidate)

    candidates.sort(key=lambda item: item["selection_score"], reverse=True)
    winner = candidates[0]
    baseline_equity = _combine(
        curves,
        BASELINE["horizons"],
        BASELINE["weights"],
        BASELINE["atr_mult"],
        BASELINE["short_mult"],
        "normal",
    )
    baseline = _candidate_payload(
        baseline_equity,
        horizons=BASELINE["horizons"],
        weights_name="deployed",
        weights=BASELINE["weights"],
        atr_mult=BASELINE["atr_mult"],
        short_mult=BASELINE["short_mult"],
    )

    winner_params = winner["params"]
    for horizon in winner_params["horizons"]:
        key = (
            int(horizon),
            float(winner_params["atr_mult"]),
            float(winner_params["short_mult"]),
            "stress",
        )
        curves[key] = _atomic_curve(
            cfg,
            frame,
            horizon=key[0],
            atr_mult=key[1],
            short_mult=key[2],
            profile="stress",
        )
    stress_equity = _combine(
        curves,
        tuple(winner_params["horizons"]),
        tuple(winner_params["weights"]),
        float(winner_params["atr_mult"]),
        float(winner_params["short_mult"]),
        "stress",
    )
    winner["stress_full"] = _summary(stress_equity)

    adoption_checks = {
        "full_cagr_above_baseline": winner["full"]["cagr"] > baseline["full"]["cagr"],
        "full_sharpe_at_least_baseline": winner["full"]["sharpe"] >= baseline["full"]["sharpe"],
        "full_drawdown_no_worse": winner["full"]["max_drawdown"]
        >= baseline["full"]["max_drawdown"],
        "sealed_test_cagr_above_baseline": winner["sealed_test"]["cagr"]
        > baseline["sealed_test"]["cagr"],
        "sealed_test_sharpe_at_least_baseline": winner["sealed_test"]["sharpe"]
        >= baseline["sealed_test"]["sharpe"],
        "stress_sharpe_positive": winner["stress_full"]["sharpe"] > 0,
    }
    adopted = all(adoption_checks.values())

    data_path = ROOT / "data" / "binance_BTC-USDT_1h.csv"
    funding_path = ROOT / "data" / "binanceusdm_BTCUSDT_USDT_funding.csv"
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Recherche BTC-only orientée rendement, sans mutation du profil paper",
        "protocol": {
            "candidate_count": len(candidates),
            "development_end": DEVELOPMENT_END.isoformat(),
            "validation_end": VALIDATION_END.isoformat(),
            "sealed_test_used_for_selection": False,
            "selection_objective": "maximise le pire CAGR développement/validation avec bonus Sharpe",
            "adoption_requires_all_checks": True,
        },
        "provenance": {
            "script_sha256": _sha256(Path(__file__)),
            "config": {"path": CONFIG.relative_to(ROOT).as_posix(), "sha256": _sha256(CONFIG)},
            "data": [
                {"path": path.relative_to(ROOT).as_posix(), "sha256": _sha256(path)}
                for path in (data_path, funding_path)
            ],
        },
        "baseline": baseline,
        "selected_candidate": winner,
        "adoption_checks": adoption_checks,
        "adopted": adopted,
        "top_candidates_pre_test": candidates[:10],
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Candidats évalués : {len(candidates)}")
    print(f"Candidat sélectionné sans voir 2025+ : {winner_params}")
    for label in ("development", "validation", "sealed_test", "full", "stress_full"):
        metrics = winner[label]
        print(
            f"{label:12} CAGR {metrics['cagr']:+.1%} | Sharpe {metrics['sharpe']:.2f} "
            f"| DD {metrics['max_drawdown']:+.1%}"
        )
    print(
        f"Baseline test CAGR {baseline['sealed_test']['cagr']:+.1%} | "
        f"Sharpe {baseline['sealed_test']['sharpe']:.2f}"
    )
    print(f"Adoption : {'OUI' if adopted else 'NON'}")
    print(f"Artefact : {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
