"""Regression coverage for finite durable financial checkpoints."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

import pytest

from btcquant.execution.state_contract import validate_carry_state, validate_trend_state
from btcquant.execution.state_store import StateStore


def _trend_state() -> dict[str, Any]:
    return {
        "slots": {
            "d20": {
                "cash": 2_000.0,
                "entry_fee": -0.01,
                "position": {
                    "entry_time": "2026-09-11T00:00:00Z",
                    "entry_price": 50_000.0,
                    "qty": 0.01,
                    "stop_price": 49_000.0,
                    "direction": 1,
                    "bars_held": 0,
                    "best_close": 50_100.0,
                    "initial_qty": 0.01,
                    "last_add_price": 50_000.0,
                    "pyramid_adds": 0,
                },
            }
        },
        "peak_equity": 2_000.0,
        "day_start_equity": 2_000.0,
    }


def _carry_state() -> dict[str, Any]:
    return {
        "equity": 4_000.0,
        "in_position": True,
        "qty": 0.01,
        "spot_qty": 0.01,
        "perp_qty": -0.01,
        "entry_equity": 4_000.0,
        "entry_price": 50_000.0,
        "spot_notional": 500.0,
        "perp_notional": 500.0,
        "borrow_principal": 500.0,
        "funding_notional_price": 50_000.0,
        "peak_equity": 4_000.0,
        "day_start_equity": 4_000.0,
    }


@pytest.mark.parametrize(
    "field",
    ("entry_price", "qty", "stop_price", "best_close", "initial_qty", "last_add_price"),
)
@pytest.mark.parametrize("invalid", (math.nan, math.inf, -math.inf, True))
def test_trend_position_financial_fields_reject_non_finite_or_boolean(field, invalid):
    state = _trend_state()
    state["slots"]["d20"]["position"][field] = invalid

    with pytest.raises(ValueError, match=field):
        validate_trend_state(state)


@pytest.mark.parametrize("invalid", (math.nan, math.inf, -math.inf, True))
def test_trend_entry_fee_rejects_non_finite_or_boolean(invalid):
    state = _trend_state()
    state["slots"]["d20"]["entry_fee"] = invalid

    with pytest.raises(ValueError, match="entry_fee"):
        validate_trend_state(state)


def test_trend_allows_signed_finite_fee_and_negative_zero():
    state = _trend_state()
    state["slots"]["d20"]["entry_fee"] = -0.01
    state["slots"]["d20"]["position"]["best_close"] = -0.0

    assert validate_trend_state(state) is state


def test_trend_validates_each_nested_slot():
    state = _trend_state()
    state["slots"]["d50"] = deepcopy(state["slots"]["d20"])
    state["slots"]["d50"]["position"]["entry_price"] = math.nan

    with pytest.raises(ValueError, match=r"trend\.d50\.position\.entry_price"):
        validate_trend_state(state)


@pytest.mark.parametrize(
    "field",
    (
        "qty",
        "spot_qty",
        "perp_qty",
        "entry_equity",
        "entry_price",
        "spot_notional",
        "perp_notional",
        "borrow_principal",
        "funding_notional_price",
    ),
)
@pytest.mark.parametrize("invalid", (math.nan, math.inf, -math.inf, True))
def test_carry_financial_fields_reject_non_finite_or_boolean(field, invalid):
    state = _carry_state()
    state[field] = invalid

    with pytest.raises(ValueError, match=field):
        validate_carry_state(state)


def test_carry_preserves_optional_none_and_valid_zero_semantics():
    state = _carry_state()
    state.update(
        {
            "qty": 0.0,
            "spot_qty": 0.0,
            "perp_qty": 0.0,
            "entry_equity": None,
            "entry_price": None,
            "funding_notional_price": None,
            "spot_notional": 0.0,
            "perp_notional": 0.0,
            "borrow_principal": 0.0,
        }
    )

    assert validate_carry_state(state) is state


@pytest.mark.parametrize("invalid", (math.nan, math.inf, -math.inf))
def test_checkpoint_writer_rejects_non_finite_json_before_persistence(tmp_path, invalid):
    store = StateStore(tmp_path / "state.db")

    with pytest.raises(ValueError, match="Checkpoint financier invalide"):
        store.save_engine_state("trend", {"slots": {}, "cash": invalid})

    assert store.load_engine_state("trend") is None


def test_valid_checkpoint_round_trip_serialization_is_identical(tmp_path):
    database = tmp_path / "state.db"
    store = StateStore(database)
    state = _trend_state()

    store.save_engine_state("trend", state)

    assert store.load_engine_state("trend") == state


def test_non_finite_json_payload_fails_restore_contract():
    state = _trend_state()
    state["slots"]["d20"]["position"]["entry_price"] = math.nan
    restored_payload = json.loads(json.dumps(state))

    with pytest.raises(ValueError, match="entry_price"):
        validate_trend_state(restored_payload)
