from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from btcquant.execution.carry_runner import CarryRunner
from btcquant.execution.errors import ReconciliationRequired
from btcquant.execution.runner import LiveRunner


def test_live_runner_checkpoints_when_stop_is_already_requested(tmp_path):
    runner = LiveRunner.__new__(LiveRunner)
    runner.symbol = "BTC/USDT"
    runner.slots = []
    runner.broker = SimpleNamespace(supports_stop_orders=False)
    runner.store = SimpleNamespace(path=tmp_path / "state.db")
    checkpoints: list[str] = []
    runner._save_state = lambda: checkpoints.append("state")
    runner._append_equity = lambda _price: checkpoints.append("equity")
    stop = threading.Event()
    stop.set()

    runner.run_forever(stop)

    assert checkpoints == ["state"]


def test_live_runner_never_checkpoints_mutated_memory_after_ambiguous_exit(tmp_path):
    runner = LiveRunner.__new__(LiveRunner)
    runner.symbol = "BTC/USDT"
    runner.slots = []
    runner.store = SimpleNamespace(path=tmp_path / "state.db")
    checkpoints: list[str] = []
    runner._save_state = lambda: checkpoints.append("state")
    runner._append_equity = lambda _price: checkpoints.append("equity")
    runner._prepare_external_execution = lambda: None
    runner._last_price = lambda: 100.0
    runner._run_cycle = lambda _price, _stop: (_ for _ in ()).throw(
        ReconciliationRequired("ambiguous fill")
    )

    with pytest.raises(ReconciliationRequired, match="ambiguous fill"):
        runner.run_forever(threading.Event())

    assert checkpoints == []


def test_carry_runner_checkpoints_and_records_equity_on_stop():
    runner = CarryRunner.__new__(CarryRunner)
    runner.live_broker = None
    runner.symbol = "BTC/USDT:USDT"
    runner.leverage = 3.0
    runner.enter_ann = 0.03
    runner.exit_ann = 0.0
    checkpoints: list[str] = []
    runner._save_state = lambda: checkpoints.append("state")
    runner._append_equity = lambda: checkpoints.append("equity")
    stop = threading.Event()
    stop.set()

    runner.run_forever(stop)

    assert checkpoints == ["state", "equity"]
