"""Résumé quotidien ou hebdomadaire envoyé sur Telegram (timers systemd).

Quotidien : équity, PnL 24 h, positions, trades du jour.
Hebdo (--weekly) : en plus, bilan des trades de la semaine (win rate, PnL)
et drawdown courant. No-op silencieux si Telegram n'est pas configuré.
"""

import argparse
import os
from datetime import datetime, UTC
from pathlib import Path

import pandas as pd

from btcquant.config import load_config, portfolio_from_config
from btcquant.execution.health import execution_health
from btcquant.execution.state_store import StateStore
from btcquant.notify import notify
from btcquant.reporting.analytics import combined_equity, deposits_total
from btcquant.reporting.repository import ReportingRepository

ROOT = Path(os.environ.get("BTCQUANT_ROOT", Path.cwd())).resolve()
STATE = ROOT / "state"
repository = ReportingRepository(STATE)
PORTFOLIO = portfolio_from_config(load_config(ROOT / "environments" / "paper" / "config.yaml"))


def _repository() -> ReportingRepository:
    """Suit le répertoire STATE actif (tests, staging ou production)."""
    global repository
    if repository.state_dir != STATE:
        repository = ReportingRepository(STATE)
    return repository


def _engine_state(engine: str, legacy_name: str) -> dict:
    return _repository().read_engine_state(engine, STATE / legacy_name) or {}


def _equity(engine: str, legacy_name: str) -> pd.Series:
    return _repository().read_engine_equity(engine, STATE / legacy_name)


def _flows() -> pd.DataFrame:
    return _repository().read_flows()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weekly", action="store_true", help="bilan sur 7 jours")
    args = parser.parse_args()
    days = 7 if args.weekly else 1

    trend_state = _engine_state("trend", "live_state_4x.json")
    carry_state = _engine_state("carry", "carry_state.json")
    trend_eq = _equity("trend", "equity_trend.csv")
    carry_eq = _equity("carry", "equity_carry.csv")

    trend_val = (
        float(trend_eq.iloc[-1])
        if len(trend_eq)
        else sum(s.get("cash", 0.0) for s in trend_state.get("slots", {}).values())
    )
    carry_val = float(carry_state.get("equity", 0.0))
    total = trend_val + carry_val

    flows = _flows()
    deposits = deposits_total(flows)
    invested = PORTFOLIO.total_capital + deposits

    # PnL sur la période, net des apports de la période
    since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    day_pnl = None
    if len(trend_eq) and len(carry_eq):
        t0 = trend_eq[trend_eq.index >= since]
        c0 = carry_eq[carry_eq.index >= since]
        if len(t0) and len(c0):
            start = float(t0.iloc[0]) + float(c0.iloc[0])
            f0 = flows[flows["ts"] > min(t0.index[0], c0.index[0])]
            dep_period = float((f0["trend_flow"] + f0["carry_flow"]).sum()) if len(f0) else 0.0
            if start > 0:
                day_pnl = (total - dep_period) / start - 1.0

    # trades de la période
    trades_today = 0
    period_trades = None
    database = STATE / "btcquant.db"
    tf = _repository().read_trades()
    if len(tf):
        ex = pd.to_datetime(tf["exit_ts"], utc=True, format="ISO8601", errors="coerce")
        mask = (ex >= since) & ex.notna()
        trades_today = int(mask.sum())
        if args.weekly and trades_today:
            sub = tf[mask.values]
            period_trades = {
                "wins": int((sub["pnl"] > 0).sum()),
                "pnl": float(sub["pnl"].sum()),
            }

    positions = []
    for name, s in trend_state.get("slots", {}).items():
        pos = s.get("position")
        if pos:
            positions.append(
                f"{name.replace('trend_ls_', 'D')} "
                f"{'LONG' if pos.get('direction', 1) == 1 else 'SHORT'}"
            )

    label = "semaine" if args.weekly else "24 h"
    lines = [
        f"{'🗓 Bilan hebdomadaire' if args.weekly else '📊'} Tandem — "
        f"{datetime.now(UTC):%d/%m/%Y %H:%M} UTC",
        f"Équity totale : {total:,.0f} $ (départ 10 000 $"
        + (f" + apports {deposits:,.0f} $" if deposits else "")
        + f", {total / invested - 1:+.1%})",
        f"PnL {label} : {day_pnl:+.2%}" if day_pnl is not None else f"PnL {label} : n/d",
        f"  Trend 4x : {trend_val:,.0f} $   Carry 3x : {carry_val:,.0f} $",
        f"Positions : {', '.join(positions) if positions else 'aucune (en attente de signal)'}",
        f"Trades clôturés ({label}) : {trades_today}",
    ]
    if period_trades:
        lines.append(
            f"  dont {period_trades['wins']} gagnants — PnL trades : {period_trades['pnl']:+,.0f} $"
        )
    if args.weekly and len(trend_eq) > 2 and len(carry_eq) > 2:
        comb = combined_equity(trend_eq, carry_eq, flows, exclude_flows=True)
        if len(comb) > 2:
            dd_now = comb.iloc[-1] / comb.cummax().iloc[-1] - 1.0
            lines.append(f"Drawdown depuis le pic : {dd_now:+.1%}")
    if trend_state.get("halted"):
        lines.append("⛔ KILL-SWITCH ACTIF")
    if database.exists():
        store = StateStore(database, initialize=False)
        health = execution_health(store, "trend")
        if health.orders_analyzed:
            fill_ratio = f"{health.fill_ratio:.1%}" if health.fill_ratio is not None else "n/d"
            rejection_rate = (
                f"{health.rejection_rate:.1%}" if health.rejection_rate is not None else "n/d"
            )
            slippage = (
                f"{health.p95_slippage_bps:.1f} bps"
                if health.p95_slippage_bps is not None
                else "n/d"
            )
            lines.append(
                f"Exécution trend ({health.orders_analyzed} ordres) : "
                f"fill {fill_ratio}, rejet {rejection_rate}, "
                f"slippage p95 {slippage}"
            )
        incidents = store.read_incidents(open_only=True)
        if incidents:
            lines.append(f"⚠ Incidents ouverts : {len(incidents)}")
            lines.extend(f"  · {item['message']}" for item in incidents[:5])
    msg = "\n".join(lines)
    print(msg)
    notify(msg)


if __name__ == "__main__":
    main()
