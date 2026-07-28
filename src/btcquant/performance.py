"""Conventions canoniques pour les ratios de performance.

Les stratégies tournent sur plusieurs timeframes, mais leurs ratios doivent
rester comparables. Le Sharpe, le Sortino et la volatilité sont donc toujours
calculés sur des rendements calendaires journaliers, avec un écart-type
d'échantillon (``ddof=1``) et 365 périodes par an.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

DAILY_PERIODS_PER_YEAR = 365.0


def daily_returns(equity: pd.Series) -> pd.Series:
    """Convertit une equity horodatée en rendements de clôture journaliers."""

    if not isinstance(equity.index, pd.DatetimeIndex):
        raise TypeError("l'equity doit utiliser un DatetimeIndex")
    if len(equity) < 2:
        return pd.Series(dtype=float)
    daily = equity.sort_index().resample("1D").last().dropna()
    return daily.pct_change().replace([np.inf, -np.inf], np.nan).dropna()


def _clean_returns(returns: pd.Series | Iterable[float]) -> pd.Series:
    if isinstance(returns, pd.Series):
        series = returns.astype(float)
    else:
        series = pd.Series(list(returns), dtype=float)
    return series.replace([np.inf, -np.inf], np.nan).dropna()


def annualized_volatility(
    returns: pd.Series | Iterable[float],
    *,
    periods_per_year: float = DAILY_PERIODS_PER_YEAR,
) -> float:
    """Volatilité annualisée, selon la convention d'échantillon ``ddof=1``."""

    if periods_per_year <= 0:
        raise ValueError("periods_per_year doit être strictement positif")
    clean = _clean_returns(returns)
    if len(clean) < 2:
        return float("nan")
    return float(clean.std(ddof=1) * np.sqrt(periods_per_year))


def sharpe_ratio(
    returns: pd.Series | Iterable[float],
    *,
    periods_per_year: float = DAILY_PERIODS_PER_YEAR,
) -> float:
    """Sharpe annualisé à taux sans risque nul."""

    clean = _clean_returns(returns)
    volatility = annualized_volatility(clean, periods_per_year=periods_per_year)
    if not np.isfinite(volatility) or volatility <= 0:
        return float("nan")
    return float(clean.mean() * periods_per_year / volatility)


def sortino_ratio(
    returns: pd.Series | Iterable[float],
    *,
    periods_per_year: float = DAILY_PERIODS_PER_YEAR,
) -> float:
    """Sortino annualisé utilisant l'écart-type des seuls rendements négatifs."""

    clean = _clean_returns(returns)
    downside = clean[clean < 0]
    downside_volatility = annualized_volatility(
        downside,
        periods_per_year=periods_per_year,
    )
    if not np.isfinite(downside_volatility) or downside_volatility <= 0:
        return float("nan")
    return float(clean.mean() * periods_per_year / downside_volatility)
