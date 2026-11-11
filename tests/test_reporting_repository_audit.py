"""Regression coverage for authoritative empty reporting sources (audit M7)."""

from pathlib import Path

import pytest

from btcquant.execution.state_store import StateStore
from btcquant.reporting.repository import ReportingReadError, ReportingRepository


@pytest.fixture
def legacy_sources(tmp_path: Path) -> Path:
    (tmp_path / "state.json").write_text('{"equity": 999}', encoding="utf-8")
    (tmp_path / "equity.csv").write_text("ts,equity\n2020-01-01T00:00:00Z,999\n", encoding="utf-8")
    (tmp_path / "trades.csv").write_text("strategy,pnl\nold,999\n", encoding="utf-8")
    (tmp_path / "flows.csv").write_text(
        "ts,kind,trend_flow,carry_flow\n2020-01-01T00:00:00Z,deposit,999,0\n",
        encoding="utf-8",
    )
    return tmp_path


def assert_empty(repository: ReportingRepository, path: Path) -> None:
    assert repository.read_engine_state("trend", path / "state.json") is None
    assert repository.engine_age_seconds("trend", path / "state.json") is None
    assert repository.read_engine_equity("trend", path / "equity.csv").empty
    assert repository.read_trades().empty
    assert repository.read_flows().empty
    assert list(repository.read_flows().columns) == ["ts", "kind", "trend_flow", "carry_flow"]


def test_empty_database_never_resurrects_legacy_data(legacy_sources: Path):
    StateStore(legacy_sources / "btcquant.db")
    repository = ReportingRepository(legacy_sources)
    assert repository.source_kind == "sqlite"
    assert_empty(repository, legacy_sources)


def test_absent_database_retains_legacy_compatibility(legacy_sources: Path):
    repository = ReportingRepository(legacy_sources)
    assert repository.source_kind == "legacy"
    assert repository.read_engine_state("trend", legacy_sources / "state.json") == {"equity": 999}
    assert repository.engine_age_seconds("trend", legacy_sources / "state.json") is not None
    assert repository.read_engine_equity("trend", legacy_sources / "equity.csv").tolist() == [999]
    assert repository.read_trades()["pnl"].tolist() == [999]
    assert repository.read_flows()["trend_flow"].tolist() == [999]
    assert not repository.database.exists()


def test_legacy_can_be_disabled_explicitly(legacy_sources: Path):
    repository = ReportingRepository(legacy_sources, allow_legacy=False)
    assert repository.source_kind == "none"
    assert_empty(repository, legacy_sources)


@pytest.mark.parametrize("source", ["state", "age", "equity", "trades", "flows"])
def test_corrupt_database_does_not_fall_back(legacy_sources: Path, source: str):
    (legacy_sources / "btcquant.db").write_bytes(b"corrupt database")
    repository = ReportingRepository(legacy_sources)
    reads = {
        "state": lambda: repository.read_engine_state("trend", legacy_sources / "state.json"),
        "age": lambda: repository.engine_age_seconds("trend", legacy_sources / "state.json"),
        "equity": lambda: repository.read_engine_equity("trend", legacy_sources / "equity.csv"),
        "trades": repository.read_trades,
        "flows": repository.read_flows,
    }
    assert repository.source_kind == "sqlite"
    with pytest.raises(ReportingReadError):
        reads[source]()


def test_cached_legacy_is_not_reused_after_database_appears(legacy_sources: Path):
    repository = ReportingRepository(legacy_sources)
    assert not repository.read_trades().empty
    assert not repository.read_engine_equity("trend", legacy_sources / "equity.csv").empty
    StateStore(repository.database)
    assert_empty(repository, legacy_sources)


def test_explicit_migration_populates_previously_empty_canonical_source(legacy_sources: Path):
    (legacy_sources / "equity_trend.csv").write_text(
        "ts,equity\n2020-01-01T00:00:00Z,123\n", encoding="utf-8"
    )
    store = StateStore(legacy_sources / "btcquant.db")
    repository = ReportingRepository(legacy_sources)
    assert repository.read_engine_equity("trend", legacy_sources / "equity_trend.csv").empty
    assert repository.read_flows().empty
    assert repository.read_engine_state("trend", legacy_sources / "state.json") is None

    assert store.migrate_legacy_json("trend", legacy_sources / "state.json")
    imported = store.migrate_legacy_journals(legacy_sources)
    assert imported["equity"] == 1
    assert imported["flows"] == 1
    # Change the cold backups: subsequent reads must still use imported SQLite.
    (legacy_sources / "equity_trend.csv").write_text(
        "ts,equity\n2020-01-01T00:00:00Z,999999\n", encoding="utf-8"
    )
    (legacy_sources / "state.json").write_text('{"equity": 999999}', encoding="utf-8")
    assert repository.read_engine_state("trend", legacy_sources / "state.json") == {"equity": 999}
    assert repository.read_engine_equity("trend", legacy_sources / "equity_trend.csv").tolist() == [
        123
    ]
    assert repository.read_flows()["trend_flow"].tolist() == [999]
