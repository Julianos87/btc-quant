"""Characterization of the historical read contract before extraction."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import btcquant.execution.historical_state_reader as history_module
from btcquant.execution.historical_state_reader import HistoricalStateReader
from btcquant.execution.state_store import StateStore


def _seed_history(database) -> None:
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO equity_samples(engine, ts, equity) VALUES(?, ?, ?)",
            [
                ("trend", "2026-01-03T00:00:00+00:00", 30.5),
                ("carry", "2026-01-02T00:00:00+00:00", -4.25),
                ("trend", "2026-01-01T00:00:00+00:00", 0.0),
            ],
        )
        connection.executemany(
            """
            INSERT INTO trades(
                exit_ts, entry_ts, strategy, direction, qty, entry_price,
                exit_price, pnl, bars_held, reason
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "2026-01-03T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    "trend_ls_55",
                    "SHORT",
                    0.0,
                    101.0,
                    100.0,
                    -2.5,
                    2,
                    "signal",
                ),
                (
                    "2026-01-02T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    "trend_ls_20",
                    "LONG",
                    1.5,
                    100.0,
                    110.0,
                    15.0,
                    6,
                    "stop",
                ),
                (
                    "2026-01-03T00:00:00+00:00",
                    "2026-01-01T12:00:00+00:00",
                    "trend_ls_100",
                    "LONG",
                    2.0,
                    90.0,
                    90.0,
                    0.0,
                    4,
                    "flat",
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO flows(ts, kind, trend_flow, carry_flow) VALUES(?, ?, ?, ?)",
            [
                ("2026-01-03T00:00:00+00:00", "rebalance", -10.0, 10.0),
                ("2026-01-01T00:00:00+00:00", "deposit", 0.0, 50.0),
                ("2026-01-03T00:00:00+00:00", "withdrawal", -5.0, -5.0),
            ],
        )


def test_historical_reader_empty_valid_schema(tmp_path):
    database = tmp_path / "state.db"
    StateStore(database)
    reader = HistoricalStateReader(database)

    assert reader.read_equity("trend") == []
    assert reader.read_trades() == []
    assert reader.read_flows() == []


def test_historical_reader_contract_and_ordering(tmp_path):
    database = tmp_path / "state.db"
    _seed_history(database)
    reader = HistoricalStateReader(database)

    assert reader.read_equity("trend") == [
        {"ts": "2026-01-01T00:00:00+00:00", "equity": 0.0},
        {"ts": "2026-01-03T00:00:00+00:00", "equity": 30.5},
    ]
    assert reader.read_equity("carry") == [{"ts": "2026-01-02T00:00:00+00:00", "equity": -4.25}]
    assert reader.read_equity("unknown") == []

    trades = reader.read_trades()
    assert [row["exit_ts"] for row in trades] == [
        "2026-01-02T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
    ]
    assert [row["id"] for row in trades] == [2, 1, 3]
    assert trades[0] == {
        "id": 2,
        "exit_ts": "2026-01-02T00:00:00+00:00",
        "entry_ts": "2026-01-01T00:00:00+00:00",
        "strategy": "trend_ls_20",
        "direction": "LONG",
        "qty": 1.5,
        "entry_price": 100.0,
        "exit_price": 110.0,
        "pnl": 15.0,
        "bars_held": 6,
        "reason": "stop",
    }

    flows = reader.read_flows()
    assert [row["ts"] for row in flows] == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
    ]
    assert [row["id"] for row in flows] == [2, 1, 3]
    assert flows == [
        {
            "id": 2,
            "ts": "2026-01-01T00:00:00+00:00",
            "kind": "deposit",
            "trend_flow": 0.0,
            "carry_flow": 50.0,
        },
        {
            "id": 1,
            "ts": "2026-01-03T00:00:00+00:00",
            "kind": "rebalance",
            "trend_flow": -10.0,
            "carry_flow": 10.0,
        },
        {
            "id": 3,
            "ts": "2026-01-03T00:00:00+00:00",
            "kind": "withdrawal",
            "trend_flow": -5.0,
            "carry_flow": -5.0,
        },
    ]


def test_historical_reader_missing_database_does_not_create_file(tmp_path):
    database = tmp_path / "missing.db"

    with pytest.raises(FileNotFoundError):
        HistoricalStateReader(database)

    assert not database.exists()


def test_historical_reader_does_not_initialize_missing_schema(tmp_path):
    database = tmp_path / "empty-sqlite.db"
    with sqlite3.connect(database):
        pass

    reader = HistoricalStateReader(database)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        reader.read_equity("trend")

    with sqlite3.connect(database) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == []


def test_historical_reader_is_read_only_and_has_no_write_api(tmp_path):
    database = tmp_path / "state.db"
    _seed_history(database)
    before = (database.stat().st_mtime_ns, database.stat().st_size)
    files_before = {path.name for path in tmp_path.iterdir()}
    reader = HistoricalStateReader(database)

    assert reader.read_equity("trend")
    assert reader.read_trades()
    assert reader.read_flows()
    assert not hasattr(reader, "execute")
    assert not hasattr(reader, "connection")
    assert not hasattr(reader, "append_equity")
    with (
        reader._connect() as connection,
        pytest.raises(sqlite3.OperationalError, match="readonly|read-only"),
    ):
        connection.execute(
            "INSERT INTO equity_samples(engine, ts, equity) VALUES('trend', 'forbidden', 1)"
        )
    assert (database.stat().st_mtime_ns, database.stat().st_size) == before
    assert {path.name for path in tmp_path.iterdir()} == files_before


def test_historical_reader_can_read_while_state_store_writes(tmp_path):
    database = tmp_path / "state.db"
    store = StateStore(database)
    reader = HistoricalStateReader(database)
    first_write_done = Event()
    finish_writes = Event()

    def write() -> None:
        store.append_equity("trend", 1.0, "2026-01-01T00:00:01+00:00")
        first_write_done.set()
        assert finish_writes.wait(timeout=5)
        store.append_equity("trend", 2.0, "2026-01-01T00:00:02+00:00")

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write)
        assert first_write_done.wait(timeout=5)
        assert reader.read_equity("trend") == [{"ts": "2026-01-01T00:00:01+00:00", "equity": 1.0}]
        finish_writes.set()
        writer.result(timeout=5)

    assert [row["equity"] for row in reader.read_equity("trend")] == [1.0, 2.0]


def test_historical_reader_runs_one_select_per_method(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    _seed_history(database)
    statements: list[str] = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(history_module.sqlite3, "connect", traced_connect)
    reader = HistoricalStateReader(database)

    reader.read_equity("trend")
    reader.read_trades()
    reader.read_flows()

    selects = [
        statement for statement in statements if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 3
