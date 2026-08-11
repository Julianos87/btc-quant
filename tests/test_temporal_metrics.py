from __future__ import annotations

import numpy as np
import pandas as pd

from btcquant.backtest.metrics import compute_metrics


def test_cagr_uses_calendar_duration_not_observation_count() -> None:
    short_span = pd.date_range("2030-01-01", periods=1000, freq="1h", tz="UTC")
    long_span = pd.date_range("2030-01-01", periods=1000, freq="2h", tz="UTC")
    short_equity = pd.Series(np.linspace(100.0, 200.0, len(short_span)), index=short_span)
    long_equity = pd.Series(np.linspace(100.0, 200.0, len(long_span)), index=long_span)

    short = compute_metrics(short_equity, [], bars_per_year=8760)
    long = compute_metrics(long_equity, [], bars_per_year=8760)

    assert long["elapsed_years"] > short["elapsed_years"]
    assert long["cagr"] < short["cagr"]
    assert short["observed_points"] == long["observed_points"] == 1000


def test_daily_sharpe_contract_does_not_impute_missing_calendar_days() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2030-01-01T00:00:00Z"),
            pd.Timestamp("2030-01-02T00:00:00Z"),
            pd.Timestamp("2030-01-05T00:00:00Z"),
        ]
    )
    equity = pd.Series([100.0, 101.0, 103.0], index=index)

    metrics = compute_metrics(equity, [], bars_per_year=365)

    assert metrics["return_frequency"] == "calendar_day_close"
    assert metrics["missing_calendar_days"] == 2
    assert metrics["annualization_periods_per_year"] == 365.0
    assert metrics["data_gaps_imputed"] is False
