from __future__ import annotations

import copy
import multiprocessing as mp
import sqlite3
import threading

import pytest

from btcquant.execution.broker import Broker, BrokerOrderSnapshot, Fill, PaperBroker
from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.errors import NonTerminalOrder, ReconciliationRequired
from btcquant.execution.order_service import OrderExecutionService
from btcquant.execution.recovery import recover_interrupted_orders
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.execution.state_store import StateStore
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Strategy


def _trend_state(*, position: dict | None = None, cycle: str | None = None) -> dict:
    return {
        "slots": {
            "strategy": {
                "cash": 1_000.0,
                "position": position,
                "position_cycle_id": cycle,
                "active_transition": None,
                "stop_order_id": None,
                "stop_order_local_id": None,
                "stop_intent_id": None,
                "stop_transition": None,
                "entry_fee": 0.0,
                "last_bar_ts": None,
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


def _position(qty: float = 1.0) -> dict:
    return {
        "entry_time": "2026-01-01T00:00:00+00:00",
        "entry_price": 100.0,
        "qty": qty,
        "stop_price": 90.0,
        "direction": 1,
        "bars_held": 0,
        "best_close": 100.0,
        "initial_qty": 1.0,
        "last_add_price": 100.0,
        "pyramid_adds": 0,
    }


def _transition(kind: str, intent: str, cycle: str) -> dict:
    return {
        "kind": kind,
        "phase": "SUBMITTING",
        "intent_id": intent,
        "position_cycle_id": cycle,
        "side": "BUY" if kind in {"ENTRY", "PYRAMID"} else "SELL",
        "requested_qty": 1.0,
        "reference_price": 100.0,
        "reason": kind.lower(),
    }


class AuditedBroker(Broker):
    is_paper = True

    def __init__(
        self,
        *,
        result: Fill | None = None,
        error: Exception | None = None,
        calls: list[str] | None = None,
    ) -> None:
        self.result = result or Fill(100.0, 1.0, 0.0, status="FILLED")
        self.error = error
        self.calls = calls if calls is not None else []
        self._lock = threading.Lock()

    def market_buy(self, qty: float, ref_price: float) -> Fill:
        return self.execute_market("BUY", qty, ref_price)

    def market_sell(self, qty: float, ref_price: float) -> Fill:
        return self.execute_market("SELL", qty, ref_price)

    def execute_market(self, side, qty, ref_price, *, client_order_id=None, **kwargs):
        del side, qty, ref_price, kwargs
        with self._lock:
            self.calls.append(str(client_order_id))
        if self.error is not None:
            raise self.error
        return self.result


class ExternalAuditedBroker(AuditedBroker):
    is_paper = False

    def __init__(
        self,
        *,
        result: Fill | None = None,
        error: Exception | None = None,
        supports_lookup: bool = True,
        snapshot: BrokerOrderSnapshot | None = None,
    ) -> None:
        super().__init__(result=result, error=error)
        self.supports_order_lookup = supports_lookup
        self.snapshot = snapshot

    def lookup_order(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        if self.snapshot is None:
            return None
        return BrokerOrderSnapshot(
            client_order_id=client_order_id,
            broker_order_id=self.snapshot.broker_order_id,
            status=self.snapshot.status,
            filled_qty=self.snapshot.filled_qty,
            price=self.snapshot.price,
            fee=self.snapshot.fee,
        )


class ProcessCountingBroker(Broker):
    is_paper = True

    def __init__(self, calls, barrier) -> None:
        self.calls = calls
        self.barrier = barrier

    def market_buy(self, qty: float, ref_price: float) -> Fill:
        return Fill(ref_price, qty, 0.0, status="FILLED")

    def market_sell(self, qty: float, ref_price: float) -> Fill:
        return Fill(ref_price, qty, 0.0, status="FILLED")

    def execute_market(self, side, qty, ref_price, *, client_order_id=None, **kwargs):
        del side, kwargs
        with self.calls.get_lock():
            self.calls.value += 1
        return Fill(ref_price, qty, 0.0, broker_order_id="process-order", status="FILLED")


def _process_submit(db_path: str, calls, barrier, result_queue) -> None:
    try:
        barrier.wait(timeout=10)
        store = StateStore(db_path, initialize=False)
        service = OrderExecutionService(store, ProcessCountingBroker(calls, barrier))
        service.submit_market(
            engine="trend",
            slot="strategy",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            intent_id="process-shared-intent",
        )
    except ReconciliationRequired:
        result_queue.put("blocked")
    except BaseException as error:
        result_queue.put(f"error:{type(error).__name__}:{error}")
    else:
        result_queue.put("sent")


def test_real_multiprocess_same_intent_has_one_broker_call(tmp_path):
    store = StateStore(tmp_path / "state.db")
    del store
    context = mp.get_context("fork")
    calls = context.Value("i", 0)
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_process_submit,
            args=(str(tmp_path / "state.db"), calls, barrier, result_queue),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    results = [result_queue.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(results) == ["blocked", "sent"]
    assert calls.value == 1
    assert len(StateStore(tmp_path / "state.db").read_orders("trend")) == 1


def test_distinct_intent_is_blocked_while_same_slot_is_unresolved(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = AuditedBroker()
    service = OrderExecutionService(store, broker)

    service.submit_market(
        engine="trend",
        slot="strategy",
        side="BUY",
        qty=1.0,
        reference_price=100.0,
        reason="entry",
        intent_id="first-intent",
    )
    with pytest.raises(ReconciliationRequired):
        service.submit_market(
            engine="trend",
            slot="strategy",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            intent_id="second-intent",
        )

    assert broker.calls == ["first-intent"]


@pytest.mark.parametrize(
    ("filled_qty", "expected_status", "raw_status"),
    [
        (0.0, "PENDING", "UNKNOWN"),
        (1.0, "UNBALANCED", "UNKNOWN"),
        (0.0, "PENDING", None),
        (1.0, "UNBALANCED", None),
    ],
)
def test_unknown_external_status_is_reconciled_not_rejected(
    tmp_path, filled_qty, expected_status, raw_status
):
    store = StateStore(tmp_path / "state.db")
    broker = ExternalAuditedBroker(
        result=Fill(
            price=100.0,
            qty=filled_qty,
            fee=0.1 if filled_qty else 0.0,
            broker_order_id="remote-unknown",
            status=raw_status,
        )
    )
    service = OrderExecutionService(store, broker)

    with pytest.raises(NonTerminalOrder):
        service.submit_market(
            engine="trend",
            slot="strategy",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            intent_id="unknown-status",
        )

    order = store.read_orders("trend")[0]
    assert order["status"] == expected_status
    assert order["filled_qty"] == pytest.approx(filled_qty)
    assert store.load_engine_state("trend") is None
    assert store.read_incidents(open_only=True)[0]["kind"] == "order_ambiguous"


def test_external_filled_without_quantity_stays_ambiguous(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = ExternalAuditedBroker(
        result=Fill(
            price=100.0,
            qty=0.0,
            fee=0.0,
            broker_order_id="remote-inconsistent",
            status="FILLED",
        )
    )
    service = OrderExecutionService(store, broker)

    with pytest.raises(NonTerminalOrder):
        service.submit_market(
            engine="trend",
            slot="strategy",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            intent_id="inconsistent-filled",
        )

    assert store.read_orders("trend")[0]["status"] == "PENDING"
    assert store.read_incidents(open_only=True)[0]["kind"] == "order_ambiguous"


@pytest.mark.parametrize(
    ("broker_status", "expected_status"),
    [("FILLED", "PARTIAL"), ("CANCELED", "PARTIAL"), ("REJECTED", "PARTIAL")],
)
def test_positive_partial_fill_is_never_promoted_to_filled(
    tmp_path, broker_status, expected_status
):
    store = StateStore(tmp_path / "state.db")
    broker = ExternalAuditedBroker(
        result=Fill(
            price=100.0,
            qty=0.4,
            fee=0.1,
            broker_order_id="remote-partial",
            status=broker_status,
        )
    )

    submitted = OrderExecutionService(store, broker).submit_market(
        engine="trend",
        slot="strategy",
        side="BUY",
        qty=1.0,
        reference_price=100.0,
        reason="entry",
        intent_id=f"partial-{broker_status.lower()}",
    )

    assert submitted.status == expected_status
    assert submitted.fill.qty == pytest.approx(0.4)


def test_external_without_lookup_stays_ambiguous_after_timeout(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = ExternalAuditedBroker(
        error=TimeoutError("response lost"),
        supports_lookup=False,
    )
    service = OrderExecutionService(store, broker)

    with pytest.raises(TimeoutError):
        service.submit_market(
            engine="trend",
            slot="strategy",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            intent_id="no-lookup-timeout",
        )

    order = store.read_orders("trend")[0]
    assert order["status"] == "PENDING"
    assert store.load_engine_state("trend") is None

    report = recover_interrupted_orders(store, broker, "trend", external=True)
    assert not report.can_start
    assert store.read_orders("trend")[0]["status"] == "UNBALANCED"


def test_recovery_rejects_mismatched_paper_external_mode(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.begin_order("trend", "strategy", "mode-check", "MARKET", "BUY", 1.0, "entry")

    with pytest.raises(ValueError, match="mode de recovery"):
        recover_interrupted_orders(store, PaperBroker(), "trend", external=True)
    with pytest.raises(ValueError, match="mode de recovery"):
        recover_interrupted_orders(store, ExternalAuditedBroker(), "trend", external=False)


@pytest.mark.parametrize(
    ("raw_status", "filled", "amount", "expected"),
    [
        ("closed", 1.0, 1.0, "FILLED"),
        ("closed", 0.4, 1.0, "PARTIAL"),
        ("closed", 0.0, 1.0, "REJECTED"),
        ("open", 0.0, 1.0, "OPEN"),
        ("new", 0.0, 1.0, "OPEN"),
        ("canceled", 0.4, 1.0, "CANCELED"),
        ("cancelled", 0.0, 1.0, "CANCELED"),
        ("rejected", 0.0, 1.0, "REJECTED"),
        ("expired", 0.0, 1.0, "REJECTED"),
        ("unknown", 1.0, 1.0, "UNKNOWN"),
        (None, 1.0, 1.0, "UNKNOWN"),
    ],
)
def test_ccxt_status_normalization_is_conservative(raw_status, filled, amount, expected):
    broker = object.__new__(CcxtBroker)
    fill = broker._fill_from_order(
        {
            "id": "exchange-order",
            "status": raw_status,
            "average": 100.0,
            "filled": filled,
            "amount": amount,
            "fees": [],
        },
        100.0,
    )
    assert fill.status == expected


def test_stale_entry_snapshot_is_rejected_before_broker(tmp_path):
    store = StateStore(tmp_path / "state.db")
    stale = _trend_state()
    store.save_engine_state("trend", stale)

    fresh = _trend_state(position=_position(), cycle="cycle-1")
    store.save_engine_state("trend", fresh)

    broker = AuditedBroker()
    service = OrderExecutionService(store, broker)
    with pytest.raises(ReconciliationRequired):
        service.submit_market(
            engine="trend",
            slot="strategy",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            state=stale,
            transition=_transition("ENTRY", "stale-entry", "cycle-1"),
        )

    assert broker.calls == []
    assert store.read_orders("trend") == []


def test_stale_checkpoint_cannot_erase_an_active_transition(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = AuditedBroker()
    service = OrderExecutionService(store, broker)
    state = _trend_state()
    transition = _transition("ENTRY", "preserved-transition", "cycle-1")

    service.submit_market(
        engine="trend",
        slot="strategy",
        side="BUY",
        qty=1.0,
        reference_price=100.0,
        reason="entry",
        state=state,
        transition=transition,
    )
    store.save_engine_state("trend", state)

    persisted = store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["slots"]["strategy"]["active_transition"] == transition
    assert store.read_orders("trend")[0]["status"] == "PENDING"
    assert broker.calls == ["preserved-transition"]


def test_reused_intent_cannot_cross_slot_or_attach_wrong_transition(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = AuditedBroker()
    service = OrderExecutionService(store, broker)
    state = _trend_state()
    state["slots"]["other"] = copy.deepcopy(state["slots"]["strategy"])
    first_transition = _transition("ENTRY", "cross-slot-intent", "cycle-1")

    service.submit_market(
        engine="trend",
        slot="strategy",
        side="BUY",
        qty=1.0,
        reference_price=100.0,
        reason="entry",
        state=state,
        transition=first_transition,
    )

    with pytest.raises(ReconciliationRequired, match="Collision d'intention"):
        service.submit_market(
            engine="trend",
            slot="other",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            state=state,
            transition=first_transition,
        )

    persisted = store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["slots"]["strategy"]["active_transition"]["intent_id"] == "cross-slot-intent"
    assert persisted["slots"]["other"]["active_transition"] is None
    assert broker.calls == ["cross-slot-intent"]


def test_database_reservation_failure_is_fail_closed(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.db")
    broker = AuditedBroker()

    def fail_reservation(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "reserve_order", fail_reservation)
    service = OrderExecutionService(store, broker)

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        service.submit_market(
            engine="trend",
            slot="strategy",
            side="BUY",
            qty=1.0,
            reference_price=100.0,
            reason="entry",
            intent_id="db-failure",
        )

    assert broker.calls == []


class RestartStrategy(Strategy):
    name = "restart"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {}

    def prepare(self, frame):
        return frame

    def entry_signal(self, row) -> int:
        return 0

    def initial_stop(self, row, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * 10.0


def _risk() -> RiskConfig:
    return RiskConfig(
        initial_capital=1_000.0,
        risk_per_trade=0.01,
        max_position_pct=0.95,
        vol_target_annual=None,
        max_drawdown_halt=0.5,
        daily_loss_limit=None,
    )


def test_runner_recovery_can_resolve_reconciliation_flag_after_confirmed_absence(tmp_path):
    database = tmp_path / "state.db"
    store = StateStore(database)
    state = _trend_state()
    state["slots"] = {
        "restart": copy.deepcopy(state["slots"]["strategy"]),
    }
    store.save_engine_state("trend", state)
    order_id = store.begin_order(
        "trend", "restart", "restart-intent", "MARKET", "BUY", 1.0, "entry"
    )
    store.mark_order_ambiguous_and_checkpoint(
        order_id,
        engine="trend",
        state=state,
        error="response lost",
    )

    broker = ExternalAuditedBroker(supports_lookup=True)
    runner = LiveRunner(
        [StrategySlot(RestartStrategy(), 1.0, 1_000.0)],
        broker,
        _risk(),
        "binance",
        "BTC/USDT",
        database,
    )

    assert runner.store.read_orders("trend")[0]["status"] == "RECOVERED_ABORTED"
    persisted = runner.store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["reconciliation_required"] is False
    assert runner.store.read_incidents(open_only=True) == []


def test_position_cycle_is_distinct_and_old_intent_cannot_reenter(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = AuditedBroker()
    service = OrderExecutionService(store, broker)

    flat = _trend_state()
    entry = _transition("ENTRY", "cycle-1-entry", "cycle-1")
    first = service.submit_market(
        engine="trend",
        slot="strategy",
        side="BUY",
        qty=1.0,
        reference_price=100.0,
        reason="entry",
        state=flat,
        transition=entry,
    )
    open_state = _trend_state(position=_position(), cycle="cycle-1")
    store.complete_order_and_checkpoint(
        first.order_id,
        engine="trend",
        state=open_state,
        status="FILLED",
        filled_qty=1.0,
        price=100.0,
    )

    pyramid = _transition("PYRAMID", "cycle-1-pyramid-1", "cycle-1")
    second = service.submit_market(
        engine="trend",
        slot="strategy",
        side="BUY",
        qty=1.0,
        reference_price=101.0,
        reason="pyramid",
        state=open_state,
        transition=pyramid,
    )
    open_after_pyramid = _trend_state(position=_position(2.0), cycle="cycle-1")
    store.complete_order_and_checkpoint(
        second.order_id,
        engine="trend",
        state=open_after_pyramid,
        status="FILLED",
        filled_qty=1.0,
        price=101.0,
    )

    partial_exit = _transition("EXIT", "cycle-1-exit-partial", "cycle-1")
    third = service.submit_market(
        engine="trend",
        slot="strategy",
        side="SELL",
        qty=1.0,
        reference_price=102.0,
        reason="exit",
        state=open_after_pyramid,
        transition=partial_exit,
    )
    remaining = _trend_state(position=_position(1.0), cycle="cycle-1")
    store.complete_order_and_checkpoint(
        third.order_id,
        engine="trend",
        state=remaining,
        status="PARTIAL",
        filled_qty=1.0,
        price=102.0,
    )

    final_exit = _transition("EXIT", "cycle-1-exit-final", "cycle-1")
    fourth = service.submit_market(
        engine="trend",
        slot="strategy",
        side="SELL",
        qty=1.0,
        reference_price=103.0,
        reason="exit",
        state=remaining,
        transition=final_exit,
    )
    flat_after_exit = _trend_state()
    store.complete_order_and_checkpoint(
        fourth.order_id,
        engine="trend",
        state=flat_after_exit,
        status="FILLED",
        filled_qty=1.0,
        price=103.0,
    )

    new_entry = _transition("ENTRY", "cycle-2-entry", "cycle-2")
    fifth = service.submit_market(
        engine="trend",
        slot="strategy",
        side="BUY",
        qty=1.0,
        reference_price=104.0,
        reason="entry",
        state=flat_after_exit,
        transition=new_entry,
    )

    assert fifth.transition is not None
    assert fifth.transition["position_cycle_id"] == "cycle-2"
    assert (
        len({first.intent_id, second.intent_id, third.intent_id, fourth.intent_id, fifth.intent_id})
        == 5
    )
    assert {first.transition["position_cycle_id"], fifth.transition["position_cycle_id"]} == {
        "cycle-1",
        "cycle-2",
    }

    with pytest.raises(ReconciliationRequired):
        service.submit_market(
            engine="trend",
            slot="strategy",
            side="BUY",
            qty=1.0,
            reference_price=104.0,
            reason="entry",
            state=flat_after_exit,
            transition=entry,
        )
    assert len(broker.calls) == 5
