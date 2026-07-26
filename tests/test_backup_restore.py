"""La restauration est vérifiée dans un répertoire isolé."""

from __future__ import annotations

import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

from scripts.verify_backup import verify_archive
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore


def _archive_with_database(tmp_path: Path) -> Path:
    state = tmp_path / "source" / "state"
    state.mkdir(parents=True)
    database = state / "btcquant.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE orders (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO orders VALUES ('order-1')")
    archive = tmp_path / "state.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(state, arcname="state")
    return archive


def test_backup_is_extracted_and_sqlite_is_checked(tmp_path):
    archive = _archive_with_database(tmp_path)
    restored = tmp_path / "restored"

    result = verify_archive(archive, restored)

    assert result["integrity"] == "ok"
    assert result["rows"] == {"orders": 1}
    assert (restored / "state" / "btcquant.db").is_file()


def test_restore_refuses_a_nonempty_destination(tmp_path):
    archive = _archive_with_database(tmp_path)
    restored = tmp_path / "restored"
    restored.mkdir()
    (restored / "keep.txt").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(FileExistsError):
        verify_archive(archive, restored)


def test_restore_rejects_path_traversal(tmp_path):
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("../../escape")
        member.size = 1
        bundle.addfile(member, io.BytesIO(b"x"))

    with pytest.raises(ValueError, match="Chemin dangereux"):
        verify_archive(archive)


def test_full_state_store_restore_is_restart_safe(tmp_path):
    state = tmp_path / "source" / "state"
    store = StateStore(state / "btcquant.db")
    store.save_engine_state(
        "trend",
        {
            "equity": 1_000.0,
            "cash": 1_000.0,
            "slots": {},
            "halted": False,
        },
    )
    store.save_engine_state(
        "carry",
        {
            "equity": 1_000.0,
            "cash": 1_000.0,
            "in_position": False,
            "qty": 0.0,
            "execution_state": "FLAT",
        },
    )
    archive = tmp_path / "complete-state.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(state, arcname="state")

    result = verify_archive(archive, tmp_path / "clean-room")

    assert result["schema_version"] == SCHEMA_VERSION
    assert result["engine_states"] == ["carry", "trend"]
    assert result["unresolved_orders"] == 0
    assert result["restart_safe"] is True


def test_restore_exposes_unresolved_external_effects(tmp_path):
    state = tmp_path / "source" / "state"
    store = StateStore(state / "btcquant.db")
    store.begin_order("trend", "trend_ls", "ambiguous", "MARKET", "BUY", 1.0, "entry")
    archive = tmp_path / "unsafe-state.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(state, arcname="state")

    result = verify_archive(archive)

    assert result["unresolved_orders"] == 1
    assert result["restart_safe"] is False
