"""Contrat transactionnel du journal SQLite."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from btcquant.execution.state_store import StateStore


def _trend_state(cash: float = 1_000.0) -> dict:
    return {
        "slots": {
            "trend_ls_20": {
                "cash": cash,
                "position": {
                    "entry_time": "2026-01-01T00:00:00+00:00",
                    "entry_price": 100.0,
                    "qty": 2.0,
                    "stop_price": 90.0,
                    "direction": 1,
                    "bars_held": 3,
                    "best_close": 105.0,
                },
                "stop_order_id": "stop-1",
                "entry_fee": 1.2,
                "last_bar_ts": "2026-01-01T04:00:00+00:00",
            }
        },
        "peak_equity": cash,
        "halted": False,
    }


def test_legacy_json_migration_is_one_shot(tmp_path):
    legacy = tmp_path / "live_state_4x.json"
    legacy.write_text(json.dumps(_trend_state()), encoding="utf-8")
    store = StateStore(tmp_path / "btcquant.db")

    assert store.migrate_legacy_json("trend", legacy) is True
    assert store.migrate_legacy_json("trend", legacy) is False
    assert store.load_engine_state("trend") == _trend_state()
    events = store.read_events("trend")
    assert [event["event_type"] for event in events] == ["legacy_json_migrated"]
    assert store.integrity_check()


def test_legacy_csv_journals_are_imported_once(tmp_path):
    (tmp_path / "equity_trend.csv").write_text(
        "ts,equity\n2026-01-01T00:00:00+00:00,6000\n",
        encoding="utf-8",
    )
    (tmp_path / "trades.csv").write_text(
        "exit_ts,entry_ts,strategy,direction,qty,entry_price,exit_price,pnl,bars_held,reason\n"
        "2026-01-02T00:00:00+00:00,2026-01-01T00:00:00+00:00,"
        "trend_ls_20,LONG,1,100,110,10,6,signal\n",
        encoding="utf-8",
    )
    (tmp_path / "flows.csv").write_text(
        "ts,kind,trend_flow,carry_flow\n2026-01-03T00:00:00+00:00,deposit,60,40\n",
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "btcquant.db")

    first = store.migrate_legacy_journals(tmp_path)
    second = store.migrate_legacy_journals(tmp_path)

    assert first == {"equity": 1, "trades": 1, "flows": 1}
    assert second == {"equity": 0, "trades": 0, "flows": 0}
    assert len(store.read_equity("trend")) == 1
    assert len(store.read_trades()) == 1
    assert len(store.read_flows()) == 1


def test_checkpoint_rolls_back_state_positions_and_event(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "btcquant.db")
    original = _trend_state(1_000.0)
    store.save_engine_state("trend", original)
    event_count = len(store.read_events())

    def fail_sync(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store, "_sync_positions", fail_sync)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.save_engine_state("trend", _trend_state(2_000.0))

    assert store.load_engine_state("trend") == original
    assert len(store.read_events()) == event_count
    assert store.integrity_check()


def test_order_intent_survives_until_explicit_completion(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    order_id = store.begin_order(
        "trend",
        "trend_ls_20",
        "intent-123",
        "MARKET",
        "BUY",
        1.5,
        "entry",
    )

    assert [order["id"] for order in store.pending_orders("trend")] == [order_id]
    store.complete_order(
        order_id,
        status="PARTIAL",
        filled_qty=1.0,
        price=100.0,
        fee=0.1,
    )

    assert store.pending_orders("trend") == []
    order = store.read_orders("trend")[0]
    assert order["status"] == "PARTIAL"
    assert order["filled_qty"] == pytest.approx(1.0)
    assert [event["event_type"] for event in store.read_events("trend")] == [
        "order_intent",
        "order_updated",
    ]


def test_order_fill_position_and_trade_commit_atomically(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    order_id = store.begin_order("trend", "trend_ls_20", "exit-1", "MARKET", "SELL", 2.0, "signal")
    flat = _trend_state()
    flat["slots"]["trend_ls_20"]["position"] = None
    trade = {
        "exit_ts": "2026-01-02T00:00:00+00:00",
        "entry_ts": "2026-01-01T00:00:00+00:00",
        "strategy": "trend_ls_20",
        "direction": "LONG",
        "qty": 2.0,
        "entry_price": 100.0,
        "exit_price": 110.0,
        "pnl": 20.0,
        "bars_held": 6,
        "reason": "signal",
    }

    store.complete_order_and_checkpoint(
        order_id,
        engine="trend",
        state=flat,
        status="FILLED",
        filled_qty=2.0,
        price=110.0,
        trade=trade,
    )

    assert store.read_orders("trend")[0]["status"] == "FILLED"
    assert store.load_engine_state("trend") == flat
    assert store.read_trades()[0]["pnl"] == pytest.approx(20.0)


def test_order_checkpoint_failure_leaves_order_pending(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "btcquant.db")
    order_id = store.begin_order(
        "trend", "trend_ls_20", "exit-crash", "MARKET", "SELL", 2.0, "signal"
    )

    def fail_sync(*args, **kwargs):
        raise RuntimeError("disk failure")

    monkeypatch.setattr(store, "_sync_positions", fail_sync)
    with pytest.raises(RuntimeError, match="disk failure"):
        store.complete_order_and_checkpoint(
            order_id,
            engine="trend",
            state=_trend_state(),
            status="FILLED",
            filled_qty=2.0,
        )

    assert store.read_orders("trend")[0]["status"] == "PENDING"
    assert store.load_engine_state("trend") is None
    assert store.read_trades() == []


def test_rebalance_commits_both_states_and_flows_together(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    trend = _trend_state(6_000.0)
    carry = {"equity": 4_000.0, "in_position": False, "execution_state": "FLAT"}

    store.save_states_and_flows(
        {"trend": trend, "carry": carry},
        [
            {"kind": "deposit", "trend_flow": 60.0, "carry_flow": 40.0},
            {"kind": "rebalance", "trend_flow": -10.0, "carry_flow": 10.0},
        ],
    )

    assert store.load_engine_state("trend") == trend
    assert store.load_engine_state("carry") == carry
    assert [row["kind"] for row in store.read_flows()] == ["deposit", "rebalance"]
    assert store.integrity_check()


def test_engine_state_is_replayable_from_hashed_events(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    first = _trend_state(1_000.0)
    final = _trend_state(1_234.0)

    store.save_engine_state("trend", first)
    store.save_engine_state("trend", final)

    assert store.replay_engine_state("trend") == final
    assert store.replay_engine_state("trend") == store.load_engine_state("trend")


def test_replay_detects_a_tampered_state_event(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", _trend_state())
    with sqlite3.connect(store.path) as connection:
        event = connection.execute(
            "SELECT id, payload FROM events WHERE engine = 'trend'"
        ).fetchone()
        assert event is not None
        payload = json.loads(event[1])
        payload["state"]["peak_equity"] = 9_999.0
        connection.execute(
            "UPDATE events SET payload = ? WHERE id = ?",
            (json.dumps(payload), event[0]),
        )

    with pytest.raises(RuntimeError, match="Hash d'état invalide"):
        store.replay_engine_state("trend")


def test_concurrent_equity_writes_are_serialized(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")

    def write(index: int) -> None:
        store.append_equity("trend", float(index), f"2026-01-01T00:00:{index:02d}+00:00")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    rows = store.read_equity("trend")
    assert len(rows) == 40
    assert {row["equity"] for row in rows} == {float(index) for index in range(40)}
    assert store.integrity_check()


def test_schema_v1_is_migrated_with_execution_observability(tmp_path):
    database = tmp_path / "legacy-v1.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema_version', '1');
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                engine TEXT NOT NULL,
                slot TEXT NOT NULL,
                intent_id TEXT NOT NULL UNIQUE,
                broker_order_id TEXT,
                order_type TEXT NOT NULL,
                side TEXT NOT NULL,
                requested_qty REAL NOT NULL,
                filled_qty REAL NOT NULL DEFAULT 0,
                price REAL,
                fee REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                reason TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    store = StateStore(database)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)").fetchall()}
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]

    assert "reference_price" in columns
    assert version == "3"
    assert store.read_incidents() == []
