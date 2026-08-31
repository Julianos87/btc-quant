"""Reprise déterministe des ordres interrompus à chaque frontière de crash."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from btcquant.execution.broker import (
    Broker,
    BrokerOrderResult,
    BrokerOrderSnapshot,
    Fill,
    PaperBroker,
)
from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.errors import ReconciliationRequired
from btcquant.execution.order_state import (
    ExternalOrderState,
    FinancialTransitionType,
    LocalOrderState,
    LogicalOrderIdentity,
)
from btcquant.execution.recovery import recover_interrupted_orders
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.execution.state_store import StateStore
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Strategy


class PowerLoss(BaseException):
    """Simule un arrêt brutal, non intercepté par ``except Exception``."""


class StaticStrategy(Strategy):
    name = "recovery"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def entry_signal(self, row: pd.Series) -> int:
        return 1

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * 10.0


class LookupBroker(Broker):
    supports_stop_orders = True
    supports_order_lookup = True

    def __init__(
        self,
        snapshot: BrokerOrderSnapshot | None = None,
        *,
        result: BrokerOrderResult | None = None,
        lookup_error: Exception | None = None,
        execute_error: Exception | None = None,
        crash_before_send: bool = False,
    ) -> None:
        self.snapshot = snapshot
        self.result = result or BrokerOrderResult(
            fill=Fill(price=100.0, qty=1.0, fee=0.1, broker_order_id="remote-1"),
            status=ExternalOrderState.FILLED,
            requested_qty=1.0,
            remaining_qty=0.0,
        )
        self.lookup_error = lookup_error
        self.execute_error = execute_error
        self.crash_before_send = crash_before_send
        self.last_client_order_id: str | None = None
        self.execute_calls = 0
        self.lookup_calls = 0

    def market_buy(self, qty: float, ref_price: float) -> BrokerOrderResult:
        return self.result

    def market_sell(self, qty: float, ref_price: float) -> BrokerOrderResult:
        return self.result

    def execute_market(
        self,
        side: str,
        qty: float,
        ref_price: float,
        *,
        client_order_id: str | None = None,
        reduce_only: bool = False,
        available_volume: float | None = None,
        delayed_price: float | None = None,
        volatility_annual: float | None = None,
    ) -> BrokerOrderResult:
        del (
            side,
            qty,
            ref_price,
            reduce_only,
            available_volume,
            delayed_price,
            volatility_annual,
        )
        self.execute_calls += 1
        self.last_client_order_id = client_order_id
        if self.crash_before_send:
            raise PowerLoss("crash before broker send")
        if self.execute_error is not None:
            raise self.execute_error
        return self.result

    def lookup_order(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        self.lookup_calls += 1
        if self.lookup_error is not None:
            raise self.lookup_error
        if self.snapshot is None:
            return None
        return BrokerOrderSnapshot(
            client_order_id=client_order_id,
            broker_order_id=self.snapshot.broker_order_id,
            status=self.snapshot.status,
            filled_qty=self.snapshot.filled_qty,
            price=self.snapshot.price,
            fee=self.snapshot.fee,
            requested_qty=self.snapshot.requested_qty,
            remaining_qty=self.snapshot.remaining_qty,
        )

    def place_stop(
        self,
        qty: float,
        stop_price: float,
        direction: int = 1,
        *,
        client_order_id: str | None = None,
    ) -> str:
        del qty, stop_price, direction, client_order_id
        return "stop-1"


def risk() -> RiskConfig:
    return RiskConfig(
        initial_capital=1_000.0,
        risk_per_trade=0.01,
        max_position_pct=0.95,
        vol_target_annual=None,
        max_drawdown_halt=0.5,
        daily_loss_limit=None,
    )


def pending_order(store: StateStore, intent_id: str = "intent-1") -> int:
    order_id = store.begin_order(
        "trend",
        "recovery",
        intent_id,
        "MARKET",
        "BUY",
        1.0,
        "entry",
    )
    return order_id


def assert_no_positions(store: StateStore) -> None:
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM positions WHERE engine = 'trend'").fetchone()[
                0
            ]
            == 0
        )


def make_runner(path: Path, broker: Broker) -> tuple[LiveRunner, StrategySlot]:
    slot = StrategySlot(StaticStrategy(), 1.0, 1_000.0)
    return (
        LiveRunner(
            [slot],
            broker,
            risk(),
            "binance",
            "BTC/USDT",
            path,
        ),
        slot,
    )


def test_ccxt_uses_stable_client_id_and_returns_broker_id():
    class FakeExchange:
        def __init__(self):
            self.params = None

        def amount_to_precision(self, symbol, qty):
            return qty

        def market(self, symbol):
            return {"limits": {"cost": {"min": None}}}

        def create_order(self, symbol, order_type, side, qty, price, params):
            self.params = params
            return {
                "id": "exchange-123",
                "status": "closed",
                "average": 101.0,
                "filled": qty,
                "amount": qty,
                "fees": [{"cost": 0.2}],
            }

    broker = object.__new__(CcxtBroker)
    broker.exchange = FakeExchange()
    broker.symbol = "BTC/USDT"

    result = broker.execute_market(
        "BUY",
        2.0,
        100.0,
        client_order_id="local-intent-123",
    )

    expected_client_id = CcxtBroker._external_client_order_id("local-intent-123")
    assert broker.exchange.params == {"newClientOrderId": expected_client_id}
    assert result.fill.broker_order_id == "exchange-123"
    assert result.fill.qty == pytest.approx(2.0)
    assert result.status == ExternalOrderState.FILLED


def test_ccxt_lookup_uses_the_same_deterministic_client_id():
    class FakeExchange:
        def __init__(self):
            self.lookup = None

        def fetch_order(self, order_id, symbol, params):
            self.lookup = (order_id, symbol, params)
            return {
                "id": "exchange-456",
                "status": "closed",
                "average": 102.0,
                "filled": 1.0,
                "amount": 1.0,
                "fees": [],
            }

    broker = object.__new__(CcxtBroker)
    broker.exchange = FakeExchange()
    broker.symbol = "BTC/USDT"

    snapshot = broker.lookup_order("local-intent-456")

    external_id = CcxtBroker._external_client_order_id("local-intent-456")
    assert broker.exchange.lookup == (
        external_id,
        "BTC/USDT",
        {"origClientOrderId": external_id},
    )
    assert snapshot is not None
    assert snapshot.status == "FILLED"
    assert snapshot.broker_order_id == "exchange-456"


def test_paper_intent_is_safely_aborted_after_restart(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    order_id = pending_order(store)

    report = recover_interrupted_orders(
        store,
        PaperBroker(),
        "trend",
        external=False,
    )

    assert report.can_start
    assert report.recovered_order_ids == [order_id]
    assert store.read_orders("trend")[0]["status"] == "RECOVERED_ABORTED"


def test_external_intent_created_is_recovered_without_lookup(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    identity = LogicalOrderIdentity(
        "trend",
        "recovery",
        "2026-08-09T16:00:00Z",
        FinancialTransitionType.ENTER_LONG,
    )
    reservation = store.reserve_market_order(
        identity,
        side="BUY",
        requested_qty=1.0,
        reference_price=100.0,
        reason="entry",
    )
    broker = LookupBroker(
        BrokerOrderSnapshot(
            "intent-1",
            "remote-never-called",
            "CANCELED",
            0.0,
            requested_qty=1.0,
            remaining_qty=1.0,
        )
    )

    report = recover_interrupted_orders(store, broker, "trend", external=True)

    assert report.can_start
    assert report.recovered_order_ids == [reservation.order_id]
    assert report.manual_order_ids == []
    assert broker.lookup_calls == 0
    order = store.read_orders("trend")[0]
    assert order["status"] == "RECOVERED_ABORTED"
    assert order["local_state"] == LocalOrderState.TERMINAL
    assert_no_positions(store)


def test_external_absence_after_submission_is_ambiguous_and_blocks(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    order_id = pending_order(store)

    report = recover_interrupted_orders(
        store,
        LookupBroker(snapshot=None),
        "trend",
        external=True,
    )

    assert not report.can_start
    assert report.recovered_order_ids == []
    assert order_id in report.lookup_errors
    order = store.read_orders("trend")[0]
    assert order["status"] == "PENDING"
    assert order["external_state"] == ExternalOrderState.UNKNOWN
    assert order["local_state"] == LocalOrderState.PENDING_RECONCILIATION


@pytest.mark.parametrize("remote_status", ["CANCELED", "REJECTED", "EXPIRED"])
def test_external_terminal_zero_reported_fill_still_requires_reconciliation(
    remote_status, tmp_path
):
    store = StateStore(tmp_path / "btcquant.db")
    order_id = pending_order(store)
    snapshot = BrokerOrderSnapshot(
        "intent-1",
        "remote-terminal",
        remote_status,
        0.0,
        requested_qty=1.0,
        remaining_qty=1.0,
    )

    report = recover_interrupted_orders(
        store,
        LookupBroker(snapshot),
        "trend",
        external=True,
    )

    order = store.read_orders("trend")[0]
    assert not report.can_start
    assert report.recovered_order_ids == []
    assert report.manual_order_ids == [order_id]
    assert order["status"] == "UNBALANCED"
    assert order["local_state"] == LocalOrderState.PENDING_RECONCILIATION
    assert order["external_state"] == ExternalOrderState(remote_status)
    assert order["broker_order_id"] == "remote-terminal"
    assert order["filled_qty"] == pytest.approx(0.0)
    assert order["remaining_qty"] == pytest.approx(1.0)
    assert store.get_external_order_observations(order_id) == []
    assert_no_positions(store)


@pytest.mark.parametrize(
    ("remote_status", "filled_qty"),
    [
        ("FILLED", 1.0),
        ("PARTIAL", 0.4),
        ("OPEN", 0.0),
        ("CANCELED", 0.4),
    ],
)
def test_any_possible_external_effect_fails_closed(remote_status, filled_qty, tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    order_id = pending_order(store)
    snapshot = BrokerOrderSnapshot(
        "intent-1",
        "remote-risky",
        remote_status,
        filled_qty,
        price=101.0 if filled_qty else None,
        fee=0.1 if filled_qty else 0.0,
    )

    report = recover_interrupted_orders(
        store,
        LookupBroker(snapshot),
        "trend",
        external=True,
    )

    order = store.read_orders("trend")[0]
    assert not report.can_start
    assert report.manual_order_ids == [order_id]
    assert order["status"] == "UNBALANCED"
    assert order["broker_order_id"] == "remote-risky"
    assert order["filled_qty"] == pytest.approx(filled_qty)
    assert store.unresolved_orders("trend")[0]["id"] == order_id


def test_lookup_outage_keeps_pending_order_retryable(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    order_id = pending_order(store)

    report = recover_interrupted_orders(
        store,
        LookupBroker(lookup_error=TimeoutError("exchange unavailable")),
        "trend",
        external=True,
    )

    assert not report.can_start
    assert order_id in report.lookup_errors
    assert store.read_orders("trend")[0]["status"] == "PENDING"


def test_open_order_is_rechecked_but_terminal_cancellation_still_requires_reconciliation(
    tmp_path,
):
    store = StateStore(tmp_path / "btcquant.db")
    order_id = pending_order(store)
    broker = LookupBroker(
        BrokerOrderSnapshot("intent-1", "remote-open", "OPEN", 0.0, remaining_qty=1.0)
    )

    first = recover_interrupted_orders(store, broker, "trend", external=True)
    broker.snapshot = BrokerOrderSnapshot(
        "intent-1",
        "remote-open",
        "CANCELED",
        0.0,
        remaining_qty=0.0,
    )
    second = recover_interrupted_orders(store, broker, "trend", external=True)
    third = recover_interrupted_orders(store, broker, "trend", external=True)

    assert not first.can_start
    assert not second.can_start
    assert not third.can_start
    assert second.recovered_order_ids == []
    assert third.recovered_order_ids == []
    assert second.manual_order_ids == [order_id]
    assert third.manual_order_ids == [order_id]
    order = store.read_orders("trend")[0]
    assert order["status"] == "UNBALANCED"
    assert order["external_state"] == ExternalOrderState.CANCELED
    assert order["local_state"] == LocalOrderState.PENDING_RECONCILIATION
    assert order["broker_order_id"] == "remote-open"
    assert_no_positions(store)


def test_crash_after_submitting_before_send_is_ambiguous_and_blocks(tmp_path):
    broker = LookupBroker(crash_before_send=True)
    runner, slot = make_runner(tmp_path / "btcquant.db", broker)
    row = pd.Series({"close": 100.0, "volume": 100.0, "_rvol": float("nan")})

    with pytest.raises(PowerLoss, match="before broker send"):
        runner._enter_position(slot, row, 100.0, 1, decision_checkpoint="2026-08-09T16:00:00Z")

    assert runner.store.pending_orders("trend")
    broker.crash_before_send = False
    report = recover_interrupted_orders(
        runner.store,
        broker,
        "trend",
        external=True,
    )
    assert not report.can_start
    order = runner.store.read_orders("trend")[0]
    assert order["status"] == "PENDING"
    assert order["local_state"] == LocalOrderState.PENDING_RECONCILIATION


def test_network_timeout_stays_pending_until_broker_lookup(tmp_path):
    snapshot = BrokerOrderSnapshot(
        "placeholder",
        "remote-timeout",
        "FILLED",
        1.0,
        price=100.0,
        fee=0.1,
    )
    broker = LookupBroker(
        snapshot,
        execute_error=TimeoutError("response lost after send"),
    )
    runner, slot = make_runner(tmp_path / "btcquant.db", broker)
    row = pd.Series({"close": 100.0, "volume": 100.0, "_rvol": float("nan")})

    with pytest.raises(ReconciliationRequired, match="résultat externe ambigu"):
        runner._enter_position(slot, row, 100.0, 1, decision_checkpoint="2026-08-09T16:00:00Z")

    order = runner.store.read_orders("trend")[0]
    assert order["status"] == "PENDING"
    assert "ambigu" in order["error"]
    broker.execute_error = None
    report = recover_interrupted_orders(
        runner.store,
        broker,
        "trend",
        external=True,
    )
    assert not report.can_start
    assert runner.store.read_orders("trend")[0]["status"] == "UNBALANCED"


def test_open_market_result_stops_runner_before_applying_position(tmp_path):
    broker = LookupBroker(
        result=BrokerOrderResult(
            fill=Fill(price=100.0, qty=0.0, fee=0.0, broker_order_id="remote-open"),
            status=ExternalOrderState.OPEN,
            requested_qty=1.0,
            remaining_qty=1.0,
        )
    )
    runner, slot = make_runner(tmp_path / "btcquant.db", broker)
    row = pd.Series({"close": 100.0, "volume": 100.0, "_rvol": float("nan")})

    with pytest.raises(ReconciliationRequired, match="non terminal"):
        runner._enter_position(
            slot,
            row,
            100.0,
            1,
            decision_checkpoint="2026-08-09T16:00:00Z",
        )

    order = runner.store.read_orders("trend")[0]
    assert broker.execute_calls == 1
    assert slot.position is None
    assert order["status"] == "OPEN"
    assert order["external_state"] == ExternalOrderState.OPEN
    assert order["local_state"] == LocalOrderState.AWAITING_EXTERNAL
    assert runner.store.load_engine_state("trend") is not None


def test_open_result_with_failed_checkpoint_still_stops_fail_closed(tmp_path, monkeypatch):
    broker = LookupBroker(
        result=BrokerOrderResult(
            fill=Fill(price=100.0, qty=0.0, fee=0.0, broker_order_id="remote-open"),
            status=ExternalOrderState.OPEN,
            requested_qty=1.0,
            remaining_qty=1.0,
        )
    )
    runner, slot = make_runner(tmp_path / "btcquant.db", broker)
    row = pd.Series({"close": 100.0, "volume": 100.0, "_rvol": float("nan")})
    monkeypatch.setattr(
        runner.store,
        "save_engine_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    with pytest.raises(ReconciliationRequired, match="checkpoint impossible"):
        runner._enter_position(
            slot,
            row,
            100.0,
            1,
            decision_checkpoint="2026-08-09T16:00:00Z",
        )

    order = runner.store.read_orders("trend")[0]
    assert broker.execute_calls == 1
    assert slot.position is None
    assert order["external_state"] == ExternalOrderState.OPEN
    assert order["local_state"] == LocalOrderState.AWAITING_EXTERNAL


def test_confirmed_rejection_advances_persisted_sequence_for_next_decision(tmp_path):
    rejected = BrokerOrderResult(
        fill=Fill(price=100.0, qty=0.0, fee=0.0, broker_order_id="rejected-1"),
        status=ExternalOrderState.REJECTED,
        requested_qty=1.0,
        remaining_qty=0.0,
    )
    broker = LookupBroker(result=rejected)
    broker.supports_stop_orders = False
    runner, slot = make_runner(tmp_path / "btcquant.db", broker)
    row = pd.Series({"close": 100.0, "volume": 100.0, "_rvol": float("nan")})
    checkpoint = "2026-08-09T16:00:00Z"

    runner._enter_position(slot, row, 100.0, 1, decision_checkpoint=checkpoint)
    broker.result = BrokerOrderResult(
        fill=Fill(price=100.0, qty=1.0, fee=0.1, broker_order_id="filled-2"),
        status=ExternalOrderState.FILLED,
        requested_qty=1.0,
        remaining_qty=0.0,
    )
    runner._enter_position(slot, row, 100.0, 1, decision_checkpoint=checkpoint)

    orders = runner.store.read_orders("trend")
    assert broker.execute_calls == 2
    assert len(orders) == 2
    assert orders[0]["logical_order_key"] != orders[1]["logical_order_key"]
    assert slot.financial_transition_seq == 2
    persisted = runner.store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["slots"]["recovery"]["financial_transition_seq"] == 2


def test_crash_after_fill_before_checkpoint_is_never_auto_applied(tmp_path, monkeypatch):
    snapshot = BrokerOrderSnapshot(
        "placeholder",
        "remote-1",
        "FILLED",
        1.0,
        price=100.0,
        fee=0.1,
    )
    broker = LookupBroker(snapshot)
    runner, slot = make_runner(tmp_path / "btcquant.db", broker)
    row = pd.Series({"close": 100.0, "volume": 100.0, "_rvol": float("nan")})

    def crash_checkpoint(*args, **kwargs):
        raise PowerLoss("after fill before checkpoint")

    monkeypatch.setattr(runner.store, "complete_order_and_checkpoint", crash_checkpoint)
    with pytest.raises(PowerLoss, match="after fill"):
        runner._enter_position(slot, row, 100.0, 1, decision_checkpoint="2026-08-09T16:00:00Z")

    assert slot.position is not None  # mémoire du processus mourant uniquement
    assert runner.store.load_engine_state("trend") is not None
    report = recover_interrupted_orders(
        StateStore(tmp_path / "btcquant.db"),
        broker,
        "trend",
        external=True,
    )
    assert not report.can_start
    assert StateStore(tmp_path / "btcquant.db").read_orders("trend")[0]["status"] == "UNBALANCED"


def test_checkpoint_exception_after_fill_stops_runner_fail_closed(tmp_path, monkeypatch):
    broker = LookupBroker()
    runner, slot = make_runner(tmp_path / "btcquant.db", broker)
    row = pd.Series({"close": 100.0, "volume": 100.0, "_rvol": float("nan")})
    monkeypatch.setattr(
        runner.store,
        "complete_order_and_checkpoint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    with pytest.raises(ReconciliationRequired, match="checkpoint métier impossible"):
        runner._enter_position(
            slot,
            row,
            100.0,
            1,
            decision_checkpoint="2026-08-09T16:00:00Z",
        )

    order = runner.store.read_orders("trend")[0]
    assert broker.execute_calls == 1
    assert order["external_state"] == ExternalOrderState.FILLED
    assert order["local_state"] == LocalOrderState.PENDING_RECONCILIATION
    assert runner.store.load_engine_state("trend") is not None


def test_accounting_exception_after_fill_stops_runner_fail_closed(tmp_path, monkeypatch):
    broker = LookupBroker()
    runner, slot = make_runner(tmp_path / "btcquant.db", broker)
    row = pd.Series({"close": 100.0, "volume": 100.0, "_rvol": float("nan")})
    monkeypatch.setattr(
        runner.accounting_service,
        "open_position",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("accounting unavailable")),
    )

    with pytest.raises(ReconciliationRequired, match="application métier impossible"):
        runner._enter_position(
            slot,
            row,
            100.0,
            1,
            decision_checkpoint="2026-08-09T16:00:00Z",
        )

    order = runner.store.read_orders("trend")[0]
    assert broker.execute_calls == 1
    assert order["external_state"] == ExternalOrderState.FILLED
    assert order["local_state"] == LocalOrderState.PENDING_RECONCILIATION
    assert runner.store.load_engine_state("trend") is not None


def test_crash_after_atomic_checkpoint_needs_no_recovery(tmp_path, monkeypatch):
    broker = LookupBroker()
    runner, slot = make_runner(tmp_path / "btcquant.db", broker)
    row = pd.Series({"close": 100.0, "volume": 100.0, "_rvol": float("nan")})
    original_checkpoint = runner.store.complete_order_and_checkpoint

    def checkpoint_then_crash(*args, **kwargs):
        original_checkpoint(*args, **kwargs)
        raise PowerLoss("after committed checkpoint")

    monkeypatch.setattr(runner.store, "complete_order_and_checkpoint", checkpoint_then_crash)
    with pytest.raises(PowerLoss, match="committed checkpoint"):
        runner._enter_position(slot, row, 100.0, 1, decision_checkpoint="2026-08-09T16:00:00Z")

    restarted_store = StateStore(tmp_path / "btcquant.db")
    assert restarted_store.unresolved_orders("trend") == []
    persisted_order = restarted_store.read_orders("trend")[0]
    assert persisted_order["status"] == "FILLED"
    assert persisted_order["broker_order_id"] == "remote-1"
    persisted = restarted_store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["slots"]["recovery"]["position"]["qty"] == pytest.approx(1.0)


def test_hyperliquid_market_exit_uses_reference_price_cloid_and_reduce_only():
    class FakeExchange:
        def __init__(self):
            self.created = None

        def amount_to_precision(self, _symbol, qty):
            return qty

        def market(self, _symbol):
            return {"limits": {"cost": {"min": None}}}

        def create_order(self, symbol, order_type, side, qty, price, params):
            self.created = (symbol, order_type, side, qty, price, params)
            return {
                "id": "hl-123",
                "status": "closed",
                "average": price,
                "filled": qty,
                "amount": qty,
            }

    broker = object.__new__(CcxtBroker)
    broker.exchange = FakeExchange()
    broker.exchange_id = "hyperliquid"
    broker.symbol = "BTC/USDC:USDC"

    result = broker.execute_market(
        "SELL",
        0.01,
        50_000.0,
        client_order_id="close-intent",
        reduce_only=True,
    )

    assert result.fill.broker_order_id == "hl-123"
    assert broker.exchange.created is not None
    _, order_type, side, qty, price, params = broker.exchange.created
    assert (order_type, side, qty, price) == ("market", "sell", 0.01, 50_000.0)
    assert params["reduceOnly"] is True
    assert params["clientOrderId"] == CcxtBroker._external_client_order_id(
        "close-intent", "hyperliquid"
    )
