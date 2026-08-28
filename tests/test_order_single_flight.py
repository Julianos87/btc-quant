"""Invariants de terminalité et d'émission unique des ordres market."""

from __future__ import annotations

import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from btcquant.execution.broker import (
    BrokerOrderResult,
    BrokerOrderSnapshot,
    Fill,
    PaperBroker,
)
from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.errors import (
    EngineInstanceAlreadyRunning,
    FinancialTransitionAlreadyReserved,
    InvalidOrderStateTransition,
)
from btcquant.execution.instance_lock import EngineInstanceLock
from btcquant.execution.order_service import OrderExecutionService
from btcquant.execution.order_state import (
    ExternalOrderState,
    FinancialTransitionType,
    LocalOrderState,
    LogicalOrderIdentity,
)
from btcquant.execution.recovery import recover_interrupted_orders
from btcquant.execution.state_store import StateStore


def _result(
    status: ExternalOrderState,
    *,
    requested: float = 1.0,
    filled: float = 0.0,
    remaining: float = 0.0,
) -> BrokerOrderResult:
    return BrokerOrderResult(
        fill=Fill(price=100.0, qty=filled, fee=0.0, broker_order_id="external-1"),
        status=status,
        requested_qty=requested,
        remaining_qty=remaining,
    )


@pytest.mark.parametrize(
    ("status", "filled", "remaining", "price", "message"),
    [
        (ExternalOrderState.OPEN, 0.1, 0.9, 100.0, "OPEN exige"),
        (ExternalOrderState.OPEN, 0.0, 0.0, 100.0, "OPEN exige"),
        (ExternalOrderState.PARTIAL_OPEN, 0.0, 1.0, 100.0, "PARTIAL_OPEN exige"),
        (ExternalOrderState.FILLED, 0.9, 0.0, 100.0, "FILLED exige"),
        (
            ExternalOrderState.PARTIAL_TERMINAL,
            1.0,
            0.0,
            100.0,
            "PARTIAL_TERMINAL exige",
        ),
        (ExternalOrderState.REJECTED, 0.1, 0.1, 100.0, "REJECTED exige un reste nul"),
        (ExternalOrderState.UNKNOWN, 0.6, 0.6, 100.0, "dépasse requested_qty"),
        (ExternalOrderState.UNKNOWN, 0.1, 0.9, float("nan"), "prix fini"),
    ],
)
def test_broker_result_rejects_contradictory_status_and_quantities(
    status,
    filled,
    remaining,
    price,
    message,
):
    with pytest.raises(ValueError, match=message):
        BrokerOrderResult(
            fill=Fill(price=price, qty=filled, fee=0.0),
            status=status,
            requested_qty=1.0,
            remaining_qty=remaining,
        )


class _OpenExchange:
    def amount_to_precision(self, _symbol, qty):
        return qty

    def market(self, _symbol):
        return {"limits": {"cost": {"min": None}}}

    def create_order(self, _symbol, _order_type, _side, qty, _price, _params):
        return {
            "id": "external-open-1",
            "status": "open",
            "amount": qty,
            "filled": 0.0,
            "remaining": qty,
            "price": 100.0,
            "fees": [],
        }


def _submit(service: OrderExecutionService, *, checkpoint: str = "2026-08-09T16:00:00Z"):
    return service.submit_market(
        engine="trend",
        slot="trend_ls_55",
        side="BUY",
        qty=1.0,
        reference_price=100.0,
        reason="entry",
        decision_checkpoint=checkpoint,
        transition_type=FinancialTransitionType.ENTER_LONG,
    )


def test_external_open_order_is_not_rejected_or_terminal_locally(tmp_path):
    broker = object.__new__(CcxtBroker)
    broker.exchange = _OpenExchange()
    broker.exchange_id = "binance"
    broker.symbol = "BTC/USDT"
    broker._wait_closed = lambda order: order
    store = StateStore(tmp_path / "state.db")

    result = _submit(OrderExecutionService(store, broker))

    assert result.status == "OPEN"
    assert result.external_state == ExternalOrderState.OPEN
    assert not result.is_terminal
    order = store.read_orders("trend")[0]
    assert order["status"] == "OPEN"
    assert order["local_state"] == LocalOrderState.AWAITING_EXTERNAL
    assert order["external_state"] == ExternalOrderState.OPEN
    assert order["remaining_qty"] == pytest.approx(1.0)
    assert store.unresolved_orders("trend") == [order]


@pytest.mark.parametrize(
    ("raw_status", "filled", "remaining", "expected"),
    [
        ("open", 0.0, 1.0, ExternalOrderState.OPEN),
        ("open", 0.4, 0.6, ExternalOrderState.PARTIAL_OPEN),
        ("open", 1.0, 0.0, ExternalOrderState.UNKNOWN),
        ("closed", 1.0, 0.0, ExternalOrderState.FILLED),
        ("closed", 0.4, 0.6, ExternalOrderState.PARTIAL_TERMINAL),
        ("closed", 0.0, 0.0, ExternalOrderState.UNKNOWN),
        ("canceled", 0.0, 1.0, ExternalOrderState.CANCELED),
        ("canceled", 0.4, 0.6, ExternalOrderState.CANCELED),
        ("rejected", 0.0, 1.0, ExternalOrderState.REJECTED),
        ("rejected", 0.4, 0.6, ExternalOrderState.REJECTED),
        ("expired", 0.0, 1.0, ExternalOrderState.EXPIRED),
        ("expired", 0.4, 0.6, ExternalOrderState.EXPIRED),
        ("venue_specific", 0.0, 1.0, ExternalOrderState.UNKNOWN),
    ],
)
def test_ccxt_normalization_never_infers_terminal_state_from_fill_quantity(
    raw_status,
    filled,
    remaining,
    expected,
):
    broker = object.__new__(CcxtBroker)
    result = broker._result_from_order(
        {
            "id": "external-1",
            "status": raw_status,
            "amount": 1.0,
            "filled": filled,
            "remaining": remaining,
            "average": 100.0,
        },
        100.0,
        1.0,
    )

    assert result.status == expected
    assert result.is_terminal == expected.is_terminal
    if raw_status in {"canceled", "rejected", "expired"}:
        assert result.fill.qty == pytest.approx(filled)
        assert result.remaining_qty == 0.0


@pytest.mark.parametrize(
    ("raw_status", "filled", "remaining", "expected_status", "expected_remaining"),
    [
        ("open", 0.0, 0.0, ExternalOrderState.UNKNOWN, 0.0),
        ("open", 0.25, 0.0, ExternalOrderState.UNKNOWN, 0.0),
        ("open", 0.0, 1.0, ExternalOrderState.OPEN, 1.0),
        ("open", 0.25, 0.75, ExternalOrderState.PARTIAL_OPEN, 0.75),
        ("open", 0.0, None, ExternalOrderState.OPEN, 1.0),
        ("open", 0.25, None, ExternalOrderState.PARTIAL_OPEN, 0.75),
        ("open", 1.0, None, ExternalOrderState.UNKNOWN, 0.0),
        ("open", 0.25, 0.40, ExternalOrderState.UNKNOWN, 0.40),
        ("pending", 0.0, 0.0, ExternalOrderState.UNKNOWN, 0.0),
        ("new", 0.0, 0.0, ExternalOrderState.UNKNOWN, 0.0),
    ],
)
def test_ccxt_open_like_statuses_preserve_explicit_remaining_evidence(
    raw_status,
    filled,
    remaining,
    expected_status,
    expected_remaining,
):
    broker = object.__new__(CcxtBroker)

    result = broker._result_from_order(
        {
            "id": "external-open-remaining",
            "status": raw_status,
            "amount": 1.0,
            "filled": filled,
            "remaining": remaining,
            "average": 100.0,
        },
        100.0,
        1.0,
    )

    assert result.status == expected_status
    assert result.remaining_qty == pytest.approx(expected_remaining)


def test_ccxt_direct_market_helpers_fail_closed_without_reserved_client_id():
    broker = object.__new__(CcxtBroker)
    broker.exchange = _OpenExchange()
    broker.exchange_id = "binance"
    broker.symbol = "BTC/USDT"

    with pytest.raises(ValueError, match="client_order_id réservé"):
        broker.market_buy(1.0, 100.0)
    with pytest.raises(ValueError, match="client_order_id réservé"):
        broker.market_sell(1.0, 100.0)


class _ResultBroker(PaperBroker):
    def __init__(self, result: BrokerOrderResult) -> None:
        super().__init__()
        self.result = result
        self.calls = 0
        self._calls_lock = threading.Lock()

    def execute_market(self, side, qty, ref_price, **_kwargs):
        del side, qty, ref_price
        with self._calls_lock:
            self.calls += 1
        return self.result


@pytest.mark.parametrize(
    ("external_state", "filled", "remaining", "local_status"),
    [
        (ExternalOrderState.PARTIAL_OPEN, 0.4, 0.6, "OPEN"),
        (ExternalOrderState.UNKNOWN, 0.0, 1.0, "PENDING"),
    ],
)
def test_partial_open_and_unknown_are_non_terminal(
    tmp_path,
    external_state,
    filled,
    remaining,
    local_status,
):
    store = StateStore(tmp_path / "state.db")
    result = _submit(
        OrderExecutionService(
            store,
            _ResultBroker(
                _result(
                    external_state,
                    filled=filled,
                    remaining=remaining,
                )
            ),
        )
    )

    assert not result.is_terminal
    assert result.status == local_status
    order = store.read_orders("trend")[0]
    assert order["external_state"] == external_state
    assert order["filled_qty"] == pytest.approx(filled)
    assert order["remaining_qty"] == pytest.approx(remaining)
    assert order["local_state"] != LocalOrderState.TERMINAL


def test_non_terminal_external_state_cannot_be_closed_locally(tmp_path):
    store = StateStore(tmp_path / "state.db")
    submitted = _submit(
        OrderExecutionService(
            store,
            _ResultBroker(_result(ExternalOrderState.OPEN, remaining=1.0)),
        )
    )

    with pytest.raises(InvalidOrderStateTransition, match="sans preuve externe terminale"):
        store.complete_order(submitted.order_id, status="REJECTED")

    order = store.read_orders("trend")[0]
    assert order["local_state"] == LocalOrderState.AWAITING_EXTERNAL
    assert order["external_state"] == ExternalOrderState.OPEN


def test_same_transition_from_two_threads_emits_once(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = _ResultBroker(_result(ExternalOrderState.FILLED, filled=1.0))
    barrier = threading.Barrier(2)

    def submit() -> str:
        barrier.wait()
        try:
            _submit(OrderExecutionService(store, broker))
        except FinancialTransitionAlreadyReserved:
            return "loser"
        return "winner"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result() for future in [executor.submit(submit) for _ in range(2)]]

    assert sorted(outcomes) == ["loser", "winner"]
    assert broker.calls == 1
    assert len(store.read_orders("trend")) == 1


def _reserve_process(database: str, barrier, outcomes) -> None:
    store = StateStore(database)
    identity = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    barrier.wait()
    reservation = store.reserve_market_order(
        identity,
        side="BUY",
        requested_qty=1.0,
        reference_price=100.0,
        reason="entry",
    )
    outcomes.put(reservation.acquired)


def test_same_transition_from_two_processes_has_one_sqlite_owner(tmp_path):
    context = multiprocessing.get_context("spawn")
    database = str(tmp_path / "state.db")
    barrier = context.Barrier(2)
    outcomes = context.Queue()
    processes = [
        context.Process(target=_reserve_process, args=(database, barrier, outcomes))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0, 0]
    assert sorted([outcomes.get(timeout=2), outcomes.get(timeout=2)]) == [False, True]
    assert len(StateStore(database).read_orders("trend")) == 1


def test_crash_after_reservation_before_broker_reclaims_same_intention_once(tmp_path):
    store = StateStore(tmp_path / "state.db")
    identity = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    first = store.reserve_market_order(
        identity,
        side="BUY",
        requested_qty=1.0,
        reference_price=100.0,
        reason="entry",
    )

    report = recover_interrupted_orders(store, PaperBroker(), "trend", external=True)
    broker = _ResultBroker(_result(ExternalOrderState.FILLED, filled=1.0))
    submitted = _submit(OrderExecutionService(StateStore(store.path), broker))
    with pytest.raises(FinancialTransitionAlreadyReserved):
        _submit(OrderExecutionService(StateStore(store.path), broker))

    assert first.acquired
    assert report.recovered_order_ids == [first.order_id]
    assert submitted.order_id == first.order_id
    assert submitted.intent_id == first.intent_id
    assert len(store.read_orders("trend")) == 1
    assert broker.calls == 1


def test_two_restarts_cannot_both_reclaim_pre_submission_crash(tmp_path):
    store = StateStore(tmp_path / "state.db")
    identity = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    store.reserve_market_order(
        identity,
        side="BUY",
        requested_qty=1.0,
        reference_price=100.0,
        reason="entry",
    )
    recover_interrupted_orders(store, PaperBroker(), "trend", external=True)
    broker = _ResultBroker(_result(ExternalOrderState.FILLED, filled=1.0))
    barrier = threading.Barrier(2)

    def restart() -> str:
        barrier.wait()
        try:
            _submit(OrderExecutionService(StateStore(store.path), broker))
        except FinancialTransitionAlreadyReserved:
            return "loser"
        return "winner"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result() for future in [executor.submit(restart) for _ in range(2)]]

    assert sorted(outcomes) == ["loser", "winner"]
    assert broker.calls == 1
    assert len(store.read_orders("trend")) == 1


def test_paper_crash_after_submitting_reclaims_the_same_intention(tmp_path):
    store = StateStore(tmp_path / "state.db")
    identity = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    reserved = store.reserve_market_order(
        identity,
        side="BUY",
        requested_qty=1.0,
        reference_price=100.0,
        reason="entry",
    )
    store.mark_order_submitting(reserved.order_id)

    report = recover_interrupted_orders(store, PaperBroker(), "trend", external=False)
    submitted = _submit(OrderExecutionService(StateStore(store.path), PaperBroker()))

    assert report.recovered_order_ids == [reserved.order_id]
    assert submitted.order_id == reserved.order_id
    assert submitted.intent_id == reserved.intent_id
    assert len(store.read_orders("trend")) == 1


def test_reclaim_restores_remaining_quantity_for_a_new_submission(tmp_path):
    store = StateStore(tmp_path / "state.db")
    identity = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    reserved = store.reserve_market_order(
        identity,
        side="BUY",
        requested_qty=1.0,
        reference_price=100.0,
        reason="entry",
    )
    store.mark_order_submitting(reserved.order_id)
    recover_interrupted_orders(store, PaperBroker(), "trend", external=False)

    assert store.reclaim_safe_market_order(reserved.order_id)
    order = store.read_orders("trend")[0]
    assert order["status"] == "PENDING"
    assert order["local_state"] == LocalOrderState.SUBMITTING
    assert order["remaining_qty"] == pytest.approx(order["requested_qty"])


def test_logical_identity_is_stable_and_changes_for_a_new_decision():
    first = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    same = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    next_bar = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T20:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    exit_transition = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.EXIT,
        "entry=2026-08-01T00:00:00Z|initial_qty=1",
    )
    next_sequence = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
        transition_sequence=1,
    )

    assert first.logical_key == same.logical_key
    assert first.intent_id == same.intent_id
    assert first.logical_key != next_bar.logical_key
    assert first.intent_id != next_bar.intent_id
    assert first.logical_key != exit_transition.logical_key
    assert first.logical_key != next_sequence.logical_key


class _PowerLoss(BaseException):
    pass


class _CrashAfterSendBroker(PaperBroker):
    supports_order_lookup = True

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.lookup_calls = 0
        self.last_client_order_id = None

    def execute_market(self, side, qty, ref_price, **_kwargs):
        del side, qty, ref_price
        self.calls += 1
        self.last_client_order_id = _kwargs["client_order_id"]
        raise _PowerLoss("exchange accepted, process died")

    def lookup_order(self, client_order_id):
        self.lookup_calls += 1
        assert client_order_id == self.last_client_order_id
        return None


def test_restart_never_reemits_an_ambiguous_transition(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = _CrashAfterSendBroker()
    with pytest.raises(_PowerLoss):
        _submit(OrderExecutionService(store, broker))

    report = recover_interrupted_orders(
        StateStore(store.path),
        broker,
        "trend",
        external=True,
    )

    with pytest.raises(FinancialTransitionAlreadyReserved):
        _submit(OrderExecutionService(StateStore(store.path), broker))

    assert broker.calls == 1
    assert broker.lookup_calls == 1
    assert not report.can_start
    order = store.read_orders("trend")[0]
    assert order["local_state"] == LocalOrderState.PENDING_RECONCILIATION
    assert order["external_state"] == ExternalOrderState.UNKNOWN


class _RecoverableResultBroker(_ResultBroker):
    supports_order_lookup = True

    def lookup_order(self, client_order_id):
        result = self.result
        return BrokerOrderSnapshot(
            client_order_id=client_order_id,
            broker_order_id=result.fill.broker_order_id,
            status=result.status,
            filled_qty=result.fill.qty,
            price=result.fill.price,
            fee=result.fill.fee,
            requested_qty=result.requested_qty,
            remaining_qty=result.remaining_qty,
        )


@pytest.mark.parametrize(
    ("external_state", "filled", "remaining"),
    [
        (ExternalOrderState.OPEN, 0.0, 1.0),
        (ExternalOrderState.PARTIAL_OPEN, 0.4, 0.6),
    ],
)
def test_restart_preserves_open_quantity_and_never_reemits(
    tmp_path,
    external_state,
    filled,
    remaining,
):
    store = StateStore(tmp_path / "state.db")
    broker = _RecoverableResultBroker(_result(external_state, filled=filled, remaining=remaining))
    submitted = _submit(OrderExecutionService(store, broker))

    report = recover_interrupted_orders(
        StateStore(store.path),
        broker,
        "trend",
        external=True,
    )
    with pytest.raises(FinancialTransitionAlreadyReserved):
        _submit(OrderExecutionService(StateStore(store.path), broker))

    assert not submitted.is_terminal
    assert not report.can_start
    assert broker.calls == 1
    order = StateStore(store.path).read_orders("trend")[0]
    assert order["filled_qty"] == pytest.approx(filled)
    assert order["remaining_qty"] == pytest.approx(remaining)
    assert order["external_state"] == external_state
    assert order["local_state"] == LocalOrderState.PENDING_RECONCILIATION


def test_confirmed_cancellation_after_restart_has_one_new_intent_owner(tmp_path):
    store = StateStore(tmp_path / "state.db")
    broker = _RecoverableResultBroker(_result(ExternalOrderState.OPEN, filled=0.0, remaining=1.0))
    first = _submit(OrderExecutionService(store, broker))
    broker.result = _result(ExternalOrderState.CANCELED, filled=0.0, remaining=0.0)

    recovery = recover_interrupted_orders(store, broker, "trend", external=True)

    broker.result = _result(ExternalOrderState.FILLED, filled=1.0, remaining=0.0)
    barrier = threading.Barrier(2)

    def restart() -> str:
        barrier.wait()
        try:
            submitted = _submit(OrderExecutionService(StateStore(store.path), broker))
        except FinancialTransitionAlreadyReserved:
            return "loser"
        return f"winner:{submitted.transition_sequence}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.result() for future in [executor.submit(restart) for _ in range(2)]]

    orders = store.read_orders("trend")
    assert recovery.can_start
    assert sorted(outcomes) == ["loser", "winner:1"]
    assert broker.calls == 2
    assert len(orders) == 2
    assert orders[0]["intent_id"] == first.intent_id
    assert orders[0]["external_state"] == ExternalOrderState.CANCELED
    assert orders[1]["intent_id"] != first.intent_id
    assert orders[1]["logical_order_key"] != first.logical_order_key


def test_instance_lock_is_a_secondary_defense_and_is_released(tmp_path):
    database = Path(tmp_path / "state.db")
    first = EngineInstanceLock(database, "trend")
    second = EngineInstanceLock(database, "trend")
    first.acquire()
    try:
        with pytest.raises(EngineInstanceAlreadyRunning):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_instance_lock_is_released_if_pid_fsync_fails(tmp_path, monkeypatch):
    database = Path(tmp_path / "state.db")
    broken = EngineInstanceLock(database, "trend")
    monkeypatch.setattr("btcquant.execution.instance_lock.os.fsync", lambda _fd: 1 / 0)

    with pytest.raises(ZeroDivisionError):
        broken.acquire()
    monkeypatch.undo()

    replacement = EngineInstanceLock(database, "trend")
    replacement.acquire()
    replacement.release()


def test_equivalent_timezone_serializations_share_identity():
    identities = [
        LogicalOrderIdentity(
            "trend",
            "trend_ls_55",
            checkpoint,
            FinancialTransitionType.ENTER_LONG,
        )
        for checkpoint in (
            "2026-08-09T16:00:00Z",
            "2026-08-09T16:00:00+00:00",
            "2026-08-09T17:00:00+01:00",
        )
    ]

    assert {identity.logical_key for identity in identities} == {identities[0].logical_key}
    assert {identity.intent_id for identity in identities} == {identities[0].intent_id}


def test_position_generation_timezone_is_canonicalized():
    identities = [
        LogicalOrderIdentity(
            "trend",
            "trend_ls_55",
            "2026-08-09T16:00:00Z",
            FinancialTransitionType.EXIT,
            generation,
        )
        for generation in (
            "entry=2026-08-01T00:00:00Z|initial_qty=1",
            "entry=2026-08-01T00:00:00+00:00|initial_qty=1",
            "entry=2026-08-01T01:00:00+01:00|initial_qty=1",
        )
    ]

    assert {identity.logical_key for identity in identities} == {identities[0].logical_key}


def test_position_transitions_require_a_position_generation():
    with pytest.raises(ValueError, match="position_generation"):
        LogicalOrderIdentity(
            "trend",
            "trend_ls_55",
            "2026-08-09T16:00:00Z",
            FinancialTransitionType.EXIT,
        )

    with pytest.raises(ValueError, match="position_generation"):
        LogicalOrderIdentity(
            "trend",
            "trend_ls_55",
            "2026-08-09T16:00:00Z",
            FinancialTransitionType.ENTER_LONG,
            "entry=2026-08-01T00:00:00Z|initial_qty=1",
        )


def test_distinct_logical_keys_map_to_distinct_exchange_client_ids():
    first = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    same = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00+00:00",
        FinancialTransitionType.ENTER_LONG,
    )
    next_sequence = LogicalOrderIdentity(
        "trend",
        "trend_ls_55",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
        transition_sequence=1,
    )

    assert first.intent_id == same.intent_id
    assert first.intent_id != next_sequence.intent_id
    for exchange_id in ("binance", "hyperliquid"):
        assert CcxtBroker._external_client_order_id(first.intent_id, exchange_id) == (
            CcxtBroker._external_client_order_id(same.intent_id, exchange_id)
        )
        assert CcxtBroker._external_client_order_id(first.intent_id, exchange_id) != (
            CcxtBroker._external_client_order_id(next_sequence.intent_id, exchange_id)
        )
