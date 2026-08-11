"""Métriques de performance calculées sur la courbe d'equity."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..performance import (
    annualized_volatility,
    daily_returns,
    sharpe_ratio,
    sortino_ratio,
)

SECONDS_PER_CALENDAR_YEAR = 365.25 * 24.0 * 60.0 * 60.0


def _utc_index(equity: pd.Series) -> pd.DatetimeIndex:
    if not isinstance(equity.index, pd.DatetimeIndex):
        raise TypeError("l'equity doit utiliser un DatetimeIndex")
    if equity.index.tz is None:
        raise ValueError("l'equity doit utiliser des timestamps UTC explicites")
    index = equity.index.tz_convert("UTC")
    if not index.is_monotonic_increasing:
        raise ValueError("l'equity doit être ordonnée chronologiquement")
    return index


def _elapsed_years(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return float("nan")
    elapsed_seconds = (index[-1] - index[0]).total_seconds()
    return elapsed_seconds / SECONDS_PER_CALENDAR_YEAR


def compute_metrics(
    equity: pd.Series,
    trades: list,
    bars_per_year: float,
    buy_hold: pd.Series | None = None,
    *,
    exposure_bars: int | None = None,
) -> dict:
    """Calcule les ratios sans confondre observations et durée calendrier.

    Contrat Sharpe/volatilité inchangé : rendements des clôtures journalières
    observées, sans remplissage des jours absents, annualisés à 365 périodes.
    Le CAGR et la durée publiée utilisent exclusivement le span UTC réel.
    ``bars_per_year`` reste accepté pour compatibilité des appelants et ne sert
    plus à fabriquer une durée historique.
    """
    index = _utc_index(equity)
    performance_returns = daily_returns(equity)
    n_bars = len(equity)
    elapsed_years = _elapsed_years(index)
    first_day = index[0].normalize()
    last_day = index[-1].normalize()
    calendar_days = (last_day - first_day).days + 1
    observed_days = len(index.normalize().unique())
    missing_calendar_days = max(0, calendar_days - observed_days)

    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0
    cagr = (
        (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / elapsed_years) - 1.0
        if elapsed_years > 0
        else np.nan
    )

    vol = annualized_volatility(performance_returns)
    sharpe = sharpe_ratio(performance_returns)
    sortino = sortino_ratio(performance_returns)

    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_dd = drawdown.min()
    calmar = cagr / abs(max_dd) if max_dd < 0 else np.nan

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)

    exposure = np.nan
    if exposure_bars is not None:
        if exposure_bars < 0 or exposure_bars > n_bars:
            raise ValueError("exposure_bars doit être compris entre 0 et le nombre de barres")
        exposure = exposure_bars / n_bars if n_bars else np.nan
    elif trades:
        bars_in_pos = min(n_bars, sum(t.bars_held for t in trades))
        exposure = bars_in_pos / n_bars

    out = {
        "start": str(equity.index[0]),
        "end": str(equity.index[-1]),
        "years": round(elapsed_years, 2),
        "elapsed_years": elapsed_years,
        "calendar_duration_days": (index[-1] - index[0]).total_seconds() / 86_400.0,
        "observed_points": n_bars,
        "observed_calendar_days": observed_days,
        "missing_calendar_days": missing_calendar_days,
        "return_frequency": "calendar_day_close",
        "annualization_periods_per_year": 365.0,
        "data_gaps_imputed": False,
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
        out["buy_hold_sharpe"] = sharpe_ratio(daily_returns(buy_hold))
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
