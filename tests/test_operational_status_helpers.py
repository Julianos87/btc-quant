from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from btcquant.operations import status


def test_semantic_health_requires_payload_contract() -> None:
    payloads = {
        "http://127.0.0.1:8666/healthz": {"status": "ok", "kind": "PROCESS_LIVENESS"},
        "http://127.0.0.1:8666/readyz": {"status": "ready", "ready": True},
    }
    result = status._semantic_health(Path("/tmp"), payloads.__getitem__)
    assert result["status"] == status.PASS

    payloads["http://127.0.0.1:8666/readyz"] = {"status": "ready", "ready": False}
    assert status._semantic_health(Path("/tmp"), payloads.__getitem__)["status"] == status.FAIL


def test_semantic_health_keeps_unavailable_evidence_unknown() -> None:
    def unavailable(url: str) -> dict[str, object]:
        raise OSError(url)

    result = status._semantic_health(Path("/tmp"), unavailable)
    assert result["status"] == status.UNKNOWN


def test_service_probe_reports_active_components_and_failures() -> None:
    def runner(*args: str) -> str:
        if args[1] == status.SERVICE_UNITS["carry"]:
            return "ActiveState=inactive\nSubState=dead\nNRestarts=1\n"
        return "ActiveState=active\nSubState=running\nNRestarts=0\n"

    result = status._service_state(runner)
    assert result["status"] == status.FAIL
    assert result["components"]["carry"]["restarts"] == "1"


def test_service_probe_requires_running_substate() -> None:
    def runner(*args: str) -> str:
        if args[1] == status.SERVICE_UNITS["dashboard"]:
            return "ActiveState=active\nSubState=exited\nNRestarts=0\n"
        return "ActiveState=active\nSubState=running\nNRestarts=0\n"

    result = status._service_state(runner)

    assert result["status"] == status.FAIL
    assert result["components"]["dashboard"]["status"] == status.FAIL


def test_service_probe_keeps_command_failure_unknown() -> None:
    def runner(*args: str) -> str:
        raise RuntimeError("probe unavailable")

    result = status._service_state(runner)
    assert result["status"] == status.UNKNOWN


def _create_state_database(path: Path, *, unresolved: bool = False) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE metadata(key TEXT, value TEXT);
            CREATE TABLE orders(local_state TEXT, order_type TEXT, status TEXT);
            CREATE TABLE engine_state(payload TEXT);
            CREATE TABLE incidents(status TEXT, severity TEXT);
            INSERT INTO metadata VALUES ('schema_version', '14');
            INSERT INTO orders VALUES ('{"OPEN" if unresolved else "TERMINAL"}', 'LIMIT',
                                      '{"OPEN" if unresolved else "FILLED"}');
            INSERT INTO engine_state VALUES ('{{}}');
            INSERT INTO incidents VALUES ('CLOSED', 'CRITICAL');
            """
        )


def test_database_reader_is_read_only_and_checks_all_safety_facts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "btcquant.db"
    _create_state_database(database)
    before = database.stat().st_mtime_ns
    result = status._read_database(tmp_path)
    assert result["status"] == status.PASS
    assert result["integrity"] == status.PASS
    assert result["foreign_key_errors"] == 0
    assert result["unresolved_orders"] == 0
    assert database.stat().st_mtime_ns == before


def test_database_reader_fails_closed_on_unresolved_orders(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "btcquant.db"
    _create_state_database(database, unresolved=True)
    assert status._read_database(tmp_path)["status"] == status.FAIL


def test_maturity_reader_exposes_binding_and_limiting_dimension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeStore:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def active_qualification_campaign(self) -> dict[str, object]:
            return {"started_at": "2026-09-15T08:53:19+00:00"}

        def latest_passed_qualification(self) -> None:
            return None

    monkeypatch.setattr(status, "StateStore", FakeStore)
    monkeypatch.setattr(
        status,
        "paper_maturity_status",
        lambda store, root, now: {
            "status": "PAPER_MATURITY_IN_PROGRESS",
            "qualified": False,
            "campaign_id": 4,
            "protocol_version": 3,
            "binding_status": "PASS",
            "binding": {"config_identity": "a" * 64},
            "observation_age_days": 1,
            "required_observation_days": 90,
            "terminal_orders": 1,
            "required_terminal_orders": 50,
            "trend_terminal_orders": 1,
            "required_trend_terminal_orders": 5,
            "closed_trades": 1,
            "required_closed_trades": 30,
            "unresolved_orders": 0,
            "reconciliation_required": 0,
            "open_critical_incidents": 0,
        },
    )
    result = status._read_maturity(tmp_path, datetime.now(UTC))
    assert result["status"] == status.WATCH
    assert result["started_at"].startswith("2026-09-15")
    assert result["limiting_dimension"] == "duration"


def test_qualification_reader_detects_release_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeStore:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def latest_paper_technical_qualification_record(self) -> dict[str, object]:
            return {
                "id": 22,
                "payload": {
                    "status": "PAPER_TECHNICAL_QUALIFIED",
                    "release_sha": "a" * 40,
                    "release_tree": "b" * 40,
                    "schema_version": 14,
                },
            }

    monkeypatch.setattr(status, "StateStore", FakeStore)
    result = status._read_qualification(
        tmp_path, {"active_sha": "c" * 40, "active_tree": "d" * 40, "schema_version": 14}
    )
    assert result["status"] == status.FAIL
    assert result["matches_active_release"] is False


def test_capacity_has_pass_watch_and_fail_bands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    total = 10**12
    monkeypatch.setattr(
        status.shutil,
        "disk_usage",
        lambda root: SimpleNamespace(
            total=total,
            used=total - 10 * status.ADVISORY_FREE_BYTES,
            free=10 * status.ADVISORY_FREE_BYTES,
        ),
    )
    assert status._read_capacity(tmp_path)["status"] == status.PASS
    monkeypatch.setattr(
        status.shutil,
        "disk_usage",
        lambda root: SimpleNamespace(
            total=total,
            used=total - status.OFFICIAL_MIN_FREE_BYTES,
            free=status.OFFICIAL_MIN_FREE_BYTES,
        ),
    )
    assert status._read_capacity(tmp_path)["status"] == status.WATCH
    monkeypatch.setattr(
        status.shutil,
        "disk_usage",
        lambda root: SimpleNamespace(total=total, used=total, free=0),
    )
    assert status._read_capacity(tmp_path)["status"] == status.FAIL


def test_safety_reader_fails_on_testnet_or_markers(tmp_path: Path) -> None:
    def runner(*args: str) -> str:
        assert args[0] == "show"
        return "ActiveState=inactive\nUnitFileState=disabled\n"

    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "TESTNET_APPROVED").touch()
    result = status._read_safety(tmp_path, runner)
    assert result["status"] == status.FAIL
    assert result["markers_present"]


def test_safety_reader_accepts_expected_inactive_disabled_testnet(tmp_path: Path) -> None:
    def runner(*args: str) -> str:
        assert args[0] == "show"
        return "ActiveState=inactive\nUnitFileState=disabled\n"

    (tmp_path / "state").mkdir()

    result = status._read_safety(tmp_path, runner)

    assert result["status"] == status.PASS
    assert result["service_active"] == "inactive"
    assert result["service_enabled"] == "disabled"


def test_backup_reader_requires_matching_verified_archive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    archive = backups / "state-20260915-0840.tar.gz.enc"
    archive.write_bytes(b"isolated fixture")

    class FakeStore:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def latest_paper_technical_qualification_record(self) -> dict[str, object]:
            return {
                "payload": {"backup_verification": {"status": "PASS", "archive_name": archive.name}}
            }

    monkeypatch.setattr(status, "StateStore", FakeStore)
    result = status._read_backup(tmp_path, datetime.now(UTC))
    assert result["status"] == status.PASS
