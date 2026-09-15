from __future__ import annotations

import sqlite3
from pathlib import Path

from btcquant.operations import status


def test_paper_database_cannot_alias_testnet_database(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "btcquant.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT, value TEXT)")
    testnet = state / "btcquant-testnet.db"
    testnet.symlink_to(database)

    result = status._read_database(tmp_path)

    assert result["status"] == status.FAIL
    assert result["reason"] == "paper_and_testnet_database_are_identical"


def test_safety_probe_uses_repository_testnet_unit_name(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(*args: str) -> str:
        calls.append(args)
        return "ActiveState=inactive\nUnitFileState=disabled\n"

    (tmp_path / "state").mkdir()
    result = status._read_safety(tmp_path, runner)

    assert result["status"] == status.PASS
    assert calls == [
        (
            "show",
            "btcquant-hyperliquid-testnet.service",
            "--property=ActiveState,UnitFileState",
            "--no-pager",
        )
    ]
