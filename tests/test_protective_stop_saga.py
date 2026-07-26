"""Reprise des stops protecteurs à chaque frontière de crash."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from btcquant.execution.broker import Broker, BrokerOrderSnapshot, Fill
from btcquant.execution.errors import ReconciliationRequired
from btcquant.execution.runner import LiveRunner, StrategySlot
from btcquant.risk import RiskConfig
from btcquant.strategies.base import Position, Strategy


class PowerLoss(BaseException):
    """Coupure brutale non interceptée par les gestionnaires applicatifs."""


class StaticStrategy(Strategy):
    name = "stop-saga"
    timeframe = "4h"

    @staticmethod
    def default_params() -> dict:
        return {}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def entry_signal(self, row: pd.Series) -> int:
        return 0

    def initial_stop(self, row: pd.Series, entry_price: float, direction: int = 1) -> float:
        return entry_price - direction * 10.0


class SagaBroker(Broker):
    supports_stop_orders = True
    supports_order_lookup = True

    def __init__(self) -> None:
        self.remote_by_intent: dict[str, str] = {}
        self.place_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.crash_after_create = False
        self.timeout_before_create = False
        self.timeout_after_create = False
        self.crash_on_cancel = False
        self.timeout_on_cancel = False

    def market_buy(self, qty: float, ref_price: float) -> Fill:
        return Fill(ref_price, qty, 0.0)

    def market_sell(self, qty: float, ref_price: float) -> Fill:
        return Fill(ref_price, qty, 0.0)

    def place_stop(
        self,
        qty: float,
        stop_price: float,
        direction: int = 1,
        *,
        client_order_id: str | None = None,
    ) -> str:
        del qty, stop_price, direction
        assert client_order_id is not None
        self.place_calls.append(client_order_id)
        if self.timeout_before_create:
            raise TimeoutError("aucune confirmation de création")
        remote_id = self.remote_by_intent.setdefault(
            client_order_id,
            f"remote-stop-{len(self.remote_by_intent) + 1}",
        )
        if self.crash_after_create:
            raise PowerLoss("après création distante")
        if self.timeout_after_create:
            raise TimeoutError("réponse de création perdue")
        return remote_id

    def lookup_order(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        remote_id = self.remote_by_intent.get(client_order_id)
        if remote_id is None:
            return None
        return BrokerOrderSnapshot(
            client_order_id=client_order_id,
            broker_order_id=remote_id,
            status="OPEN",
            filled_qty=0.0,
        )

    def cancel_stop(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if self.crash_on_cancel:
            raise PowerLoss("pendant annulation")
        if self.timeout_on_cancel:
            raise TimeoutError("réponse d'annulation perdue")


def _risk() -> RiskConfig:
    return RiskConfig(
        initial_capital=1_000.0,
        risk_per_trade=0.01,
        max_position_pct=0.95,
        vol_target_annual=None,
        max_drawdown_halt=0.5,
        daily_loss_limit=None,
    )


def _position() -> Position:
    return Position(
        entry_time=pd.Timestamp("2026-01-01", tz="UTC"),
        entry_price=100.0,
        qty=1.0,
        stop_price=90.0,
        direction=1,
        best_close=100.0,
    )


def _runner(path: Path, broker: Broker) -> tuple[LiveRunner, StrategySlot]:
    slot = StrategySlot(StaticStrategy(), 1.0, 1_000.0)
    return (
        LiveRunner(
            [slot],
            broker,
            _risk(),
            "binance",
            "BTC/USDT",
            path,
        ),
        slot,
    )


def _restarted_runner(path: Path, broker: Broker) -> tuple[LiveRunner, StrategySlot]:
    runner, slot = _runner(path, broker)
    # En production cette reprise intervient uniquement après reconcile().
    runner._recover_protective_stop_transitions()
    return runner, slot


def _seed_old_stop(runner: LiveRunner, slot: StrategySlot) -> int:
    slot.position = _position()
    old_intent = "old-protective-intent"
    local_id = runner.store.begin_order(
        "trend",
        slot.strategy.name,
        old_intent,
        "STOP",
        "SELL",
        1.0,
        "seed",
        reference_price=90.0,
    )
    runner.store.complete_order(
        local_id,
        status="OPEN",
        broker_order_id="old-stop",
    )
    slot.stop_order_id = "old-stop"
    slot.stop_order_local_id = local_id
    slot.stop_intent_id = old_intent
    runner._save_state()
    return local_id


def test_timeout_after_create_is_resolved_by_client_id(tmp_path):
    broker = SagaBroker()
    runner, slot = _runner(tmp_path / "btcquant.db", broker)
    old_local = _seed_old_stop(runner, slot)
    broker.timeout_after_create = True

    runner._begin_stop_replacement(
        slot,
        qty=1.0,
        stop_price=95.0,
        direction=1,
        reason="ratchet",
    )

    assert len(broker.place_calls) == 1
    assert broker.cancel_calls == ["old-stop"]
    assert slot.stop_order_id == "remote-stop-1"
    assert slot.position is not None and slot.position.stop_price == 95.0
    assert runner.store.read_orders("trend")[0]["status"] == "CANCELED"
    assert runner.store.read_orders("trend")[1]["status"] == "OPEN"
    assert runner.store.read_orders("trend")[0]["id"] == old_local
    assert runner.store.unresolved_orders("trend") == []


def test_crash_after_remote_create_reuses_the_same_intent(tmp_path):
    database = tmp_path / "btcquant.db"
    broker = SagaBroker()
    runner, slot = _runner(database, broker)
    _seed_old_stop(runner, slot)
    broker.crash_after_create = True

    with pytest.raises(PowerLoss, match="création distante"):
        runner._begin_stop_replacement(
            slot,
            qty=1.0,
            stop_price=95.0,
            direction=1,
            reason="ratchet",
        )

    pending = runner.store.read_orders("trend")[1]
    assert pending["status"] == "PENDING"
    persisted = runner.store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["slots"]["stop-saga"]["stop_transition"]["phase"] == "PLACING"

    broker.crash_after_create = False
    restarted, restarted_slot = _runner(database, broker)
    # Le constructeur charge l'état mais ne touche pas l'exchange avant que
    # run_forever ait validé la position distante via reconcile().
    assert restarted_slot.stop_transition is not None
    assert len(broker.place_calls) == 1
    assert broker.cancel_calls == []
    restarted._recover_protective_stop_transitions()

    assert restarted_slot.stop_transition is None
    assert restarted_slot.stop_order_id == "remote-stop-1"
    assert len(broker.place_calls) == 1
    assert restarted.store.read_orders("trend")[1]["status"] == "OPEN"


def test_unconfirmed_placement_stops_runner_then_recovers(tmp_path):
    database = tmp_path / "btcquant.db"
    broker = SagaBroker()
    runner, slot = _runner(database, broker)
    _seed_old_stop(runner, slot)
    broker.timeout_before_create = True

    with pytest.raises(ReconciliationRequired, match="Création du stop non confirmée"):
        runner._begin_stop_replacement(
            slot,
            qty=1.0,
            stop_price=95.0,
            direction=1,
            reason="ratchet",
        )

    assert runner.store.read_orders("trend")[1]["status"] == "PENDING"
    assert runner.store.read_incidents(open_only=True)[0]["kind"] == (
        "protective_stop_transition_pending"
    )

    broker.timeout_before_create = False
    restarted, restarted_slot = _restarted_runner(database, broker)

    assert restarted_slot.stop_order_id == "remote-stop-1"
    assert restarted.store.read_incidents(open_only=True) == []


def test_crash_after_confirmation_resumes_only_the_cancel(tmp_path):
    database = tmp_path / "btcquant.db"
    broker = SagaBroker()
    runner, slot = _runner(database, broker)
    _seed_old_stop(runner, slot)
    broker.crash_on_cancel = True

    with pytest.raises(PowerLoss, match="annulation"):
        runner._begin_stop_replacement(
            slot,
            qty=1.0,
            stop_price=95.0,
            direction=1,
            reason="ratchet",
        )

    persisted = runner.store.load_engine_state("trend")
    assert persisted is not None
    transition = persisted["slots"]["stop-saga"]["stop_transition"]
    assert transition["phase"] == "CANCELING"
    assert runner.store.read_orders("trend")[1]["status"] == "OPEN"

    broker.crash_on_cancel = False
    _, restarted_slot = _restarted_runner(database, broker)

    assert restarted_slot.stop_order_id == "remote-stop-1"
    assert len(broker.place_calls) == 1
    assert broker.cancel_calls == ["old-stop", "old-stop"]


def test_sqlite_failure_after_create_reverts_to_recoverable_placing(tmp_path, monkeypatch):
    database = tmp_path / "btcquant.db"
    broker = SagaBroker()
    runner, slot = _runner(database, broker)
    _seed_old_stop(runner, slot)

    def fail_confirmation(*args, **kwargs):
        raise OSError("sqlite indisponible")

    monkeypatch.setattr(
        runner.store,
        "complete_order_and_checkpoint",
        fail_confirmation,
    )
    with pytest.raises(ReconciliationRequired, match="confirmation SQLite"):
        runner._begin_stop_replacement(
            slot,
            qty=1.0,
            stop_price=95.0,
            direction=1,
            reason="ratchet",
        )

    assert slot.stop_transition is not None
    assert slot.stop_transition["phase"] == "PLACING"
    assert broker.cancel_calls == []

    _, restarted_slot = _restarted_runner(database, broker)

    assert restarted_slot.stop_transition is None
    assert restarted_slot.stop_order_id == "remote-stop-1"
    assert len(broker.place_calls) == 1


def test_crash_after_cancel_before_final_checkpoint_is_idempotent(tmp_path, monkeypatch):
    database = tmp_path / "btcquant.db"
    broker = SagaBroker()
    runner, slot = _runner(database, broker)
    _seed_old_stop(runner, slot)
    original_save = runner.store.save_engine_state

    def crash_on_final_checkpoint(*args, **kwargs):
        if kwargs.get("event_type") == "protective_order_replaced":
            raise PowerLoss("avant checkpoint final")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(runner.store, "save_engine_state", crash_on_final_checkpoint)
    with pytest.raises(PowerLoss, match="checkpoint final"):
        runner._begin_stop_replacement(
            slot,
            qty=1.0,
            stop_price=95.0,
            direction=1,
            reason="ratchet",
        )

    persisted = runner.store.load_engine_state("trend")
    assert persisted is not None
    assert persisted["slots"]["stop-saga"]["stop_transition"]["phase"] == "CANCELING"

    restarted, restarted_slot = _restarted_runner(database, broker)

    assert restarted_slot.stop_transition is None
    assert restarted_slot.stop_order_id == "remote-stop-1"
    assert len(broker.place_calls) == 1
    assert broker.cancel_calls == ["old-stop", "old-stop"]
    orders = restarted.store.read_orders("trend")
    assert [order["status"] for order in orders] == ["CANCELED", "OPEN"]


def test_restart_reprotects_a_checkpointed_position_without_stop(tmp_path):
    database = tmp_path / "btcquant.db"
    broker = SagaBroker()
    runner, slot = _runner(database, broker)
    slot.position = _position()
    runner._save_state()

    restarted, restarted_slot = _restarted_runner(database, broker)

    assert restarted_slot.stop_order_id == "remote-stop-1"
    assert restarted_slot.stop_transition is None
    assert restarted.store.read_orders("trend")[0]["status"] == "OPEN"


def test_cancel_transition_closes_the_stop_journal(tmp_path):
    broker = SagaBroker()
    runner, slot = _runner(tmp_path / "btcquant.db", broker)
    _seed_old_stop(runner, slot)
    slot.position = None
    runner._prepare_stop_cancellation(slot, reason="position_closed")
    runner._save_state()

    runner._resume_stop_transition(slot)

    assert broker.cancel_calls == ["old-stop"]
    assert slot.stop_order_id is None
    assert slot.stop_transition is None
    assert runner.store.read_orders("trend")[0]["status"] == "CANCELED"


def test_cancel_timeout_stops_then_restart_completes(tmp_path):
    database = tmp_path / "btcquant.db"
    broker = SagaBroker()
    runner, slot = _runner(database, broker)
    _seed_old_stop(runner, slot)
    slot.position = None
    runner._prepare_stop_cancellation(slot, reason="position_closed")
    runner._save_state()
    broker.timeout_on_cancel = True

    with pytest.raises(ReconciliationRequired, match="Annulation du stop"):
        runner._resume_stop_transition(slot)

    assert slot.stop_transition is not None
    assert runner.store.read_incidents(open_only=True)[0]["kind"] == (
        "protective_stop_transition_pending"
    )

    broker.timeout_on_cancel = False
    restarted, restarted_slot = _restarted_runner(database, broker)

    assert restarted_slot.stop_transition is None
    assert restarted_slot.stop_order_id is None
    assert restarted.store.read_incidents(open_only=True) == []
