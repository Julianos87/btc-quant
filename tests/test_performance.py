from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from btcquant.performance import (
    DAILY_PERIODS_PER_YEAR,
    annualized_volatility,
    daily_returns,
    sharpe_ratio,
    sortino_ratio,
)
from btcquant.backtest.metrics import compute_metrics
from btcquant.reporting.analytics import live_metrics


def test_daily_returns_make_intraday_and_daily_equity_comparable():
    index = pd.date_range("2030-01-01", periods=12, freq="6h", tz="UTC")
    intraday = pd.Series(
        [100, 101, 102, 104, 104, 105, 103, 106, 106, 107, 108, 110],
        index=index,
        dtype=float,
    )
    daily = intraday.resample("1D").last().dropna()

    assert daily_returns(intraday).equals(daily.pct_change().dropna())
    assert sharpe_ratio(daily_returns(intraday)) == pytest.approx(
        sharpe_ratio(daily_returns(daily))
    )


def test_ratios_use_sample_standard_deviation_and_daily_annualization():
    returns = pd.Series([0.01, -0.02, 0.03, -0.01], dtype=float)
    expected_volatility = returns.std(ddof=1) * np.sqrt(DAILY_PERIODS_PER_YEAR)

    assert annualized_volatility(returns) == pytest.approx(expected_volatility)
    assert sharpe_ratio(returns) == pytest.approx(
        returns.mean() / returns.std(ddof=1) * np.sqrt(DAILY_PERIODS_PER_YEAR)
    )
    downside = returns[returns < 0]
    assert sortino_ratio(returns) == pytest.approx(
        returns.mean() / downside.std(ddof=1) * np.sqrt(DAILY_PERIODS_PER_YEAR)
    )


def test_ratios_reject_invalid_annualizer_and_return_nan_without_dispersion():
    with pytest.raises(ValueError, match="strictement positif"):
        annualized_volatility([0.01, 0.02], periods_per_year=0)

    assert np.isnan(sharpe_ratio([0.01]))
    assert np.isnan(sharpe_ratio([0.01, 0.01]))
    assert np.isnan(sortino_ratio([0.01, 0.02, 0.03]))


def test_daily_returns_require_a_datetime_index():
    with pytest.raises(TypeError, match="DatetimeIndex"):
        daily_returns(pd.Series([100.0, 101.0]))


def test_backtest_and_live_reporting_publish_the_same_sharpe():
    returns = np.resize([0.01, -0.006, 0.004, -0.002, 0.008], 31)
    equity = pd.Series(
        10_000 * np.cumprod(np.r_[1.0, 1.0 + returns]),
        index=pd.date_range("2030-01-01", periods=32, freq="1D", tz="UTC"),
    )

    backtest = compute_metrics(equity, [], bars_per_year=365)
    live = live_metrics(equity, initial_capital=10_000)

    assert live["sharpe"] == pytest.approx(backtest["sharpe"])
    assert live["sortino"] == pytest.approx(backtest["sortino"])
    assert live["vol_annual"] == pytest.approx(backtest["volatility"])
