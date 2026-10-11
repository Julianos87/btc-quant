from __future__ import annotations

import pytest

from scripts.diagnose_carry_cash import diagnostic


def test_fixed_cash_diagnostic_uses_asymmetric_leg_costs() -> None:
    report = diagnostic()
    assert report["selection_performed"] is False
    window = report["scenarios"]["cash_funded"]["windows"][0]
    plan = window["plan"]
    spot_rate = plan["spot_fee_rate"] + 5 / 10_000
    perp_rate = plan["perp_fee_rate"] + 5 / 10_000
    expected_entry_cost = plan["spot_notional"] * spot_rate + plan["perp_notional"] * perp_rate
    assert plan["spot_fee_rate"] == 0.001
    assert plan["perp_fee_rate"] == 0.0005
    assert plan["estimated_entry_cost"] == pytest.approx(expected_entry_cost)
