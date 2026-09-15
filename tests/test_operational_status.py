from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from btcquant.operations import status


def _domain(state: str = status.PASS) -> dict[str, object]:
    return {"status": state}


def _healthy_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status, "_read_release", lambda root, source: _domain())
    monkeypatch.setattr(status, "_read_qualification", lambda root, release: _domain())
    monkeypatch.setattr(
        status,
        "_read_maturity",
        lambda root, now: {
            "status": status.WATCH,
            "campaign_id": 4,
            "binding_status": "PASS",
            "dimensions": {
                "duration": {"value": 0.5, "required": 90, "progress_percent": 0.56},
                "terminal_orders": {"value": 0, "required": 50, "progress_percent": 0.0},
                "trend_terminal_orders": {"value": 0, "required": 5, "progress_percent": 0.0},
                "closed_trades": {"value": 0, "required": 30, "progress_percent": 0.0},
            },
            "limiting_dimension": "terminal_orders",
        },
    )
    monkeypatch.setattr(status, "_read_database", lambda root: _domain())
    monkeypatch.setattr(status, "_service_state", lambda runner: _domain())
    monkeypatch.setattr(status, "_semantic_health", lambda root, getter: _domain())
    monkeypatch.setattr(status, "_read_backup", lambda root, now: _domain())
    monkeypatch.setattr(status, "_read_capacity", lambda root, **kwargs: _domain())
    monkeypatch.setattr(status, "_read_safety", lambda root, runner: _domain())


def test_status_is_read_only_and_watch_is_nonzero_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _healthy_report(monkeypatch)
    report = status.collect_status(Path("/isolated/root"), now=datetime(2026, 9, 15, tzinfo=UTC))

    assert report["overall"] == status.WATCH
    assert report["exit_code"] == 0
    assert report["read_only"] is True
    assert report["domains"]["maturity"]["campaign_id"] == 4


@pytest.mark.parametrize("state", [status.FAIL, status.UNKNOWN])
def test_failure_and_unknown_never_become_pass(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    _healthy_report(monkeypatch)
    monkeypatch.setattr(status, "_read_database", lambda root: _domain(state))
    report = status.collect_status(Path("/isolated/root"))

    assert report["overall"] == state
    assert report["exit_code"] == (1 if state == status.FAIL else 2)


def test_source_drift_is_visible_without_failing_active_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _healthy_report(monkeypatch)
    release = {
        "status": status.PASS,
        "active_sha": "a" * 40,
        "active_tree": "b" * 40,
        "source": {
            "status": status.WATCH,
            "sha": "c" * 40,
            "tree": "d" * 40,
            "runtime_drift": True,
        },
    }
    monkeypatch.setattr(status, "_read_release", lambda root, source: release)

    report = status.collect_status(Path("/isolated/root"))

    assert report["overall"] == status.WATCH
    assert report["domains"]["release"]["source"]["runtime_drift"] is True


def test_progress_is_bounded_and_deterministic() -> None:
    assert status._progress(5, 10) == 50.0
    assert status._progress(-1, 10) == 0.0
    assert status._progress(100, 10) == 100.0
    assert status._progress(1, 0) is None
    assert status._progress("unknown", 10) is None


def test_human_output_exposes_limiting_dimension_without_secret_fields() -> None:
    report = {
        "overall": status.WATCH,
        "observed_at": "2026-09-15T00:00:00+00:00",
        "domains": {
            "maturity": {
                "dimensions": {
                    "terminal_orders": {"value": 0, "required": 50, "progress_percent": 0.0}
                },
                "limiting_dimension": "terminal_orders",
            },
            "capacity": {"retention": {"deletions_performed": 0}},
            "alerts": {"status": status.PASS},
        },
    }
    output = status.render_human(report)

    assert "terminal_orders" in output
    assert "DRY_RUN_ONLY" in output
    assert "BACKUP_ENCRYPTION_KEY" not in output
    assert "PRIVATE_KEY" not in output
