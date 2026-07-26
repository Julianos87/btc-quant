"""Génère la référence reproductible du profil trend sans accès réseau.

Le fichier produit lie les résultats aux hashes du code source courant, de la
configuration et de chaque cache de données. Il sert de garde anti-régression
pendant le refactoring ; ce n'est pas une promesse de performance future.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from btcquant.backtest import BacktestEngine
from btcquant.backtest.metrics import compute_metrics
from btcquant.carry import add_funding_columns, load_funding
from btcquant.config import (
    build_strategies,
    execution_config_from_config,
    load_config,
    risk_from_config,
)
from btcquant.data import TIMEFRAME_TO_PANDAS, load_ohlcv, resample
from btcquant.domain import ExecutionSimulator
from btcquant.indicators import bars_per_year
from btcquant.risk import RiskConfig

CONFIG = ROOT / "config_4x.yaml"
DESTINATION = ROOT / "audit" / "baseline_reference.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def _tree_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> None:
    cfg = load_config(CONFIG)
    risk = risk_from_config(cfg)
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

    curves: dict[str, pd.Series] = {}
    all_trades = []
    strategies: dict[str, dict[str, float | int | str]] = {}
    built = build_strategies(cfg)
    for strategy, fraction, market in built:
        frame = (
            base
            if strategy.timeframe == cfg["data"]["base_timeframe"]
            else resample(base, TIMEFRAME_TO_PANDAS[strategy.timeframe])
        )
        if market == "perp":
            frame = add_funding_columns(frame, funding, TIMEFRAME_TO_PANDAS[strategy.timeframe])
        slot_risk = RiskConfig(
            **{
                **risk.__dict__,
                "initial_capital": risk.initial_capital * fraction,
            }
        )
        fee_rate = cfg["costs"]["perp_fee_rate"] if market == "perp" else cfg["costs"]["fee_rate"]
        result = BacktestEngine(
            risk=slot_risk,
            funding_rate_8h=(cfg["costs"].get("funding_rate_8h", 0.0) if market == "perp" else 0.0),
            allow_short=market == "perp",
            execution_simulator=ExecutionSimulator(execution_config_from_config(cfg, fee_rate)),
        ).run(strategy, frame)
        curves[strategy.name] = result.equity
        all_trades.extend(result.trades)
        strategies[strategy.name] = {
            "timeframe": strategy.timeframe,
            "market": market,
            "capital_fraction": fraction,
            "trades": len(result.trades),
            "final_equity": float(result.equity.iloc[-1]),
            "sharpe": float(result.metrics["sharpe"]),
            "max_drawdown": float(result.metrics["max_drawdown"]),
        }

    combined = pd.concat(curves.values(), axis=1, sort=True).ffill().dropna().sum(axis=1)
    metrics = compute_metrics(
        combined, [], max(bars_per_year(strategy.timeframe) for strategy, _, _ in built)
    )
    chronological_trades = sorted(all_trades, key=lambda trade: trade.exit_time)
    worst_loss_streak = loss_streak = 0
    for trade in chronological_trades:
        loss_streak = loss_streak + 1 if trade.pnl <= 0 else 0
        worst_loss_streak = max(worst_loss_streak, loss_streak)
    winning_returns = [trade.pnl_pct for trade in all_trades if trade.pnl > 0]
    losing_returns = [trade.pnl_pct for trade in all_trades if trade.pnl <= 0]
    drawdowns = combined / combined.cummax() - 1.0
    monthly_returns = combined.resample("ME").last().pct_change().dropna()
    elapsed_years = (combined.index[-1] - combined.index[0]).total_seconds() / (365.25 * 86400)
    safe_symbol = cfg["symbol"].replace("/", "-")
    safe_funding_symbol = f"{cfg['symbol']}:{cfg['quote_currency']}".replace("/", "").replace(
        ":", "_"
    )
    data_files = [
        ROOT
        / cfg["data"]["dir"]
        / f"{cfg['exchange']}_{safe_symbol}_{cfg['data']['base_timeframe']}.csv",
        ROOT / "data" / f"binanceusdm_{safe_funding_symbol}_funding.csv",
    ]
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "purpose": "Safety Baseline anti-régression; aucune promesse de performance",
        "provenance": {
            "base_git_commit": _git_commit(),
            "source_tree_sha256": _tree_sha256(list((ROOT / "src").rglob("*.py"))),
            "config": {
                "path": str(CONFIG.relative_to(ROOT)),
                "sha256": _sha256(CONFIG),
            },
            "data": [
                {
                    "path": path.relative_to(ROOT).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for path in data_files
            ],
            "base_rows": len(base),
            "base_span": [base.index[0].isoformat(), base.index[-1].isoformat()],
            "funding_rows": len(funding),
            "funding_span": [
                funding.index[0].isoformat(),
                funding.index[-1].isoformat(),
            ],
        },
        "results": {
            "strategies": strategies,
            "combined": {
                "trades": sum(int(item["trades"]) for item in strategies.values()),
                "final_equity": float(combined.iloc[-1]),
                "cagr": float(metrics["cagr"]),
                "sharpe": float(metrics["sharpe"]),
                "max_drawdown": float(metrics["max_drawdown"]),
            },
            "conformity": {
                "profile": "trend 4x — baseline reproductible",
                "n_trades": len(all_trades),
                "trades_per_year": len(all_trades) / elapsed_years,
                "win_rate": sum(trade.pnl > 0 for trade in all_trades) / len(all_trades),
                "avg_win_pct": sum(winning_returns) / len(winning_returns),
                "avg_loss_pct": sum(losing_returns) / len(losing_returns),
                "worst_loss_streak": worst_loss_streak,
                "max_drawdown": float(drawdowns.min()),
                "dd_time_fraction": {
                    str(threshold): float((drawdowns <= -threshold / 100).mean())
                    for threshold in range(5, 55, 5)
                },
                "monthly_return_p10": float(monthly_returns.quantile(0.10)),
                "monthly_return_p90": float(monthly_returns.quantile(0.90)),
                "worst_month": float(monthly_returns.min()),
            },
        },
    }
    DESTINATION.parent.mkdir(exist_ok=True)
    DESTINATION.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Référence écrite : {DESTINATION}")


if __name__ == "__main__":
    main()
