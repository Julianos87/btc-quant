from __future__ import annotations

import threading
from types import SimpleNamespace

from btcquant.execution.carry_runner import CarryRunner
from btcquant.execution.runner import LiveRunner


def test_live_runner_checkpoints_when_stop_is_already_requested():
    runner = LiveRunner.__new__(LiveRunner)
    runner.symbol = "BTC/USDT"
    runner.slots = []
    runner.broker = SimpleNamespace(supports_stop_orders=False)
    checkpoints: list[str] = []
    runner._save_state = lambda: checkpoints.append("state")
    runner._append_equity = lambda _price: checkpoints.append("equity")
    stop = threading.Event()
    stop.set()

    runner.run_forever(stop)

    assert checkpoints == ["state"]


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
