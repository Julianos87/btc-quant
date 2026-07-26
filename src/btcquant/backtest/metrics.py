"""Métriques de performance calculées sur la courbe d'équity et les trades."""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_metrics(
    equity: pd.Series,
    trades: list,
    bars_per_year: float,
    buy_hold: pd.Series | None = None,
) -> dict:
    returns = equity.pct_change().dropna()
    n_bars = len(equity)
    years = n_bars / bars_per_year if bars_per_year else np.nan

    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 else np.nan

    vol = returns.std() * np.sqrt(bars_per_year)
    sharpe = (
        returns.mean() / returns.std() * np.sqrt(bars_per_year) if returns.std() > 0 else np.nan
    )
    downside = returns[returns < 0]
    sortino = (
        returns.mean() / downside.std() * np.sqrt(bars_per_year)
        if len(downside) > 1 and downside.std() > 0
        else np.nan
    )

    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_dd = drawdown.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)

    exposure = np.nan
    if trades:
        bars_in_pos = sum(t.bars_held for t in trades)
        exposure = bars_in_pos / n_bars

    out = {
        "start": str(equity.index[0]),
        "end": str(equity.index[-1]),
        "years": round(years, 2),
        "total_return": total_return,
        "cagr": cagr,
        "volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "n_trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else np.nan,
        "profit_factor": gross_win / gross_loss
        if gross_loss > 0
        else np.inf
        if gross_win > 0
        else np.nan,
        "avg_win_pct": np.mean([t.pnl_pct for t in wins]) if wins else np.nan,
        "avg_loss_pct": np.mean([t.pnl_pct for t in losses]) if losses else np.nan,
        "avg_bars_held": np.mean([t.bars_held for t in trades]) if trades else np.nan,
        "exposure": exposure,
    }
    if buy_hold is not None and len(buy_hold) > 1:
        out["buy_hold_return"] = buy_hold.iloc[-1] / buy_hold.iloc[0] - 1.0
        bh_ret = buy_hold.pct_change().dropna()
        out["buy_hold_sharpe"] = (
            bh_ret.mean() / bh_ret.std() * np.sqrt(bars_per_year) if bh_ret.std() > 0 else np.nan
        )
        bh_dd = (buy_hold / buy_hold.cummax() - 1.0).min()
        out["buy_hold_max_dd"] = bh_dd
    return out


def format_metrics(metrics: dict) -> str:
    pct = lambda v: f"{v:+.1%}" if pd.notna(v) else "n/a"
    num = lambda v: f"{v:.2f}" if pd.notna(v) else "n/a"
    lines = [
        f"Période            : {metrics['start']} → {metrics['end']} ({metrics['years']} ans)",
        f"Rendement total    : {pct(metrics['total_return'])}   (buy & hold : {pct(metrics.get('buy_hold_return', float('nan')))})",
        f"CAGR               : {pct(metrics['cagr'])}",
        f"Volatilité ann.    : {pct(metrics['volatility'])}",
        f"Sharpe             : {num(metrics['sharpe'])}   (buy & hold : {num(metrics.get('buy_hold_sharpe', float('nan')))})",
        f"Sortino            : {num(metrics['sortino'])}",
        f"Max drawdown       : {pct(metrics['max_drawdown'])}   (buy & hold : {pct(metrics.get('buy_hold_max_dd', float('nan')))})",
        f"Calmar             : {num(metrics['calmar'])}",
        f"Trades             : {metrics['n_trades']}  |  win rate {pct(metrics['win_rate'])}  |  profit factor {num(metrics['profit_factor'])}",
        f"Gain moyen         : {pct(metrics['avg_win_pct'])}  |  perte moyenne : {pct(metrics['avg_loss_pct'])}",
        f"Durée moyenne      : {num(metrics['avg_bars_held'])} barres  |  exposition : {pct(metrics['exposure'])}",
    ]
    return "\n".join(lines)
