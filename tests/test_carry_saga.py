"""Saga d'exécution des deux jambes du carry."""

from __future__ import annotations

import pytest
import pandas as pd

from dataclasses import replace

from btcquant.carry import PAPER_CARRY_POLICY
from btcquant.execution.carry_contract import (
    CarrySagaResult,
    CarrySagaStatus,
)
from btcquant.execution.carry_runner import CarryRunner


def _fast_policy():
    """Lissage d'un jour : les scénarios de saga tiennent en quelques paiements."""

    return replace(PAPER_CARRY_POLICY, smooth_days=1)


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
        [0.001] * 25,
        index=pd.date_range("2026-01-01", periods=25, freq="h", tz="UTC"),
    )


def test_runner_persists_opening_before_calling_external_broker(tmp_path, monkeypatch):
    database = tmp_path / "btcquant.db"
    live = RunnerBrokerStub(error=TimeoutError("response lost"))
    runner = CarryRunner(state_file=database, live_broker=live, policy=_fast_policy())
    monkeypatch.setattr(runner, "_recent_funding", positive_funding)

    with pytest.raises(TimeoutError, match="response lost"):
        runner._tick()

    state = runner.store.load_engine_state("carry")
    assert state is not None and state["execution_state"] == "OPENING"
    assert runner.store.pending_orders("carry")
    with pytest.raises(RuntimeError, match="indéterminé"):
        CarryRunner(state_file=database, live_broker=live, policy=_fast_policy())


def test_runner_accepts_only_a_balanced_partial_open(tmp_path, monkeypatch):
    result = CarrySagaResult(
        CarrySagaStatus.PARTIAL,
        spot_qty=0.6,
        perp_qty=0.6,
    )
    runner = CarryRunner(
        state_file=tmp_path / "btcquant.db",
        live_broker=RunnerBrokerStub(result=result),
        policy=_fast_policy(),
    )
    monkeypatch.setattr(runner, "_recent_funding", positive_funding)

    runner._tick()

    assert runner.execution_state == "OPEN"
    assert runner.in_position
    assert runner.qty == pytest.approx(0.6)
    assert runner.store.read_orders("carry")[0]["status"] == "PARTIAL"
