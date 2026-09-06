"""Focused tests for the research-only Trend discovery switches."""

from __future__ import annotations

import pandas as pd

from scripts.run_strategy_discovery import (
    ABLATIONS,
    DEPLOYED,
    DiscoveryTrend,
    _candidate_space,
    _variant_params,
)


def _row(**overrides: object) -> pd.Series:
    values: dict[str, object] = {
        "close": 110.0,
        "atr": 2.0,
        "donchian_high": 100.0,
        "donchian_low": 90.0,
        "ema_fast": 95.0,
        "ema_slow": 100.0,
        "regime_up": False,
        "adx": 30.0,
        "funding": 0.0,
    }
    values.update(overrides)
    return pd.Series(values)


def test_discovery_ablation_space_is_small_and_explicit() -> None:
    assert len(ABLATIONS) == 13
    assert _variant_params("full") == DEPLOYED
    candidates = _candidate_space()
    assert len(candidates) == 8
    assert len({tuple(sorted(candidate.items())) for candidate in candidates}) == 8


def test_no_regime_variant_is_a_real_donchian_signal() -> None:
    strategy = DiscoveryTrend(**_variant_params("no_ema_regime"))
    # The EMA regime is down, but the channel breakout is long; removing only
    # the regime gate therefore exposes the long signal.
    assert strategy.entry_signal(_row()) == 1
    assert DiscoveryTrend(**DEPLOYED).entry_signal(_row()) == 0


def test_stop_and_exit_switches_are_explicit_and_causal() -> None:
    no_stop = DiscoveryTrend(**_variant_params("no_atr_stop"))
    fixed = DiscoveryTrend(**_variant_params("fixed_stop_8pct"))
    position = pd.Series(
        {
            "entry_price": 100.0,
        }
    )
    assert (
        no_stop.trailing_stop(
            _row(),
            type(
                "P",
                (),
                {
                    "best_close": 100.0,
                    "direction": 1,
                    "stop_price": 90.0,
                },
            )(),
        )
        is None
    )
    assert fixed.initial_stop(_row(), float(position["entry_price"]), 1) == 92.0
    channel = DiscoveryTrend(**_variant_params("donchian_only"))
    assert channel.exit_signal(_row(close=85.0), type("P", (), {"direction": 1})()) is True
