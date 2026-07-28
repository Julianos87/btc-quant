from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from dashboard import app as dashboard
from btcquant.execution.state_store import StateStore
from btcquant.reporting.repository import ReportingReadError, ReportingRepository


def test_sqlite_state_has_priority_over_stale_legacy_json(tmp_path: Path):
    legacy = tmp_path / "live_state_4x.json"
    legacy.write_text('{"source": "legacy"}', encoding="utf-8")
    store = StateStore(tmp_path / "btcquant.db")
    store.save_engine_state("trend", {"source": "sqlite"})

    assert ReportingRepository(tmp_path).read_engine_state("trend", legacy) == {"source": "sqlite"}


def test_csv_cache_is_invalidated_when_file_changes(tmp_path: Path):
    path = tmp_path / "equity_trend.csv"
    path.write_text("ts,equity\n2030-01-01T00:00:00Z,100\n", encoding="utf-8")
    repository = ReportingRepository(tmp_path)
    first = repository.read_engine_equity("trend", path)

    path.write_text(
        "ts,equity\n2030-01-01T00:00:00Z,100\n2030-01-01T00:01:00Z,101\n",
        encoding="utf-8",
    )
    second = repository.read_engine_equity("trend", path)

    assert first.tolist() == [100]
    assert second.tolist() == [100, 101]


def test_torn_flow_rows_are_discarded_in_legacy_fallback(tmp_path: Path):
    (tmp_path / "flows.csv").write_text(
        "ts,kind,trend_flow,carry_flow\n"
        "2030-01-01T00:00:00Z,deposit,100,50\n"
        "2030-01-02T00:00:00Z,deposit,10,\n",
        encoding="utf-8",
    )

    flows = ReportingRepository(tmp_path).read_flows()

    assert len(flows) == 1
    assert flows.iloc[0]["trend_flow"] == 100
    assert pd.notna(flows.iloc[0]["ts"])


def test_corrupt_sqlite_never_falls_back_to_stale_legacy_state(tmp_path: Path):
    legacy = tmp_path / "live_state_4x.json"
    legacy.write_text('{"halted": false}', encoding="utf-8")
    (tmp_path / "btcquant.db").write_bytes(b"not a sqlite database")

    with pytest.raises(ReportingReadError, match="SQLite illisible"):
        ReportingRepository(tmp_path).read_engine_state("trend", legacy)


def test_dashboard_never_hides_corrupt_operational_database(tmp_path: Path, monkeypatch):
    (tmp_path / "live_state_4x.json").write_text('{"slots": {}}', encoding="utf-8")
    (tmp_path / "carry_state.json").write_text('{"equity": 4000}', encoding="utf-8")
    (tmp_path / "btcquant.db").write_bytes(b"not a sqlite database")
    monkeypatch.setattr(dashboard, "STATE", tmp_path)
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", None)
    monkeypatch.setattr(dashboard, "_cached", lambda *_args: None)

    response = dashboard.app.test_client().get("/api/summary")

    assert response.status_code == 503
    assert response.get_json() == {"error": "reporting_unavailable"}
