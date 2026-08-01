from __future__ import annotations

import pytest

from btcquant.carry import CarryPolicy
from scripts.research_carry_net_edge import raw_funding_breakeven, raw_funding_threshold


def test_net_carry_threshold_covers_borrow_round_trip_and_margin():
    policy = CarryPolicy(
        leverage=3.0,
        borrow_rate_ann=0.10,
        fee_rate=0.0005,
        slippage_bps=5.0,
    )

    threshold = raw_funding_threshold(
        policy,
        expected_holding_days=180,
        minimum_net_return_ann=0.03,
    )

    assert raw_funding_breakeven(policy) == pytest.approx(0.20 / 3.0)
    assert threshold == pytest.approx((0.20 + 0.012 * 365 / 180 + 0.03) / 3.0)
    assert threshold > raw_funding_breakeven(policy)
