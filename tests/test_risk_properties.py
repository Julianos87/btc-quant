from __future__ import annotations

import math

from hypothesis import given, strategies as st

from btcquant.risk import RiskConfig, position_size

finite_positive = st.floats(
    min_value=1e-3,
    max_value=1e9,
    allow_nan=False,
    allow_infinity=False,
)


@given(
    equity=finite_positive,
    entry=st.floats(min_value=1.0, max_value=1e7, allow_nan=False, allow_infinity=False),
    distance=st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False),
    risk_fraction=st.floats(
        min_value=1e-5,
        max_value=0.25,
        allow_nan=False,
        allow_infinity=False,
    ),
    max_position=st.floats(
        min_value=1e-3,
        max_value=1.0,
        allow_nan=False,
        allow_infinity=False,
    ),
    leverage=st.floats(
        min_value=0.1,
        max_value=10.0,
        allow_nan=False,
        allow_infinity=False,
    ),
)
def test_position_size_never_exceeds_risk_or_notional_caps(
    equity, entry, distance, risk_fraction, max_position, leverage
):
    config = RiskConfig(
        initial_capital=equity,
        risk_per_trade=risk_fraction,
        max_position_pct=max_position,
        vol_target_annual=None,
        max_leverage=leverage,
    )

    qty = position_size(equity, entry, entry - distance, None, config)

    assert math.isfinite(qty)
    assert qty >= 0
    assert qty * distance <= equity * risk_fraction * (1 + 1e-10)
    assert qty * entry <= equity * max_position * leverage * (1 + 1e-10)


@given(
    equity=finite_positive,
    entry=st.floats(min_value=1.0, max_value=1e7, allow_nan=False, allow_infinity=False),
    distance=st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False),
)
def test_long_and_short_sizing_are_symmetric(equity, entry, distance):
    config = RiskConfig(initial_capital=equity, vol_target_annual=None, max_leverage=3.0)

    long_qty = position_size(equity, entry, entry - distance, None, config, direction=1)
    short_qty = position_size(equity, entry, entry + distance, None, config, direction=-1)

    assert math.isclose(long_qty, short_qty, rel_tol=1e-14)
