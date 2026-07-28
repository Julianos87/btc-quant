"""Contrats partagés des checkpoints moteurs."""

from __future__ import annotations

import pytest

from btcquant.execution.state_contract import validate_carry_state, validate_trend_state


def test_minimal_valid_trend_state():
    state = validate_trend_state(
        {
            "slots": {
                "d20": {
                    "cash": 2_000.0,
                    "position": None,
                }
            }
        }
    )
    assert state["slots"]["d20"]["cash"] == 2_000.0


def test_trend_rejects_position_missing_a_risk_field():
    with pytest.raises(ValueError, match="stop_price"):
        validate_trend_state(
            {
                "slots": {
                    "d20": {
                        "cash": 2_000.0,
                        "position": {
                            "entry_time": "2026-01-01T00:00:00Z",
                            "entry_price": 100.0,
                            "qty": 1.0,
                            "direction": 1,
                            "bars_held": 0,
                            "best_close": 100.0,
                        },
                    }
                }
            }
        )


def test_carry_requires_equity_and_position_flag():
    assert validate_carry_state({"equity": 4_000.0, "in_position": False})["equity"] == 4_000
    with pytest.raises(ValueError, match="in_position"):
        validate_carry_state({"equity": 4_000.0})
    with pytest.raises(ValueError, match="equity"):
        validate_carry_state({"equity": "4000", "in_position": False})
