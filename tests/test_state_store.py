"""Contrat transactionnel du journal SQLite."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from btcquant.execution.errors import AccountingIdentityCollision, MigrationRequiredError

from btcquant.execution.order_state import ExternalOrderState, LocalOrderState
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


def test_applied_deposit_rolls_back_with_engine_states(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "btcquant.db")
    store.register_deposit("monthly:2026-08", 100.0)

    def fail_sync(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(store, "_sync_positions", fail_sync)
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.save_states_and_flows(
            {"trend": _trend_state(1_060.0)},
            [{"kind": "deposit", "trend_flow": 60.0, "carry_flow": 40.0}],
            applied_deposit_ids=["monthly:2026-08"],
        )

    assert store.load_engine_state("trend") is None
    assert store.read_flows() == []
    pending = store.read_deposits(status="PENDING")
    assert len(pending) == 1
    assert pending[0]["deposit_id"] == "monthly:2026-08"


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

    store = StateStore(database, allow_migration=True)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)").fetchall()}
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]

    assert "reference_price" in columns
    assert {
        "logical_order_key",
        "local_state",
        "external_state",
        "remaining_qty",
    } <= columns
    assert version == "14"
    assert store.read_deposits() == []
    assert store.read_incidents() == []


def test_existing_legacy_database_requires_explicit_migration(tmp_path):
    database = tmp_path / "legacy-v4-no-auto-migration.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema_version', '4');
            CREATE TABLE marker(value TEXT NOT NULL);
            INSERT INTO marker VALUES('untouched');
            """
        )

    with pytest.raises(MigrationRequiredError, match="Migration explicite requise"):
        StateStore(database)

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "4"
        )
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "untouched"

    StateStore(database, allow_migration=True)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "14"
        )


def test_schema_v4_migration_is_idempotent_and_never_invents_market_terminality(
    tmp_path,
):
    database = tmp_path / "legacy-v4.db"
    timestamp = "2026-08-01T00:00:00+00:00"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema_version', '4');
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                engine TEXT NOT NULL,
                slot TEXT NOT NULL,
                intent_id TEXT NOT NULL UNIQUE,
                broker_order_id TEXT,
                order_type TEXT NOT NULL,
                side TEXT NOT NULL,
                requested_qty REAL NOT NULL,
                reference_price REAL,
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
        connection.executemany(
            """
            INSERT INTO orders(
                engine, slot, intent_id, order_type, side, requested_qty,
                reference_price, filled_qty, fee, status, reason, created_at, updated_at
            ) VALUES('trend', 'slot', ?, ?, 'BUY', 1, 100, ?, 0, ?, 'legacy', ?, ?)
            """,
            [
                ("market-pending", "MARKET", 0.0, "PENDING", timestamp, timestamp),
                ("market-open", "MARKET", 0.0, "OPEN", timestamp, timestamp),
                ("market-partial", "MARKET", 0.4, "PARTIAL", timestamp, timestamp),
                ("market-rejected", "MARKET", 0.0, "REJECTED", timestamp, timestamp),
                ("market-filled", "MARKET", 1.0, "FILLED", timestamp, timestamp),
                ("carry-rejected", "CARRY_PAIR", 0.0, "REJECTED", timestamp, timestamp),
            ],
        )

    StateStore(database, allow_migration=True)
    store = StateStore(database)  # deuxième passage : migration idempotente
    orders = {order["intent_id"]: order for order in store.read_orders("trend")}

    assert len(orders) == 6
    assert orders["market-pending"]["local_state"] == LocalOrderState.PENDING_RECONCILIATION
    assert orders["market-open"]["local_state"] == LocalOrderState.AWAITING_EXTERNAL
    assert orders["market-open"]["external_state"] == ExternalOrderState.OPEN
    for intent in ("market-partial", "market-rejected"):
        assert orders[intent]["local_state"] == LocalOrderState.PENDING_RECONCILIATION
        assert orders[intent]["external_state"] == ExternalOrderState.UNKNOWN
    assert orders["market-partial"]["remaining_qty"] == pytest.approx(0.6)
    assert orders["market-rejected"]["remaining_qty"] == pytest.approx(1.0)
    assert orders["market-filled"]["local_state"] == LocalOrderState.TERMINAL
    assert orders["market-filled"]["external_state"] == ExternalOrderState.FILLED
    assert orders["carry-rejected"]["local_state"] == LocalOrderState.TERMINAL
    assert orders["carry-rejected"]["external_state"] == ExternalOrderState.REJECTED

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        indexes = {row[1]: row for row in connection.execute("PRAGMA index_list(orders)")}
        connection.execute(
            "UPDATE orders SET logical_order_key='duplicate-key' WHERE intent_id='market-filled'"
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE orders SET logical_order_key='duplicate-key' "
                "WHERE intent_id='carry-rejected'"
            )
        connection.rollback()

    assert version == "14"
    assert indexes["idx_orders_logical_order_key"][2] == 1
    assert indexes["idx_orders_logical_order_key"][4] == 1
    assert store.integrity_check()


def test_schema_migration_rejects_wrongly_named_non_unique_index_and_rolls_back(tmp_path):
    database = tmp_path / "broken-index-v4.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema_version', '4');
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                engine TEXT NOT NULL,
                slot TEXT NOT NULL,
                intent_id TEXT NOT NULL UNIQUE,
                logical_order_key TEXT,
                broker_order_id TEXT,
                order_type TEXT NOT NULL,
                side TEXT NOT NULL,
                requested_qty REAL NOT NULL,
                reference_price REAL,
                filled_qty REAL NOT NULL DEFAULT 0,
                price REAL,
                fee REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                reason TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX idx_orders_logical_order_key ON orders(logical_order_key);
            """
        )

    with pytest.raises(RuntimeError, match="sans garantir l'unicité"):
        StateStore(database, allow_migration=True)

    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
        index = next(
            row
            for row in connection.execute("PRAGMA index_list(orders)")
            if row[1] == "idx_orders_logical_order_key"
        )

    assert version == "4"
    assert "local_state" not in columns
    assert index[2] == 0


def _funding_ledger(event_key: str = "hyperliquid|BTC/USDC:USDC|2030-01-01T01:00:00+00:00") -> dict:
    return {
        "event_key": event_key,
        "venue": "hyperliquid",
        "instrument": "BTC/USDC:USDC",
        "funding_timestamp": "2030-01-01T01:00:00+00:00",
        "native_funding_rate": 0.001,
        "position_generation": "hyperliquid|position-1",
        "funding_notional": 30_000.0,
        "funding_notional_price": 30_000.0,
        "funding_notional_price_source": "OHLC_APPROXIMATION",
        "funding_notional_price_timestamp": "2030-01-01T01:00:00+00:00",
        "funding_pnl": 30.0,
        "borrow_principal": 20_000.0,
        "borrow_rate_ann": 0.10,
        "borrow_dt_seconds": 3_600.0,
        "borrow_cost": 0.228,
        "applied_at": "2030-01-01T01:00:01+00:00",
    }


def _carry_checkpoint(equity: float = 10_000.0) -> dict:
    return {
        "equity": equity,
        "in_position": True,
        "execution_state": "OPEN",
        "qty": 1.0,
        "spot_qty": 1.0,
        "perp_qty": 1.0,
    }


def test_v5_to_v6_funding_ledger_migration_is_additive_and_idempotent(tmp_path):
    database = tmp_path / "v5.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE funding_ledger")
        connection.execute("UPDATE metadata SET value='5' WHERE key='schema_version'")
        connection.commit()

    StateStore(database, allow_migration=True)
    StateStore(database)
    with sqlite3.connect(database) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = {row[1] for row in connection.execute("PRAGMA table_info(funding_ledger)")}

    assert version == "14"
    assert "funding_ledger" in tables
    assert {
        "event_key",
        "funding_timestamp",
        "native_funding_rate",
        "position_generation",
        "borrow_cost",
    } <= columns


def test_funding_ledger_replay_is_exact_and_collision_is_fail_closed(tmp_path):
    store = StateStore(tmp_path / "ledger.db")
    ledger = _funding_ledger()
    state = _carry_checkpoint()
    assert (
        store.apply_carry_accounting_event_and_checkpoint(
            ledger, state, event_payload={"classification": "APPLIED"}
        )
        == "applied"
    )
    persisted_state = store.load_engine_state("carry")
    event_count = len(store.read_events("carry"))

    assert (
        store.apply_carry_accounting_event_and_checkpoint(ledger, {**state, "equity": 9_999.0})
        == "replayed"
    )
    assert store.load_engine_state("carry") == persisted_state
    assert len(store.read_events("carry")) == event_count

    changed = {**ledger, "native_funding_rate": 0.002}
    with pytest.raises(AccountingIdentityCollision):
        store.apply_carry_accounting_event_and_checkpoint(changed, state)
    assert len(store.read_funding_ledger()) == 1
    assert store.load_engine_state("carry") == persisted_state


@pytest.mark.parametrize("failure_point", ["before_insert", "after_ledger", "before_commit"])
def test_funding_ledger_and_checkpoint_roll_back_together(tmp_path, monkeypatch, failure_point):
    store = StateStore(tmp_path / f"{failure_point}.db")
    ledger = _funding_ledger()
    state = _carry_checkpoint()

    if failure_point == "before_insert":

        def fail(*args, **kwargs):
            raise RuntimeError("crash before ledger")

        monkeypatch.setattr(store, "_insert_funding_ledger", fail)
    elif failure_point == "after_ledger":

        def fail(*args, **kwargs):
            raise RuntimeError("crash after ledger")

        monkeypatch.setattr(store, "_sync_positions", fail)
    else:

        def fail(*args, **kwargs):
            raise RuntimeError("crash before commit")

        monkeypatch.setattr(store, "_insert_event", fail)

    with pytest.raises(RuntimeError, match="crash"):
        store.apply_carry_accounting_event_and_checkpoint(ledger, state)

    assert store.read_funding_ledger() == []
    assert store.load_engine_state("carry") is None
    assert store.read_events("carry") == []


def test_concurrent_funding_replay_has_one_owner(tmp_path):
    store = StateStore(tmp_path / "concurrent.db")
    ledger = _funding_ledger()
    state = _carry_checkpoint()

    def apply() -> str:
        return store.apply_carry_accounting_event_and_checkpoint(ledger, state)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: apply(), range(2)))

    assert sorted(results) == ["applied", "replayed"]
    assert len(store.read_funding_ledger()) == 1


# ── rétention du journal ────────────────────────────────────────────────────
# Le journal grossit d'un checkpoint complet par tick et par moteur (~1 440/jour
# pour le trend), chacun portant l'état sérialisé et son SHA-256. Sans purge,
# une campagne de 90 jours dépasse la centaine de milliers de lignes que
# `read_events` chargeait intégralement en mémoire.


def _seed_journal(store, checkpoints: int = 40) -> None:
    for index in range(checkpoints):
        store.save_engine_state("trend", {"slots": {}, "tick": index})
    order_id = store.begin_order(
        "trend", "slot", "retention-intent", "MARKET", "BUY", 1.0, "entry", reference_price=100.0
    )
    store.complete_order(order_id, status="FILLED", filled_qty=1.0, price=100.0)


def test_compact_equity_keeps_five_minute_buckets_before_cutoff(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    for minute in range(12):
        store.append_equity("trend", 10_000.0 + minute, ts=f"2026-01-01T00:{minute:02d}:00+00:00")
    store.append_equity("trend", 10_100.0, ts="2026-01-02T00:00:00+00:00")

    before, after = store.compact_equity("trend", "2026-01-01T12:00:00+00:00", min_rows=1)

    assert before == 13
    remaining = [row["ts"] for row in store.read_equity("trend")]
    assert "2026-01-02T00:00:00+00:00" in remaining
    old = [ts for ts in remaining if ts.startswith("2026-01-01")]
    assert old == [
        "2026-01-01T00:04:00+00:00",
        "2026-01-01T00:09:00+00:00",
        "2026-01-01T00:11:00+00:00",
    ]
    assert after == 4


def test_compact_events_removes_old_routine_checkpoints(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    _seed_journal(store)
    future = "2099-01-01T00:00:00+00:00"

    before, after = store.compact_events(future, keep_per_engine=5)

    assert after < before
    kinds = [event["event_type"] for event in store.read_events()]
    assert kinds.count("checkpoint") == 5


def test_compact_events_never_touches_the_audit_trail(tmp_path):
    """Ordres, fills et flux gardent leur valeur après coup : ils ne sont
    jamais purgés, quel que soit le seuil."""
    store = StateStore(tmp_path / "btcquant.db")
    _seed_journal(store)
    audit_before = [event for event in store.read_events() if event["event_type"] != "checkpoint"]

    store.compact_events("2099-01-01T00:00:00+00:00", keep_per_engine=1)

    audit_after = [event for event in store.read_events() if event["event_type"] != "checkpoint"]
    assert [event["id"] for event in audit_after] == [event["id"] for event in audit_before]


def test_compact_events_spares_recent_checkpoints(tmp_path):
    """Un cutoff dans le passé ne doit rien supprimer : la purge est datée."""
    store = StateStore(tmp_path / "btcquant.db")
    _seed_journal(store)
    before = len(store.read_events())

    _, after = store.compact_events("2000-01-01T00:00:00+00:00", keep_per_engine=1)

    assert after == before


def test_replay_still_works_on_the_retained_window(tmp_path):
    """La reconstruction d'état reste possible après compaction : c'est le
    dernier checkpoint qui compte, et il est conservé."""
    store = StateStore(tmp_path / "btcquant.db")
    _seed_journal(store)
    store.compact_events("2099-01-01T00:00:00+00:00", keep_per_engine=3)

    assert store.replay_engine_state("trend") == store.load_engine_state("trend")


def test_read_events_limit_returns_the_most_recent_in_order(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    _seed_journal(store, checkpoints=10)
    everything = store.read_events()

    recent = store.read_events(limit=3)

    assert [event["id"] for event in recent] == [event["id"] for event in everything[-3:]]


def test_read_events_since_id_paginates(tmp_path):
    store = StateStore(tmp_path / "btcquant.db")
    _seed_journal(store, checkpoints=6)
    everything = store.read_events()
    pivot = everything[2]["id"]

    assert [event["id"] for event in store.read_events(since_id=pivot)] == [
        event["id"] for event in everything[3:]
    ]


def test_json_error_names_the_offending_field(tmp_path):
    """Un checkpoint non sérialisable doit être diagnosticable du premier coup :
    `TypeError: Object of type bool is not JSON serializable` ne dit ni la clé
    ni le moteur concernés, alors qu'il fait échouer toute la transaction."""
    import numpy as np
    import pytest

    store = StateStore(tmp_path / "btcquant.db")

    with pytest.raises(TypeError, match=r"halted \(numpy\.bool"):
        store.save_engine_state("trend", {"slots": {}, "halted": np.bool_(False)})


def test_json_error_reports_nested_fields(tmp_path):
    import numpy as np
    import pytest

    store = StateStore(tmp_path / "btcquant.db")

    with pytest.raises(TypeError, match=r"slots\.d20\.flag"):
        store.save_engine_state("trend", {"slots": {"d20": {"flag": np.bool_(True)}}})
