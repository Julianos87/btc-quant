"""Durable Paper stop identity, legacy collision and atomic replay regressions."""

from copy import deepcopy
import json

import pandas as pd
import pytest

from btcquant.execution.broker import PaperBroker
from btcquant.execution.errors import ReconciliationRequired
from btcquant.execution.historical_state_reader import HistoricalStateReader
from btcquant.execution.state_store import StateStore
from btcquant.strategies.base import Position
from test_trend_paper_realism import _runner


def _record(store, **changes):
    trade = {
        "exit_ts": "2026-10-07T01:00:00Z",
        "entry_ts": "2026-10-01T00:00:00Z",
        "strategy": "realism",
        "direction": "LONG",
        "qty": 1.0,
        "entry_price": 100.0,
        "exit_price": 90.0,
        "pnl": -10.0,
        "bars_held": 2,
        "reason": "stop_exchange",
    }
    args = dict(
        engine="trend",
        slot="realism",
        intent_id="observed-stop-paper-stop-1",
        broker_order_id="paper-stop-1",
        side="SELL",
        requested_qty=1.0,
        filled_qty=1.0,
        price=90.0,
        fee=0.0,
        reason="stop_exchange",
        state={"slots": {}, "day": "2026-10-07"},
        trade=trade,
    )
    args.update(changes)
    return store.record_observed_fill_and_checkpoint(**args)


def test_identity_survives_restart_without_active_stops():
    old = PaperBroker(simulate_exchange_stops=True)
    first = old.place_stop(1, 90, 1, client_order_id="old-client")
    fresh = PaperBroker(simulate_exchange_stops=True)
    second = fresh.place_stop(1, 90, 1, client_order_id="new-client")
    assert first != second
    assert first == fresh.place_stop(1, 90, 1, client_order_id="old-client")
    assert first.startswith("paper-stop-v2-")
    assert old.place_stop(1, 90, 1) != fresh.place_stop(1, 90, 1)


def test_restore_legacy_and_replay_intention_validate_economics():
    broker = PaperBroker(simulate_exchange_stops=True)
    broker.restore_protective_stop(
        "paper-stop-1", qty=1, stop_price=90, direction=1, client_order_id="legacy"
    )
    assert broker.place_stop(1, 90, 1, client_order_id="legacy") == "paper-stop-1"
    with pytest.raises(ValueError):
        broker.place_stop(2, 90, 1, client_order_id="legacy")
    with pytest.raises(ValueError):
        broker.restore_protective_stop(
            "paper-stop-1", qty=2, stop_price=90, direction=1, client_order_id="legacy"
        )


def test_true_replay_and_legacy_alias_are_noops(tmp_path):
    store = StateStore(tmp_path / "state.db")
    assert _record(store)
    before = store.load_engine_state("trend")
    assert _record(store) is False
    assert _record(store, intent_id="observed-stop-v2-migrated") is False
    assert store.load_engine_state("trend") == before
    assert len(HistoricalStateReader(store.path).read_trades()) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"filled_qty": 2},
        {"price": 89},
        {"fee": 1},
        {"side": "BUY"},
        {"requested_qty": 2},
        {"slot": "other"},
        {"broker_order_id": "another-stop"},
    ],
)
def test_economic_identity_collision_rolls_back(tmp_path, changes):
    store = StateStore(tmp_path / "state.db")
    assert _record(store)
    before = store.load_engine_state("trend")
    with pytest.raises(ReconciliationRequired, match="OBSERVED_STOP_IDENTITY_CONFLICT"):
        _record(store, **changes)
    assert store.load_engine_state("trend") == before
    assert len(HistoricalStateReader(store.path).read_trades()) == 1


def test_same_economics_different_position_is_not_replay(tmp_path):
    store = StateStore(tmp_path / "state.db")
    assert _record(store)
    trade = HistoricalStateReader(store.path).read_trades()[0]
    trade["entry_ts"] = "2026-10-24T00:00:00Z"
    with pytest.raises(ReconciliationRequired, match="génération"):
        _record(store, trade=trade, intent_id="observed-stop-v2-new-position")


def test_v2_intention_mismatch_with_same_broker_id_is_conflict(tmp_path):
    store = StateStore(tmp_path / "state.db")
    assert _record(store, intent_id="observed-stop-v2-first")
    with pytest.raises(ReconciliationRequired, match="intention"):
        _record(store, intent_id="observed-stop-v2-second")


def test_recycled_legacy_stop_fails_closed_in_real_runner(tmp_path):
    runner = _runner(tmp_path, broker=PaperBroker(simulate_exchange_stops=True))
    runner.notifier = lambda _: True
    assert _record(runner.store)
    slot = runner.slots[0]
    slot.position = Position(
        entry_time=pd.Timestamp("2026-10-24T00:00:00Z"),
        entry_price=100,
        qty=2,
        stop_price=90,
        direction=1,
        best_close=100,
        initial_qty=2,
    )
    runner.broker.restore_protective_stop(
        "paper-stop-1", qty=2, stop_price=90, direction=1, client_order_id="new-client"
    )
    slot.stop_order_id = "paper-stop-1"
    slot.stop_intent_id = "new-client"
    slot.last_bar_ts = pd.Timestamp("2026-10-28T04:00:00Z")
    runner._save_state()
    before = deepcopy(runner.store.load_engine_state("trend"))
    runner.broker.observe_market_price(80)
    with pytest.raises(ReconciliationRequired):
        runner._monitor_exchange_stops()
    assert runner.store.load_engine_state("trend") == before
    assert len(HistoricalStateReader(runner.store.path).read_trades()) == 1


def test_active_paper_stop_is_rekeyed_when_legacy_fill_id_collides(tmp_path):
    broker = PaperBroker(simulate_exchange_stops=True)
    runner = _runner(tmp_path, broker=broker)
    assert _record(
        runner.store,
        broker_order_id="paper-stop-13",
        intent_id="observed-stop-paper-stop-13",
    )

    slot = runner.slots[0]
    position = Position(
        entry_time=pd.Timestamp("2026-10-24T16:00:44.605840Z"),
        entry_price=79_733.0267985684,
        qty=0.033707363791744716,
        stop_price=81_626.99362570583,
        direction=-1,
        bars_held=22,
        best_close=79_567.0,
        initial_qty=0.033707363791744716,
    )
    intent_id = "btq-stop-current-generation"
    local_order_id = runner.store.begin_order(
        "trend",
        slot.strategy.name,
        intent_id,
        "STOP",
        "BUY",
        position.qty,
        "trailing_stop",
        reference_price=position.stop_price,
    )
    runner.store.complete_order(local_order_id, status="OPEN", broker_order_id="paper-stop-13")
    slot.position = position
    slot.stop_order_id = "paper-stop-13"
    slot.stop_order_local_id = local_order_id
    slot.stop_intent_id = intent_id
    runner._save_state()

    runner._load_state()

    new_id = PaperBroker.stop_order_id_for_client(intent_id)
    assert slot.stop_order_id == new_id
    assert runner.store.read_order_by_intent(intent_id)["broker_order_id"] == new_id
    orders = runner.store.read_orders("trend")
    assert any(
        order["intent_id"] == "observed-stop-paper-stop-13"
        and order["broker_order_id"] == "paper-stop-13"
        for order in orders
    )
    events = runner.store.read_events("trend")
    migration = [event for event in events if event["event_type"] == "paper_stop_identity_migrated"]
    assert len(migration) == 1
    payload = json.loads(migration[0]["payload"])
    assert payload["old_broker_order_id"] == "paper-stop-13"
    assert payload["new_broker_order_id"] == new_id


def test_paper_ohlc_stop_ignores_bar_that_started_before_order(tmp_path):
    broker = PaperBroker(simulate_exchange_stops=True)
    runner = _runner(tmp_path, broker=broker)
    slot = runner.slots[0]
    slot.position = Position(
        entry_time=pd.Timestamp("2026-09-30T00:00:00Z"),
        entry_price=100.0,
        qty=1.0,
        stop_price=90.0,
        direction=1,
        bars_held=2,
        best_close=100.0,
        initial_qty=1.0,
    )
    intent_id = "paper-stop-created-mid-bar"
    local_order_id = runner.store.begin_order(
        "trend", slot.strategy.name, intent_id, "STOP", "SELL", 1.0, "test", reference_price=90.0
    )
    broker_id = broker.place_stop(1.0, 90.0, 1, client_order_id=intent_id)
    runner.store.complete_order(local_order_id, status="OPEN", broker_order_id=broker_id)
    slot.stop_order_id = broker_id
    slot.stop_order_local_id = local_order_id
    slot.stop_intent_id = intent_id
    with runner.store._transaction() as connection:
        connection.execute(
            "UPDATE orders SET created_at = ? WHERE id = ?",
            ("2026-10-01T00:05:00+00:00", local_order_id),
        )

    frame = pd.DataFrame(
        [
            [100.0, 101.0, 89.0, 95.0, 1_000.0],
            [100.0, 101.0, 89.0, 95.0, 1_000.0],
        ],
        index=pd.to_datetime(["2026-10-01T00:00:00Z", "2026-10-01T04:00:00Z"]),
        columns=["open", "high", "low", "close", "volume"],
    )
    runner._process_bar(
        slot, 95.0, frame=frame, target_ts=frame.index[0], allow_trade_actions=False
    )
    assert broker.protective_order_snapshot(broker_id).status == "OPEN"

    runner._process_bar(
        slot, 95.0, frame=frame, target_ts=frame.index[1], allow_trade_actions=False
    )
    snapshot = broker.protective_order_snapshot(broker_id)
    assert snapshot.status == "FILLED"
    assert snapshot.filled_at == "2026-10-01T08:00:00+00:00"


def test_new_intention_closes_once_despite_old_history_and_restart(tmp_path):
    runner = _runner(tmp_path, broker=PaperBroker(simulate_exchange_stops=True))
    runner.notifier = lambda _: True
    assert _record(runner.store)
    slot = runner.slots[0]
    slot.position = Position(
        entry_time=pd.Timestamp("2026-10-24T00:00:00Z"),
        entry_price=100,
        qty=2,
        stop_price=90,
        direction=1,
        best_close=100,
        initial_qty=2,
    )
    slot.stop_order_id = runner.broker.place_stop(2, 90, 1, client_order_id="new-client")
    slot.stop_intent_id = "new-client"
    slot.last_bar_ts = pd.Timestamp("2026-10-28T04:00:00Z")
    runner._save_state()
    runner.broker.observe_market_price(80)
    runner._monitor_exchange_stops()
    assert slot.position is None
    assert len(HistoricalStateReader(runner.store.path).read_trades()) == 2
    runner._startup_lock.release()
    restored = _runner(tmp_path, broker=PaperBroker(simulate_exchange_stops=True))
    assert restored.slots[0].position is None
    restored._monitor_exchange_stops()
    assert len(HistoricalStateReader(restored.store.path).read_trades()) == 2
