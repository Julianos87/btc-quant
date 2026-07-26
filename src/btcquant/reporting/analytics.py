"""Calculs financiers purs utilisés par les interfaces de reporting.

Ce module ne lit aucun fichier, n'accède pas au réseau et ne dépend pas de
Flask. Les entrées sont des séries/dataframes déjà chargés par un repository.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


def deposits_total(flows: pd.DataFrame) -> float:
    """Retourne les apports externes nets, transferts internes neutralisés."""
    if flows.empty:
        return 0.0
    required = {"trend_flow", "carry_flow"}
    if not required.issubset(flows.columns):
        return 0.0
    return float((flows["trend_flow"] + flows["carry_flow"]).sum())


def net_of_flows(equity: pd.Series, flows: pd.DataFrame, column: str) -> pd.Series:
    """Soustrait le cumul des flux d'une poche à ses échantillons d'equity."""
    if equity.empty or flows.empty or column not in flows.columns or "ts" not in flows.columns:
        return equity
    cumulative = flows.groupby("ts")[column].sum().sort_index().cumsum()
    return equity - cumulative.reindex(equity.index, method="ffill").fillna(0.0)


def combined_equity(
    trend: pd.Series,
    carry: pd.Series,
    flows: pd.DataFrame | None = None,
    *,
    exclude_flows: bool = False,
) -> pd.Series:
    """Aligne à la minute et additionne les deux poches du portefeuille."""
    if trend.empty or carry.empty:
        return pd.Series(dtype=float)
    if exclude_flows and flows is not None:
        trend = net_of_flows(trend, flows, "trend_flow")
        carry = net_of_flows(carry, flows, "carry_flow")
    trend_minutes = trend.resample("1min").last().ffill()
    carry_minutes = carry.resample("1min").last().ffill()
    common_index = trend_minutes.index.intersection(carry_minutes.index)
    return (trend_minutes[common_index] + carry_minutes[common_index]).dropna()


def live_metrics(combined: pd.Series, initial_capital: float) -> dict[str, float | int | None]:
    """Calcule les ratios glissants du portefeuille sur une equity hors flux."""
    result: dict[str, float | int | None] = {
        "sharpe": None,
        "sortino": None,
        "calmar": None,
        "cagr": None,
        "max_dd": None,
        "cur_dd": None,
        "vol_annual": None,
        "days": 0,
    }
    if len(combined) < 3 or initial_capital <= 0:
        return result

    daily = combined.resample("1D").last().dropna()
    elapsed_seconds = (combined.index[-1] - combined.index[0]).total_seconds()
    result["days"] = int(elapsed_seconds // 86400)

    running_peak = combined.cummax()
    max_drawdown = float((combined / running_peak - 1.0).min())
    result["max_dd"] = max_drawdown
    result["cur_dd"] = float(combined.iloc[-1] / running_peak.iloc[-1] - 1.0)

    years = elapsed_seconds / (365.25 * 86400)
    ending_ratio = float(combined.iloc[-1]) / initial_capital
    if years > 0 and ending_ratio > 0:
        cagr = ending_ratio ** (1.0 / years) - 1.0
        result["cagr"] = float(cagr)
        if max_drawdown < 0:
            result["calmar"] = float(cagr / abs(max_drawdown))

    returns = daily.pct_change().dropna()
    if len(returns) >= 14 and returns.std() > 0:
        annualizer = math.sqrt(365)
        result["sharpe"] = float(returns.mean() / returns.std() * annualizer)
        result["vol_annual"] = float(returns.std() * annualizer)
        downside = returns[returns < 0]
        if len(downside) > 1 and downside.std() > 0:
            result["sortino"] = float(returns.mean() / downside.std() * annualizer)
    return result


def trade_analytics(
    trades: pd.DataFrame,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Agrège le PnL et calcule les records d'un journal de trades."""
    breakdown: dict[str, list[dict[str, Any]]] = {"by_strategy": [], "by_direction": []}
    records: dict[str, Any] = {}
    required = {"strategy", "direction", "pnl", "exit_ts"}
    if trades.empty or not required.issubset(trades.columns):
        return breakdown, records

    for key, field in (("by_strategy", "strategy"), ("by_direction", "direction")):
        breakdown[key] = [
            {
                "name": str(name).replace("trend_ls_", "D"),
                "n": int(len(group)),
                "wins": int((group["pnl"] > 0).sum()),
                "pnl": float(group["pnl"].sum()),
            }
            for name, group in trades.groupby(field)
        ]

    best: Any = trades.loc[trades["pnl"].idxmax()]
    worst: Any = trades.loc[trades["pnl"].idxmin()]
    chronological = trades.sort_values("exit_ts")["pnl"]

    def longest(winning: bool) -> int:
        longest_run = current_run = 0
        for pnl in chronological:
            matches = (pnl > 0) if winning else (pnl <= 0)
            current_run = current_run + 1 if matches else 0
            longest_run = max(longest_run, current_run)
        return longest_run

    records.update(
        {
            "biggest_win": float(best["pnl"]),
            "biggest_win_strat": str(best["strategy"]).replace("trend_ls_", "D"),
            "biggest_loss": float(worst["pnl"]),
            "biggest_loss_strat": str(worst["strategy"]).replace("trend_ls_", "D"),
            "longest_win_streak": longest(True),
            "longest_loss_streak": longest(False),
        }
    )
    return breakdown, records


def carry_funding_curve(
    carry_equity: pd.Series,
    flows: pd.DataFrame,
    initial_capital: float,
    *,
    max_points: int = 400,
) -> tuple[float | None, list[list[int | float]]]:
    """Retourne le PnL carry hors flux et une courbe limitée pour l'API."""
    if len(carry_equity) <= 1:
        return None, []
    cumulative = carry_equity - initial_capital
    if not flows.empty and {"ts", "carry_flow"}.issubset(flows.columns):
        carry_flows = flows.groupby("ts")["carry_flow"].sum().sort_index().cumsum()
        cumulative = cumulative - carry_flows.reindex(cumulative.index, method="ffill").fillna(0.0)
    total = float(cumulative.iloc[-1])
    if len(cumulative) > max_points:
        cumulative = cumulative.resample("1h").last().dropna()
    curve = [
        [int(pd.Timestamp(str(timestamp)).timestamp() * 1000), round(float(value), 2)]
        for timestamp, value in cumulative.items()
    ]
    return total, curve


def best_and_worst_day(combined: pd.Series) -> tuple[float | None, float | None]:
    """Retourne les meilleurs et pires rendements journaliers."""
    if len(combined) <= 2:
        return None, None
    daily_returns = combined.resample("1D").last().pct_change().dropna()
    if daily_returns.empty:
        return None, None
    return float(daily_returns.max()), float(daily_returns.min())
