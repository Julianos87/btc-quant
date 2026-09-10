"""Characterization and safety tests for operational state observations."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

import btcquant.execution.readonly_state_db as readonly_db_module
from btcquant.execution.operational_state_reader import OperationalStateReader
from btcquant.execution.readonly_state_db import open_state_db_readonly
from btcquant.execution.state_store import StateStore


def _set_engine_timestamp(database, engine: str, timestamp: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO engine_state(engine, payload, updated_at) VALUES(?, '{}', ?) "
            "ON CONFLICT(engine) DO UPDATE SET updated_at=excluded.updated_at",
            (engine, timestamp),
        )


def test_operational_reader_matches_state_store_incident_contract(tmp_path):
    database = tmp_path / "state.db"
    store = StateStore(database)
    first = store.record_incident(
        "trend:warning",
        engine="trend",
        severity="WARNING",
        kind="stale",
        message="first",
    )
    second = store.record_incident(
        "carry:critical",
        engine="carry",
        severity="CRITICAL",
        kind="missing",
        message="second",
    )
    assert store.resolve_incident(first["fingerprint"])
    reader = OperationalStateReader(database)

    all_incidents = reader.read_incidents()
    assert [item["fingerprint"] for item in all_incidents] == [
        "carry:critical",
        "trend:warning",
    ]
    assert [item["fingerprint"] for item in reader.read_incidents(open_only=True)] == [
        "carry:critical"
    ]
    assert [item["fingerprint"] for item in reader.read_incidents(engine="trend")] == [
        "trend:warning"
    ]
    assert reader.read_incidents(open_only=True, engine="carry") == [
        item for item in all_incidents if item["id"] == second["id"]
    ]
    assert reader.read_incidents(engine="unknown") == []


def test_operational_reader_matches_engine_time_contract(tmp_path):
    database = tmp_path / "state.db"
    StateStore(database)
    reader = OperationalStateReader(database)
    current = datetime(2026, 9, 10, 12, tzinfo=UTC)
    observed = current - timedelta(seconds=42.5)
    _set_engine_timestamp(database, "trend", observed.isoformat())
    _set_engine_timestamp(database, "carry", (current + timedelta(seconds=3)).isoformat())

    assert reader.engine_updated_at("trend") == observed
    assert reader.engine_age_seconds("trend", now=current) == 42.5
    assert reader.engine_age_seconds("carry", now=current) == -3.0
    assert reader.engine_updated_at("unknown") is None
    assert reader.engine_age_seconds("unknown", now=current) is None


def test_operational_reader_preserves_naive_and_malformed_timestamp_behavior(tmp_path):
    database = tmp_path / "state.db"
    StateStore(database)
    reader = OperationalStateReader(database)
    current = datetime(2026, 9, 10, 12, tzinfo=UTC)
    _set_engine_timestamp(database, "naive", "2026-09-10T11:59:00")

    assert reader.engine_updated_at("naive") == datetime(2026, 9, 10, 11, 59, tzinfo=UTC)
    with pytest.raises(TypeError):
        reader.engine_age_seconds("naive", now=current)

    _set_engine_timestamp(database, "invalid", "not-a-timestamp")
    with pytest.raises(ValueError):
        reader.engine_updated_at("invalid")
    with pytest.raises(ValueError):
        reader.engine_age_seconds("invalid", now=current)


def test_operational_reader_integrity_contract_and_non_ok_result(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    StateStore(database)
    reader = OperationalStateReader(database)

    assert reader.integrity_check() is True

    class NonOkConnection:
        def execute(self, statement):
            assert statement == "PRAGMA integrity_check"
            return self

        def fetchone(self):
            return ("database disk image is malformed",)

    @contextmanager
    def non_ok_connection(_path):
        yield NonOkConnection()

    monkeypatch.setattr(
        "btcquant.execution.operational_state_reader.open_state_db_readonly",
        non_ok_connection,
    )
    assert reader.integrity_check() is False


def test_operational_reader_is_fail_closed_for_missing_or_invalid_database(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        OperationalStateReader(missing)
    assert not missing.exists()

    schema_missing = tmp_path / "schema-missing.db"
    with sqlite3.connect(schema_missing):
        pass
    reader = OperationalStateReader(schema_missing)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        reader.read_incidents()
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        reader.engine_updated_at("trend")
    assert reader.integrity_check() is True
    with sqlite3.connect(schema_missing) as connection:
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        )

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        OperationalStateReader(corrupt).integrity_check()


def test_operational_reader_has_no_write_api_and_connection_rejects_writes(tmp_path):
    database = tmp_path / "state.db"
    StateStore(database)
    before = (database.stat().st_mtime_ns, database.stat().st_size)
    reader = OperationalStateReader(database)

    assert not hasattr(reader, "execute")
    assert not hasattr(reader, "connection")
    assert not hasattr(reader, "record_incident")
    with (
        open_state_db_readonly(database) as connection,
        pytest.raises(sqlite3.OperationalError, match="readonly|read-only"),
    ):
        connection.execute(
            "INSERT INTO incidents(fingerprint, severity, kind, message, context, status, "
            "occurrences, first_seen, last_seen) "
            "VALUES('forbidden', 'CRITICAL', 'test', 'test', '{}', 'OPEN', 1, 'x', 'x')"
        )
    assert (database.stat().st_mtime_ns, database.stat().st_size) == before


def test_operational_reader_observes_incident_and_heartbeat_writer_commits(tmp_path):
    database = tmp_path / "state.db"
    store = StateStore(database)
    reader = OperationalStateReader(database)
    first_write_done = Event()
    finish_writes = Event()

    def write() -> None:
        store.record_incident(
            "trend:stale",
            engine="trend",
            severity="CRITICAL",
            kind="stale",
            message="stale",
        )
        _set_engine_timestamp(database, "trend", "2026-09-10T12:00:00+00:00")
        first_write_done.set()
        assert finish_writes.wait(timeout=5)
        store.resolve_incident("trend:stale")
        _set_engine_timestamp(database, "trend", "2026-09-10T12:01:00+00:00")

    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(write)
        assert first_write_done.wait(timeout=5)
        assert len(reader.read_incidents(open_only=True)) == 1
        assert reader.engine_updated_at("trend") == datetime(2026, 9, 10, 12, tzinfo=UTC)
        finish_writes.set()
        writer.result(timeout=5)

    assert reader.read_incidents(open_only=True) == []
    assert reader.engine_updated_at("trend") == datetime(2026, 9, 10, 12, 1, tzinfo=UTC)


def test_operational_reader_runs_one_observation_per_call(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    StateStore(database)
    statements: list[str] = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(readonly_db_module.sqlite3, "connect", traced_connect)
    reader = OperationalStateReader(database)
    reader.read_incidents()
    reader.engine_age_seconds("trend")
    reader.engine_updated_at("trend")
    reader.integrity_check()

    observations = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("SELECT", "PRAGMA INTEGRITY_CHECK"))
    ]
    assert len(observations) == 4
