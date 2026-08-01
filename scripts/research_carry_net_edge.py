"""Teste un signal carry net de l'emprunt et des couts de bascule.

La selection utilise uniquement 2019-2024. La periode 2025+ reste scellee
jusqu'au choix final. Ce protocole n'autorise aucun changement du profil paper:
le basis et le taux d'emprunt historiques reels restent incomplets.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.carry import (  # noqa: E402
    PAYMENTS_PER_YEAR,
    PAPER_CARRY_POLICY,
    CarryPolicy,
    backtest_carry,
    load_funding,
)
from btcquant.performance import daily_returns, sharpe_ratio  # noqa: E402

OUTPUT = ROOT / "audit" / "carry_net_edge_research.json"
DATA = ROOT / "data" / "binanceusdm_BTCUSDT_USDT_funding.csv"
DEVELOPMENT_END = pd.Timestamp("2022-12-31 23:59:59", tz="UTC")
VALIDATION_END = pd.Timestamp("2024-12-31 23:59:59", tz="UTC")
EXPECTED_HOLDING_DAYS = (90, 180)
MINIMUM_NET_RETURNS = (0.0, 0.03)
EXIT_MODES = ("raw_zero", "net_breakeven")
BORROW_STRESS_RATES = (0.05, 0.10, 0.15, 0.20)


@dataclass(frozen=True)
class NetCarryRule:
    expected_holding_days: int
    minimum_net_return_ann: float
    exit_mode: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def raw_funding_threshold(
    policy: CarryPolicy,
    *,
    expected_holding_days: int,
    minimum_net_return_ann: float,
) -> float:
    """Funding brut requis pour payer emprunt, aller-retour et marge nette."""

    round_trip_cost = 2.0 * policy.switch_cost
    annualized_switch_cost = round_trip_cost * 365.0 / expected_holding_days
    return (
        (policy.leverage - 1.0) * policy.borrow_rate_ann
        + annualized_switch_cost
        + minimum_net_return_ann
    ) / policy.leverage


def raw_funding_breakeven(policy: CarryPolicy) -> float:
    return (policy.leverage - 1.0) * policy.borrow_rate_ann / policy.leverage


def _summary(equity: pd.Series) -> dict[str, float]:
    years = len(equity) / PAYMENTS_PER_YEAR
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0)
    drawdown = float((equity / equity.cummax() - 1.0).min())
    return {
        "cagr": cagr,
        "sharpe": float(sharpe_ratio(daily_returns(equity))),
        "max_drawdown": drawdown,
        "total_return": total_return,
    }


def _period(equity: pd.Series, start: pd.Timestamp | None, end: pd.Timestamp | None) -> dict:
    selected = equity
    if start is not None:
        selected = selected[selected.index >= start]
    if end is not None:
        selected = selected[selected.index <= end]
    return _summary(selected)


def _evaluate(
    funding: pd.Series,
    policy: CarryPolicy,
    *,
    label: str,
    enter_ann: float,
    exit_ann: float,
    rule: NetCarryRule | None,
) -> dict:
    result = backtest_carry(
        funding,
        leverage=policy.leverage,
        fee_rate=policy.fee_rate,
        slippage_bps=policy.slippage_bps,
        enter_ann=enter_ann,
        exit_ann=exit_ann,
        smooth_days=policy.smooth_days,
        initial_capital=policy.capital,
        borrow_rate_ann=policy.borrow_rate_ann,
    )
    equity = result["equity"]
    return {
        "label": label,
        "rule": asdict(rule) if rule else None,
        "enter_ann": enter_ann,
        "exit_ann": exit_ann,
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
        "exposure": float(result["exposure"]),
        "cycles": int(result["cycles"]),
        "real_market_inputs_complete": bool(result["real_market_inputs_complete"]),
    }


def _score(candidate: dict) -> float:
    development = candidate["development"]
    validation = candidate["validation"]
    values = (
        development["cagr"],
        development["sharpe"],
        validation["cagr"],
        validation["sharpe"],
    )
    if not all(math.isfinite(value) and value > 0 for value in values):
        return float("-inf")
    return min(development["cagr"], validation["cagr"]) + 0.10 * min(
        development["sharpe"], validation["sharpe"]
    )


def _candidate_for_rule(
    funding: pd.Series,
    policy: CarryPolicy,
    rule: NetCarryRule,
) -> dict:
    enter_ann = raw_funding_threshold(
        policy,
        expected_holding_days=rule.expected_holding_days,
        minimum_net_return_ann=rule.minimum_net_return_ann,
    )
    exit_ann = 0.0 if rule.exit_mode == "raw_zero" else raw_funding_breakeven(policy)
    item = _evaluate(
        funding,
        policy,
        label=(
            f"net_{rule.minimum_net_return_ann:.0%}_hold_{rule.expected_holding_days}d_"
            f"{rule.exit_mode}"
        ),
        enter_ann=enter_ann,
        exit_ann=exit_ann,
        rule=rule,
    )
    item["selection_score"] = _score(item)
    return item


def main() -> None:
    funding = load_funding(data_dir=ROOT / "data", refresh=False)
    policy = PAPER_CARRY_POLICY
    baseline = _evaluate(
        funding,
        policy,
        label="paper_baseline",
        enter_ann=policy.enter_ann,
        exit_ann=policy.exit_ann,
        rule=None,
    )
    candidates = [
        _candidate_for_rule(
            funding,
            policy,
            NetCarryRule(holding_days, minimum_net, exit_mode),
        )
        for holding_days in EXPECTED_HOLDING_DAYS
        for minimum_net in MINIMUM_NET_RETURNS
        for exit_mode in EXIT_MODES
    ]
    candidates.sort(key=lambda item: item["selection_score"], reverse=True)
    selected = candidates[0]

    selected_rule = NetCarryRule(**selected["rule"])
    borrow_sensitivity = {}
    for borrow_rate in BORROW_STRESS_RATES:
        stressed_policy = CarryPolicy(**{**asdict(policy), "borrow_rate_ann": borrow_rate})
        borrow_sensitivity[f"{borrow_rate:.0%}"] = _candidate_for_rule(
            funding,
            stressed_policy,
            selected_rule,
        )

    checks = {
        "full_cagr_above_baseline": selected["full"]["cagr"] > baseline["full"]["cagr"],
        "full_sharpe_above_baseline": selected["full"]["sharpe"] > baseline["full"]["sharpe"],
        "full_drawdown_no_worse": selected["full"]["max_drawdown"]
        >= baseline["full"]["max_drawdown"],
        "sealed_cagr_no_worse": selected["sealed_test"]["cagr"] >= baseline["sealed_test"]["cagr"],
        "sealed_drawdown_no_worse": selected["sealed_test"]["max_drawdown"]
        >= baseline["sealed_test"]["max_drawdown"],
        "positive_at_15pct_borrow": borrow_sensitivity["15%"]["full"]["cagr"] > 0,
        "real_market_inputs_complete": selected["real_market_inputs_complete"],
    }
    research_passed = all(
        value for key, value in checks.items() if key != "real_market_inputs_complete"
    )
    adopted = research_passed and checks["real_market_inputs_complete"]
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Signal carry net de l'emprunt et des couts de bascule",
        "protocol": {
            "development_end": DEVELOPMENT_END.isoformat(),
            "validation_end": VALIDATION_END.isoformat(),
            "sealed_test_used_for_selection": False,
            "expected_holding_days": list(EXPECTED_HOLDING_DAYS),
            "minimum_net_returns_ann": list(MINIMUM_NET_RETURNS),
            "exit_modes": list(EXIT_MODES),
            "borrow_stress_rates": list(BORROW_STRESS_RATES),
            "limitation": "basis et taux d'emprunt historiques reels incomplets",
        },
        "provenance": {
            "script_sha256": _sha256(Path(__file__)),
            "data": {"path": DATA.relative_to(ROOT).as_posix(), "sha256": _sha256(DATA)},
        },
        "paper_policy": asdict(policy),
        "baseline": baseline,
        "selected_pre_test": selected,
        "borrow_sensitivity": borrow_sensitivity,
        "adoption_checks": checks,
        "research_passed": research_passed,
        "adopted": adopted,
        "candidates_ranked_pre_test": candidates,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Selection pre-test: {selected['label']} | entree {selected['enter_ann']:.2%} "
        f"| sortie {selected['exit_ann']:.2%}"
    )
    for label in ("development", "validation", "sealed_test", "full"):
        metrics = selected[label]
        print(
            f"{label:12} CAGR {metrics['cagr']:+.1%} | Sharpe {metrics['sharpe']:.2f} "
            f"| DD {metrics['max_drawdown']:+.1%}"
        )
    print(
        f"Recherche: {'PASS' if research_passed else 'FAIL'} | Adoption: {'OUI' if adopted else 'NON'}"
    )
    print(f"Artefact: {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
