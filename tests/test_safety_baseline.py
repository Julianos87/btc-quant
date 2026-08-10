"""Garde-fous opérationnels introduits par le lot Safety Baseline."""

from __future__ import annotations

from pathlib import Path
import threading

import pandas as pd
import pytest

from btcquant.execution.broker import Broker, BrokerOrderResult, Fill, PaperBroker
from btcquant.execution.carry_contract import (
    CarrySagaResult,
    CarrySagaStatus,
)
from btcquant.execution.carry_runner import CarryRunner
from btcquant.execution.ccxt_broker import CcxtBroker
from btcquant.execution.order_state import ExternalOrderState
from btcquant.execution.reconcile import reconcile
from btcquant.execution.runner import LiveRunner, ReconciliationRequired, StrategySlot
from btcquant.execution.state_store import StateStore
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Position, Strategy


class StaticStrategy(Strategy):
    name = "safety"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def entry_signal(self, row: pd.Series) -> int:
        return 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * 10

    def trailing_stop(self, row: pd.Series, position: Position) -> float | None:
        return None

    def exit_signal(self, row: pd.Series, position: Position) -> bool:
        return False


class RecordingBroker(Broker):
    supports_stop_orders = True

    def __init__(self, fills: list[Fill], stop: dict | None = None) -> None:
        self.fills = fills
        self.stop = stop or {"id": "existing-stop", "status": "open", "amount": 2.0}
        self.cancelled: list[str] = []
        self.placed: list[tuple[float, float, int]] = []

    def _next_result(self, requested_qty: float) -> BrokerOrderResult:
        fill = self.fills.pop(0)
        status = (
            ExternalOrderState.REJECTED
            if fill.qty <= 0
            else ExternalOrderState.PARTIAL_TERMINAL
            if fill.qty < requested_qty - 1e-9
            else ExternalOrderState.FILLED
        )
        return BrokerOrderResult(fill, status, requested_qty, 0.0)

    def market_buy(self, qty: float, ref_price: float) -> BrokerOrderResult:
        del ref_price
        return self._next_result(qty)

    def market_sell(self, qty: float, ref_price: float) -> BrokerOrderResult:
        del ref_price
        return self._next_result(qty)

    def execute_market(
        self,
        side: str,
        qty: float,
        ref_price: float,
        *,
        client_order_id: str | None = None,
        **_kwargs,
    ) -> BrokerOrderResult:
        del side, ref_price
        assert client_order_id is not None
        return self._next_result(qty)

    def place_stop(
        self,
        qty: float,
        stop_price: float,
        direction: int = 1,
        *,
        client_order_id: str | None = None,
    ) -> str:
        del client_order_id
        self.placed.append((qty, stop_price, direction))
        return f"stop-{len(self.placed)}"

    def cancel_stop(self, order_id: str) -> None:
        self.cancelled.append(order_id)

        self.stop = {**self.stop, "status": "canceled", "remaining": 0.0}

    def stop_status(self, order_id: str) -> dict:
        return self.stop


def _risk(max_drawdown: float = 0.2) -> RiskConfig:
    return RiskConfig(
        initial_capital=1_000,
        risk_per_trade=0.01,
        max_position_pct=0.95,
        vol_target_annual=None,
        max_drawdown_halt=max_drawdown,
        daily_loss_limit=None,
    )


def _runner(tmp_path: Path, broker: Broker, cash: float = 1_000) -> tuple[LiveRunner, StrategySlot]:
    slot = StrategySlot(StaticStrategy(), 1.0, cash)
    runner = LiveRunner([slot], broker, _risk(), "binance", "BTC/USDT", tmp_path / "state.json")
    return runner, slot


def _position(qty: float = 2.0) -> Position:
    return Position(
        entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
        entry_price=100.0,
        qty=qty,
        stop_price=90.0,
        direction=1,
        best_close=100.0,
    )


def test_kill_switch_liquidates_at_current_tick(tmp_path):
    broker = RecordingBroker([Fill(price=70.0, qty=2.0, fee=0.0)])
    runner, slot = _runner(tmp_path, broker, cash=800.0)
    slot.position = _position()
    runner.peak_equity = 1_000.0

    runner._update_kill_switches(70.0)
    runner._liquidate_if_halted(70.0)

    assert runner.halted
    assert slot.position is None


def test_failed_exit_keeps_position_and_existing_stop(tmp_path):
    broker = RecordingBroker([Fill(price=100.0, qty=0.0, fee=0.0)])
    runner, slot = _runner(tmp_path, broker)
    slot.position = _position()
    slot.stop_order_id = "existing-stop"

    runner._exit_position(slot, 100.0, "signal")

    assert slot.position is not None
    assert slot.stop_order_id == "existing-stop"
    assert broker.cancelled == []


def test_partial_exit_reprotects_remainder_before_cancel(tmp_path):
    broker = RecordingBroker([Fill(price=100.0, qty=0.75, fee=0.0)])
    runner, slot = _runner(tmp_path, broker)
    slot.position = _position(qty=2.0)
    slot.stop_order_id = "existing-stop"

    runner._exit_position(slot, 100.0, "signal")

    assert slot.position is not None
    assert slot.position.qty == pytest.approx(1.25)
    assert broker.placed == [(1.25, 90.0, 1)]
    assert broker.cancelled == ["existing-stop"]
    assert slot.stop_order_id == "stop-1"


def test_partial_exit_persists_remainder_and_retries_only_remainder(tmp_path):
    broker = RecordingBroker(
        [
            Fill(price=100.0, qty=0.4, fee=0.0),
            Fill(price=99.0, qty=0.6, fee=0.0),
        ]
    )
    runner, slot = _runner(tmp_path, broker)
    slot.position = _position(qty=1.0)
    slot.stop_order_id = "existing-stop"

    runner._exit_position(slot, 100.0, "signal")

    first_order = runner.store.read_orders("trend")[0]
    assert first_order["requested_qty"] == pytest.approx(1.0)
    assert first_order["filled_qty"] == pytest.approx(0.4)
    assert first_order["status"] == "PARTIAL"
    assert slot.position is not None
    assert slot.position.qty == pytest.approx(0.6)
    assert broker.placed == [(0.6, 90.0, 1)]

    # L'intention initiale est terminale mais son reliquat métier reste
    # visible dans la position checkpointée. Le second essai ne réémet que
    # 0.6 BTC et reçoit une séquence logique différente.
    runner._exit_position(slot, 99.0, "signal")

    market_orders = [
        order for order in runner.store.read_orders("trend") if order["order_type"] == "MARKET"
    ]
    assert market_orders[1]["requested_qty"] == pytest.approx(0.6)
    assert market_orders[1]["filled_qty"] == pytest.approx(0.6)
    assert market_orders[1]["logical_order_key"] != market_orders[0]["logical_order_key"]
    assert slot.position is None


def test_canceled_exchange_stop_is_recreated_at_next_tick(tmp_path):
    broker = RecordingBroker(
        [],
        {"id": "existing-stop", "status": "canceled", "amount": 2.0, "filled": 0.0},
    )
    runner, slot = _runner(tmp_path, broker)
    slot.position = _position()
    slot.stop_order_id = "existing-stop"

    runner._monitor_exchange_stops()

    assert broker.placed == [(2.0, 90.0, 1)]
    assert slot.stop_order_id == "stop-1"
    state = runner.store.load_engine_state("trend")
    assert state is not None
    assert state["slots"]["safety"]["stop_order_id"] == "stop-1"


def test_partial_exchange_stop_fails_closed_and_persists_block(tmp_path):
    broker = RecordingBroker(
        [],
        {
            "id": "existing-stop",
            "status": "open",
            "amount": 2.0,
            "filled": 0.75,
            "remaining": 1.25,
            "average": 90.0,
        },
    )
    runner, slot = _runner(tmp_path, broker)
    slot.position = _position()
    slot.stop_order_id = "existing-stop"

    with pytest.raises(ReconciliationRequired, match="état ambigu"):
        runner._monitor_exchange_stops()

    assert runner.reconciliation_required
    assert slot.position is not None
    assert slot.position.qty == pytest.approx(2.0)
    assert runner.store.read_incidents(open_only=True)[0]["kind"] == ("protective_order_uncertain")
    with pytest.raises(ReconciliationRequired, match="démarrage interdit"):
        _runner(tmp_path, broker)


def test_full_exchange_stop_is_materialized_atomically(tmp_path):
    broker = RecordingBroker(
        [],
        {
            "id": "existing-stop",
            "status": "closed",
            "amount": 2.0,
            "filled": 2.0,
            "remaining": 0.0,
            "average": 89.0,
            "fee": {"cost": 0.2},
        },
    )
    runner, slot = _runner(tmp_path, broker)
    slot.position = _position()
    slot.stop_order_id = "existing-stop"
    slot.entry_fee = 0.1

    runner._monitor_exchange_stops()

    assert slot.position is None
    assert slot.stop_order_id is None
    order = runner.store.read_orders("trend")[0]
    assert order["order_type"] == "STOP"
    assert order["status"] == "FILLED"
    assert order["broker_order_id"] == "existing-stop"
    trade = runner.store.read_trades()[0]
    assert trade["qty"] == pytest.approx(2.0)
    assert trade["pnl"] == pytest.approx(-22.3)


def test_stop_filled_while_offline_is_materialized_before_reconciliation(tmp_path):
    broker = RecordingBroker(
        [],
        {
            "id": "existing-stop",
            "status": "closed",
            "amount": 2.0,
            "filled": 2.0,
            "remaining": 0.0,
            "average": 89.0,
        },
    )
    broker.supports_position_reconciliation = True
    broker.net_position = lambda _symbol: 0.0
    runner, slot = _runner(tmp_path, broker)
    runner.notifier = lambda _message: None
    slot.position = _position()
    slot.stop_order_id = "existing-stop"
    stop_event = threading.Event()
    stop_event.set()

    runner.run_forever(stop_event)

    assert slot.position is None
    assert runner.store.read_trades()[0]["reason"] == "stop_exchange"


def test_reconciliation_errors_fail_closed():
    broker = RecordingBroker([])
    broker.supports_position_reconciliation = True
    broker.net_position = lambda _symbol: (_ for _ in ()).throw(
        TimeoutError("exchange unavailable")
    )
    assert reconcile(broker, [], "BTC/USDT") is False


def test_periodic_position_reconciliation_is_bounded_and_fail_closed(tmp_path):
    broker = RecordingBroker([])
    broker.supports_position_reconciliation = True
    remote = {"qty": 2.0}
    calls: list[int] = []

    def net_position(_symbol: str) -> float:
        calls.append(1)
        return remote["qty"]

    broker.net_position = net_position
    runner, slot = _runner(tmp_path, broker)
    runner.notifier = lambda _message: True
    slot.position = _position(qty=2.0)

    runner._maybe_reconcile_position(force=True)
    runner._maybe_reconcile_position()
    assert len(calls) == 1

    runner._last_position_reconciliation_at = runner.clock.time() - 301.0
    remote["qty"] = 0.0
    with pytest.raises(ReconciliationRequired):
        runner._maybe_reconcile_position()

    assert runner.reconciliation_required is True
    incidents = runner.store.read_incidents(open_only=True)
    assert incidents[0]["kind"] == "position_reconciliation_required"


def test_external_broker_is_centrally_disabled():

    with pytest.raises(RuntimeError, match="Safety Baseline"):
        CcxtBroker()


def test_carry_close_failure_marks_unbalanced(tmp_path, monkeypatch):
    class CarryBrokerStub:
        def reconcile(self):
            return True

        def close_position(self, qty, *, intent_id):
            return CarrySagaResult(
                CarrySagaStatus.UNBALANCED,
                spot_qty=qty,
                perp_qty=0.0,
                error="perp leg unknown",
            )

    runner = CarryRunner(
        state_file=tmp_path / "carry.json",
        live_broker=CarryBrokerStub(),
    )
    runner.in_position = True
    runner.execution_state = "OPEN"
    runner.qty = 1.0
    funding = pd.Series(
        [-0.001],
        index=pd.DatetimeIndex([pd.Timestamp("2026-01-01", tz="UTC")]),
    )
    monkeypatch.setattr(runner, "_recent_funding", lambda: funding)

    runner._tick()

    assert runner.execution_state == "UNBALANCED"
    assert runner.in_position is True
    assert runner.qty == pytest.approx(0.0)
    assert runner.spot_qty == pytest.approx(1.0)
    assert runner.perp_qty == pytest.approx(0.0)


def test_paper_runner_recovers_interrupted_order_as_aborted(tmp_path):
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.begin_order(
        "trend",
        "safety",
        "crashed-paper-order",
        "MARKET",
        "BUY",
        1.0,
        "entry",
    )

    LiveRunner(
        [StrategySlot(StaticStrategy(), 1.0, 1_000.0)],
        PaperBroker(),
        _risk(),
        "binance",
        "BTC/USDT",
        database,
    )

    order = store.read_orders("trend")[0]
    assert order["status"] == "RECOVERED_ABORTED"
    assert store.pending_orders("trend") == []


def test_external_runner_refuses_pending_order_after_crash(tmp_path):
    database = tmp_path / "btcquant.db"
    store = StateStore(database)
    store.begin_order(
        "trend",
        "safety",
        "unknown-external-order",
        "MARKET",
        "BUY",
        1.0,
        "entry",
    )

    with pytest.raises(RuntimeError, match="indéterminé"):
        LiveRunner(
            [StrategySlot(StaticStrategy(), 1.0, 1_000.0)],
            RecordingBroker([]),
            _risk(),
            "binance",
            "BTC/USDT",
            database,
        )
