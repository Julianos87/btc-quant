from __future__ import annotations

import runpy
import sys
from pathlib import Path

from btcquant.execution.state_store import SCHEMA_VERSION, StateStore

ROOT = Path(__file__).resolve().parents[1]


def test_schema_remains_v8_after_compaction_change():
    assert SCHEMA_VERSION == 11


def test_compact_script_keeps_lot7_recovery_fence_and_90d_window():
    text = (ROOT / "scripts" / "compact_equity.py").read_text(encoding="utf-8")
    assert "assert_writer_recovery_clear" in text
    assert "KEEP_FULL_DAYS = 90" in text
    assert "KEEP_EVENT_DAYS = 7" in text


def test_inspect_state_is_read_only_and_reports_no_orders(tmp_path, monkeypatch, capsys):
    database = tmp_path / "btcquant.db"
    StateStore(database)
    before = database.read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        ["inspect_state.py", "--database", str(database)],
    )

    runpy.run_path(str(ROOT / "scripts" / "inspect_state.py"), run_name="__main__")

    output = capsys.readouterr().out
    assert "aucun ordre journalisé" in output
    assert database.read_bytes() == before
