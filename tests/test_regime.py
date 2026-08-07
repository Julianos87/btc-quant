from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from btcquant.regime import AdaptiveRegimeConfig, adaptive_regime_frame


def _series(size: int = 800) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(7)
    returns = rng.normal(0.0002, 0.01, size)
    close = pd.Series(100 * np.exp(np.cumsum(returns)))
    adx = pd.Series(np.clip(25 + rng.normal(0, 5, size), 0, 100))
    return close, adx


def _config() -> AdaptiveRegimeConfig:
    return AdaptiveRegimeConfig(
        efficiency_bars=20,
        volatility_bars=20,
        reference_bars=120,
        smoothing_span=6,
        minimum_multiplier=0.35,
    )


def test_adaptive_regime_is_causal_when_future_prices_change():
    close, adx = _series()
    original = adaptive_regime_frame(close, adx, bars_per_year=2190, config=_config())
    changed = close.copy()
    changed.iloc[-10:] *= np.linspace(1.0, 1.5, 10)
    revised = adaptive_regime_frame(changed, adx, bars_per_year=2190, config=_config())

    assert_frame_equal(original.iloc[:-10], revised.iloc[:-10])


def test_adaptive_multiplier_is_bounded_and_warmup_is_explicit():
    close, adx = _series()
    result = adaptive_regime_frame(close, adx, bars_per_year=2190, config=_config())
    ready = result["adaptive_risk_multiplier"].dropna()

    assert not ready.empty
    assert ready.between(0.35, 1.0).all()
    assert set(result.loc[ready.index, "adaptive_regime"]) <= {
        "CHOP",
        "TRANSITION",
        "TREND",
        "STRESS",
    }
    assert (
        result.loc[result["adaptive_risk_multiplier"].isna(), "adaptive_regime"] == "WARMUP"
    ).all()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minimum_multiplier": 0.0},
        {"minimum_multiplier": 0.8, "maximum_multiplier": 0.7},
        {"maximum_multiplier": 1.1},
        {"reference_bars": 20, "efficiency_bars": 20},
        {"volatility_shock_ratio": 1.0},
    ],
)
def test_adaptive_regime_rejects_unsafe_configuration(kwargs):
    with pytest.raises(ValueError):
        AdaptiveRegimeConfig(**kwargs)
