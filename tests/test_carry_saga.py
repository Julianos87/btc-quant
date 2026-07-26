"""Saga d'exécution des deux jambes du carry."""

from __future__ import annotations

import pytest
import pandas as pd

from btcquant.execution.carry_broker import (
    CarryBroker,
    CarrySagaResult,
    CarrySagaStatus,
)
from btcquant.execution.carry_runner import CarryRunner


class FakeExchange:
    def __init__(self, fills, *, precision: int = 6, price: float = 100.0):
        self.fills = list(fills)
        self.precision = precision
        self.price = price
        self.calls = []

    def amount_to_precision(self, symbol, qty):
        factor = 10**self.precision
        return int(float(qty) * factor) / factor

    def fetch_ticker(self, symbol):
        return {"last": self.price}

    def create_order(self, symbol, order_type, side, qty, price, params):
        self.calls.append((symbol, order_type, side, qty, params))
        outcome = self.fills.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {
            "id": f"order-{len(self.calls)}",
            "status": "closed",
            "filled": outcome,
            "average": self.price,
        }


def broker(spot_fills, perp_fills, *, spot_precision=6, perp_precision=6):
    instance = object.__new__(CarryBroker)
    instance.symbol = "BTC/USDT"
    instance.perp_symbol = "BTC/USDT:USDT"
    instance.spot = FakeExchange(spot_fills, precision=spot_precision)
    instance.perp = FakeExchange(perp_fills, precision=perp_precision)
    return instance


def test_open_waits_for_both_actual_fills():
    instance = broker([1.0], [1.0])

    result = instance.open_position(100.0, intent_id="intent-1")

    assert result.status == CarrySagaStatus.FILLED
    assert result.is_balanced
    assert result.neutral_qty == pytest.approx(1.0)
    assert result.spot_fill is not None and result.spot_fill.filled_qty == 1.0
    assert result.perp_fill is not None and result.perp_fill.filled_qty == 1.0
    assert instance.spot.calls[0][4]["newClientOrderId"] == CarryBroker._coid("intent-1", "spotbuy")


def test_open_uses_a_quantity_valid_on_both_markets():
    instance = broker(
        [1.234],
        [1.234],
        spot_precision=3,
        perp_precision=4,
    )

    result = instance.open_position(123.456, intent_id="precision")

    assert result.status == CarrySagaStatus.FILLED
    assert instance.spot.calls[0][3] == pytest.approx(1.234)
    assert instance.perp.calls[0][3] == pytest.approx(1.234)


def test_partial_perp_is_compensated_to_a_smaller_neutral_position():
    instance = broker([1.0, 0.4], [0.6])

    result = instance.open_position(100.0, intent_id="partial")

    assert result.status == CarrySagaStatus.PARTIAL
    assert result.spot_qty == pytest.approx(0.6)
    assert result.perp_qty == pytest.approx(0.6)
    assert result.compensation_fill is not None
    assert instance.spot.calls[1][2] == "sell"


def test_failed_compensation_exposes_unbalanced_state():
    instance = broker([1.0, RuntimeError("unwind failed")], [0.0])

    result = instance.open_position(100.0, intent_id="unbalanced")

    assert result.status == CarrySagaStatus.UNBALANCED
    assert result.spot_qty == pytest.approx(1.0)
    assert result.perp_qty == pytest.approx(0.0)
    assert "unwind failed" in (result.error or "")


def test_partial_close_keeps_a_smaller_balanced_position():
    instance = broker([0.6], [0.6])

    result = instance.close_position(1.0, intent_id="close-partial")

    assert result.status == CarrySagaStatus.PARTIAL
    assert result.spot_qty == pytest.approx(0.4)
    assert result.perp_qty == pytest.approx(0.4)
    assert result.is_balanced


def test_spot_close_failure_is_never_reported_as_flat():
    instance = broker([RuntimeError("spot unavailable")], [1.0])

    result = instance.close_position(1.0, intent_id="close-broken")

    assert result.status == CarrySagaStatus.UNBALANCED
    assert result.spot_qty == pytest.approx(1.0)
    assert result.perp_qty == pytest.approx(0.0)


class RunnerBrokerStub:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def reconcile(self):
        return True

    def open_position(self, notional, *, intent_id):
        if self.error is not None:
            raise self.error
        return self.result


def positive_funding():
    return pd.Series(
        [0.001],
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01", tz="UTC")]),
    )


def test_runner_persists_opening_before_calling_external_broker(tmp_path, monkeypatch):
    database = tmp_path / "btcquant.db"
    live = RunnerBrokerStub(error=TimeoutError("response lost"))
    runner = CarryRunner(state_file=database, live_broker=live, smooth_days=1)
    monkeypatch.setattr(runner, "_recent_funding", positive_funding)

    with pytest.raises(TimeoutError, match="response lost"):
        runner._tick()

    state = runner.store.load_engine_state("carry")
    assert state is not None and state["execution_state"] == "OPENING"
    assert runner.store.pending_orders("carry")
    with pytest.raises(RuntimeError, match="indéterminé"):
        CarryRunner(state_file=database, live_broker=live, smooth_days=1)


def test_runner_accepts_only_a_balanced_partial_open(tmp_path, monkeypatch):
    result = CarrySagaResult(
        CarrySagaStatus.PARTIAL,
        spot_qty=0.6,
        perp_qty=0.6,
    )
    runner = CarryRunner(
        state_file=tmp_path / "btcquant.db",
        live_broker=RunnerBrokerStub(result=result),
        smooth_days=1,
    )
    monkeypatch.setattr(runner, "_recent_funding", positive_funding)

    runner._tick()

    assert runner.execution_state == "OPEN"
    assert runner.in_position
    assert runner.qty == pytest.approx(0.6)
    assert runner.store.read_orders("carry")[0]["status"] == "PARTIAL"
