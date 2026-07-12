"""Résumé quotidien ou hebdomadaire envoyé sur Telegram (timers systemd).

Quotidien : équity, PnL 24 h, positions, trades du jour.
Hebdo (--weekly) : en plus, bilan des trades de la semaine (win rate, PnL)
et drawdown courant. No-op silencieux si Telegram n'est pas configuré.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from btcquant.notify import notify

STATE = ROOT / "state"


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _equity(name: str) -> pd.Series:
    p = STATE / name
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(p)
    idx = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return pd.Series(df["equity"].values, index=idx).sort_index()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weekly", action="store_true", help="bilan sur 7 jours")
    args = parser.parse_args()
    days = 7 if args.weekly else 1

    trend_state = _read_json(STATE / "live_state_4x.json")
    carry_state = _read_json(STATE / "carry_state.json")
    trend_eq = _equity("equity_trend.csv")
    carry_eq = _equity("equity_carry.csv")

    trend_val = float(trend_eq.iloc[-1]) if len(trend_eq) else sum(
        s.get("cash", 0.0) for s in trend_state.get("slots", {}).values()
    )
    carry_val = float(carry_state.get("equity", 0.0))
    total = trend_val + carry_val

    # PnL sur la période
    since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    day_pnl = None
    if len(trend_eq) and len(carry_eq):
        t0 = trend_eq[trend_eq.index >= since]
        c0 = carry_eq[carry_eq.index >= since]
        if len(t0) and len(c0):
            start = float(t0.iloc[0]) + float(c0.iloc[0])
            if start > 0:
                day_pnl = total / start - 1.0

    # trades de la période
    trades_today = 0
    period_trades = None
    tpath = STATE / "trades.csv"
    if tpath.exists():
        tf = pd.read_csv(tpath)
        if len(tf):
            ex = pd.to_datetime(tf["exit_ts"], utc=True, format="ISO8601")
            mask = ex >= since
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
            positions.append(f"{name.replace('trend_ls_', 'D')} "
                             f"{'LONG' if pos.get('direction', 1) == 1 else 'SHORT'}")

    label = "semaine" if args.weekly else "24 h"
    lines = [
        f"{'🗓 Bilan hebdomadaire' if args.weekly else '📊'} btc-quant — "
        f"{datetime.now(timezone.utc):%d/%m/%Y %H:%M} UTC",
        f"Équity totale : {total:,.0f} $ (départ 10 000 $, {total/10000-1:+.1%})",
        f"PnL {label} : {day_pnl:+.2%}" if day_pnl is not None else f"PnL {label} : n/d",
        f"  Trend 4x : {trend_val:,.0f} $   Carry 3x : {carry_val:,.0f} $",
        f"Positions : {', '.join(positions) if positions else 'aucune (en attente de signal)'}",
        f"Trades clôturés ({label}) : {trades_today}",
    ]
    if period_trades:
        lines.append(f"  dont {period_trades['wins']} gagnants — PnL trades : "
                     f"{period_trades['pnl']:+,.0f} $")
    if args.weekly and len(trend_eq) > 2 and len(carry_eq) > 2:
        t = trend_eq.resample("1min").last().ffill()
        c = carry_eq.resample("1min").last().ffill()
        idx = t.index.intersection(c.index)
        comb = (t[idx] + c[idx]).dropna()
        if len(comb) > 2:
            dd_now = comb.iloc[-1] / comb.cummax().iloc[-1] - 1.0
            lines.append(f"Drawdown depuis le pic : {dd_now:+.1%}")
    if trend_state.get("halted"):
        lines.append("⛔ KILL-SWITCH ACTIF")
    msg = "\n".join(lines)
    print(msg)
    notify(msg)


if __name__ == "__main__":
    main()
