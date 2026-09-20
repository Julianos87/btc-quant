from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from btcquant.execution.broker import Fill, PaperBroker
from btcquant.execution.errors import ReconciliationRequired
from btcquant.execution.order_service import OrderExecutionService
from btcquant.execution.state_store import StateStore


class CountingBroker(PaperBroker):
    def __init__(
        self, *, started: threading.Event | None = None, release: threading.Event | None = None
    ):
        super().__init__()
        self.calls = 0
        self._lock = threading.Lock()
        self.started = started
        self.release = release

    def execute_market(self, side, qty, ref_price, *, client_order_id=None, **kwargs):
        del side, kwargs
        with self._lock:
            self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            assert self.release.wait(5)
        return Fill(price=ref_price, qty=qty, fee=0.0, broker_order_id="paper-1", status="FILLED")


def trend_state() -> dict:
    return {
        "slots": {
            "strategy": {
                "cash": 1_000.0,
                "position": None,
                "position_cycle_id": "cycle-1",
                "active_transition": None,
            }
        },
        "peak_equity": 1_000.0,
        "halted": False,
        "day": None,
        "day_start_equity": 1_000.0,
        "daily_lockout": False,
        "reconciliation_required": False,
        "last_funding_ts": None,
    }


def submit(service: OrderExecutionService, *, intent_id: str, state=None, transition=None):
    return service.submit_market(
        engine="trend",
        slot="strategy",
        side="BUY",
        qty=1.0,
        reference_price=100.0,
        reason="entry",
        intent_id=intent_id,
        state=state,
        transition=transition,
    )


def test_same_intent_never_calls_broker_twice(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = CountingBroker()
    service = OrderExecutionService(store, broker)

    submit(service, intent_id="stable-intent")
    with pytest.raises(ReconciliationRequired):
        submit(service, intent_id="stable-intent")

    assert broker.calls == 1
    assert len(store.read_orders("trend")) == 1


def test_transition_reservation_is_atomic_and_replay_is_blocked(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = CountingBroker()
    service = OrderExecutionService(store, broker)
    state = trend_state()
    transition = {
        "kind": "ENTRY",
        "phase": "SUBMITTING",
        "intent_id": "trend-strategy-entry-stable",
        "position_cycle_id": "cycle-1",
    }

    submit(service, intent_id="ignored", state=state, transition=transition)
    persisted = store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["slots"]["strategy"]["active_transition"] == transition

    with pytest.raises(ReconciliationRequired):
        submit(service, intent_id="ignored", state=state, transition=transition)

    persisted = store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["reconciliation_required"] is True
    assert broker.calls == 1


def test_concurrent_same_intent_has_one_broker_call(tmp_path):
    store = StateStore(tmp_path / "state.db")
    started = threading.Event()
    release = threading.Event()
    broker = CountingBroker(started=started, release=release)
    service = OrderExecutionService(store, broker)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(submit, service, intent_id="concurrent-intent")
        assert started.wait(5)
        second = executor.submit(submit, service, intent_id="concurrent-intent")
        with pytest.raises(ReconciliationRequired):
            second.result(timeout=5)
        release.set()
        first.result(timeout=5)

    assert broker.calls == 1
    assert len(store.read_orders("trend")) == 1
