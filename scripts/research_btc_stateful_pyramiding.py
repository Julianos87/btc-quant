"""Validation exacte d'un renfort stateful sur la position BTC existante."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from itertools import product
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from btcquant.carry import add_funding_columns, load_funding
from btcquant.config import load_config, risk_from_config
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from scripts.research_btc_cost_filter import (
    CONFIG,
    DEVELOPMENT_END,
    VALIDATION_END,
    _period,
    _sha256,
    _summary,
)
from scripts.research_btc_trend_reinforcement import _ensemble

OUTPUT = ROOT / "audit" / "btc_stateful_pyramiding_research.json"
STEPS_ATR = (0.25, 0.50, 0.75, 1.00)
ADD_FRACTIONS = (0.15, 0.30, 0.45)
# Hypothèse issue du proxy de renforcement, fixée avant le test stateful.
RETURN_PROFILE = (0.50, 0.30)


def _candidate(
    equity: pd.Series,
    trades: int,
    step: float | None,
    fraction: float,
) -> dict:
    return {
        "pyramid_atr_step": step,
        "pyramid_add_fraction": fraction,
        "pyramid_max_adds": 0 if step is None else 1,
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
    if development["max_drawdown"] < -0.60 or validation["max_drawdown"] < -0.60:
        return float("-inf")
    return min(development["cagr"], validation["cagr"]) + 0.05 * min(
        development["sharpe"],
        validation["sharpe"],
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
    capital = float(risk_from_config(cfg).initial_capital)
    baseline_equity, baseline_trades = _ensemble(
        cfg,
        frame,
        capital=capital,
        entry_buffer_atr=0.0,
        profile="normal",
    )
    baseline = _candidate(baseline_equity, baseline_trades, None, 0.0)
    baseline["pretest_score"] = _score(baseline)
    candidates = [baseline]
    for step, fraction in product(STEPS_ATR, ADD_FRACTIONS):
        equity, trades = _ensemble(
            cfg,
            frame,
            capital=capital,
            entry_buffer_atr=0.0,
            pyramid_atr_step=step,
            pyramid_add_fraction=fraction,
            profile="normal",
        )
        item = _candidate(equity, trades, step, fraction)
        item["pretest_score"] = _score(item)
        candidates.append(item)
    selected = max(candidates, key=_score)
    stress_equity, _ = _ensemble(
        cfg,
        frame,
        capital=capital,
        entry_buffer_atr=0.0,
        pyramid_atr_step=selected["pyramid_atr_step"],
        pyramid_add_fraction=selected["pyramid_add_fraction"] or 0.30,
        profile="stress",
    )
    selected["stress_full"] = _summary(stress_equity)
    return_profile = next(
        item
        for item in candidates
        if (
            item["pyramid_atr_step"],
            item["pyramid_add_fraction"],
        )
        == RETURN_PROFILE
    )
    return_stress_equity, _ = _ensemble(
        cfg,
        frame,
        capital=capital,
        entry_buffer_atr=0.0,
        pyramid_atr_step=return_profile["pyramid_atr_step"],
        pyramid_add_fraction=return_profile["pyramid_add_fraction"],
        profile="stress",
    )
    return_profile["stress_full"] = _summary(return_stress_equity)
    checks = {
        "full_cagr_gain_at_least_10pp": (
            return_profile["full"]["cagr"] >= baseline["full"]["cagr"] + 0.10
        ),
        "full_sharpe_no_worse": (return_profile["full"]["sharpe"] >= baseline["full"]["sharpe"]),
        "full_drawdown_degradation_at_most_1pp": (
            return_profile["full"]["max_drawdown"] >= baseline["full"]["max_drawdown"] - 0.01
        ),
        "sealed_test_cagr_no_worse": (
            return_profile["sealed_test"]["cagr"] >= baseline["sealed_test"]["cagr"]
        ),
        "stress_drawdown_above_halt": (return_profile["stress_full"]["max_drawdown"] > -0.60),
    }
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Validation exacte du renforcement stateful BTC",
        "protocol": {
            "candidate_count": len(candidates),
            "selection_uses_sealed_test": False,
            "steps_atr": list(STEPS_ATR),
            "add_fractions": list(ADD_FRACTIONS),
            "max_adds": 1,
            "accounting": [
                "ordre exécuté à l'ouverture suivante",
                "frais et slippage appliqués à l'ajout",
                "prix d'entrée moyen pondéré",
                "quantité totale bornée par le plafond de levier",
            ],
        },
        "provenance": {
            "script_sha256": _sha256(Path(__file__)),
            "engine_sha256": _sha256(ROOT / "src" / "btcquant" / "backtest" / "engine.py"),
            "strategy_sha256": _sha256(ROOT / "src" / "btcquant" / "strategies" / "trend_ls.py"),
            "config_sha256": _sha256(CONFIG),
        },
        "baseline": baseline,
        "selected_pretest": selected,
        "recommended_return_profile": return_profile,
        "adoption_checks": checks,
        "adopted": all(checks.values()),
        "candidates": sorted(candidates, key=_score, reverse=True),
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Profil rendement {return_profile['pyramid_add_fraction']:.0%} après "
        f"{return_profile['pyramid_atr_step']} ATR | "
        f"CAGR {return_profile['full']['cagr']:+.1%} | "
        f"Sharpe {return_profile['full']['sharpe']:.2f} | "
        f"DD {return_profile['full']['max_drawdown']:+.1%} | "
        f"test {return_profile['sealed_test']['cagr']:+.1%} | "
        f"stress DD {return_profile['stress_full']['max_drawdown']:+.1%}"
    )
    print(f"Adoption : {'OUI' if payload['adopted'] else 'NON'}")
    print(f"Artefact : {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
