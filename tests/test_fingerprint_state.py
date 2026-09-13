from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from btcquant.execution.state_store import StateStore
import scripts.fingerprint_state as fingerprint_state
from scripts.fingerprint_state import _canonical_rows, fingerprint_database


def _snapshot(tmp_path: Path) -> Path:
    database = tmp_path / "snapshot" / "btcquant.db"
    StateStore(database)
    return database


def test_fingerprint_is_deterministic_and_non_secret(tmp_path: Path) -> None:
    database = _snapshot(tmp_path)

    first = fingerprint_database(database)
    second = fingerprint_database(database)

    assert first == second
    assert first["schema_version"] == 14
    assert first["integrity"] == "ok"
    assert first["foreign_key_errors"] == 0
    assert first["tables"]["orders"]["count"] == 0
    assert set(first["tables"]) == {
        "orders",
        "external_fills",
        "financial_fill_applications",
        "trades",
        "engine_state",
        "incidents",
        "qualification_campaigns",
        "readiness_reports",
    }


def test_fingerprint_does_not_mutate_snapshot(tmp_path: Path) -> None:
    database = _snapshot(tmp_path)
    before = database.read_bytes()

    fingerprint_database(database)

    assert database.read_bytes() == before


def test_fingerprint_rejects_production_path() -> None:
    with pytest.raises(ValueError, match="production database"):
        fingerprint_database("/opt/btcquant/state/btcquant.db")


def test_fingerprint_rejects_database_outside_snapshot_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fingerprint_state, "SNAPSHOT_ROOTS", (tmp_path / "approved",))
    outside = tmp_path / f"outside-{tmp_path.name}.db"
    try:
        with sqlite3.connect(outside) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")

        with pytest.raises(ValueError, match="approved temporary snapshot root"):
            fingerprint_database(outside)
    finally:
        outside.unlink(missing_ok=True)


def test_canonical_rows_support_without_rowid_and_typed_values() -> None:
    def build(rows: list[tuple[str, bytes | None, float]]) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE sample(key TEXT PRIMARY KEY, payload BLOB, amount REAL) WITHOUT ROWID"
        )
        connection.executemany("INSERT INTO sample VALUES (?, ?, ?)", rows)
        return connection

    first = build([("b", None, 1.25), ("a", b"\x00\xff", 2.5)])
    second = build([("a", b"\x00\xff", 2.5), ("b", None, 1.25)])
    try:
        assert _canonical_rows(first, "sample") == _canonical_rows(second, "sample")
    finally:
        first.close()
        second.close()


def test_fingerprint_fails_closed_on_corrupt_snapshot(tmp_path: Path) -> None:
    database = _snapshot(tmp_path)
    with database.open("r+b") as handle:
        handle.seek(4096)
        handle.write(b"not-a-valid-sqlite-page")

    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        fingerprint_database(database)
