from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dashboard.app as dashboard_app
from dashboard.carry_dated_status import (
    DEFAULT_COLLECTOR_STATUS_RELATIVE_PATH,
    MAX_SNAPSHOT_AGE_SECONDS,
    STATUS_ENV,
    project_status,
    validate_snapshot,
)


def _snapshot(*, now: datetime | None = None) -> dict:
    stamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    return {
        "strategy": "carry_btc_dated",
        "schema_version": 1,
        "paper_only": True,
        "status": "FLAT",
        "instrument": None,
        "expiry_utc": None,
        "capital": "4000",
        "cash_total": "4000",
        "free_cash": "4000",
        "reserved_margin": "0",
        "stress_cash_buffer": "0",
        "spot_qty": "0",
        "future_qty": "0",
        "hedge_residual": "0",
        "spot_avg_entry": None,
        "future_avg_entry": None,
        "last_settlement_key": None,
        "last_settlement_index": None,
        "last_settlement_period_end": None,
        "settlements_applied": 0,
        "pnl_realized": "0",
        "pnl_unrealized": "0",
        "fees_paid": "0",
        "decision": {
            "action": "WAIT",
            "reason_code": "NO_ELIGIBLE_OPPORTUNITY",
            "reason_message": "No eligible opportunity.",
            "gross_premium": "0",
            "net_before_reserve": "0",
            "risk_reserve": "0",
            "net_after_reserve": "0",
        },
        "decision_at": stamp,
        "reason_code": "NO_ELIGIBLE_OPPORTUNITY",
        "reason_message": "No eligible opportunity.",
        "market": {"spot_timestamp": stamp, "future_timestamp": stamp},
        "accounting_uncertain": False,
        "incident": None,
        "pending_saga_ids": [],
        "last_updated_at": stamp,
    }


def test_missing_snapshot_is_not_projected_as_flat_or_ready(tmp_path):
    report = project_status(tmp_path, now=datetime.now(UTC))

    assert report["status"] == "NOT_DEPLOYED"
    assert report["snapshot"] is None
    assert report["readiness_status"] == "UNKNOWN"
    assert report["economic_evidence"] == "INSUFFICIENT_EVIDENCE"


def test_valid_snapshot_is_observable_but_not_operationally_ready(tmp_path):
    snapshot = _snapshot()
    path = tmp_path / "carry-dated-experiment" / "carry_dated_status.json"
    path.parent.mkdir()
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    report = project_status(tmp_path, now=datetime.now(UTC))

    assert report["status"] == "AVAILABLE"
    assert report["snapshot"]["status"] == "FLAT"
    assert report["readiness_status"] == "UNKNOWN"
    assert report["readiness_reason_codes"] == [
        "COLLECTOR_SYSTEMD_LIVENESS_NOT_PROBED",
        "RUNNER_UNIT_PROBE_NOT_BOUND",
        "DATABASE_INTEGRITY_NOT_BOUND",
        "BACKUP_RESTORE_NOT_BOUND",
    ]
    assert report["components"]["runner"]["unit"] == "btcquant-carry-dated-experimental.service"
    assert report["components"]["collector"]["unit"] == "btcquant-carry-dated-collector.service"
    assert report["components"]["collector"]["status"] == "UNKNOWN"
    assert report["economic_evidence"] == "INSUFFICIENT_EVIDENCE"


def test_collector_heartbeat_and_storage_are_projected_without_claiming_readiness(tmp_path):
    now = datetime.now(UTC)
    dated_dir = tmp_path / "carry-dated-experiment"
    dated_dir.mkdir()
    (dated_dir / "carry_dated_status.json").write_text(json.dumps(_snapshot(now=now)))
    collector = {
        "schema_version": 1,
        "service": "btcquant-carry-dated-collector",
        "paper_only": True,
        "state": "OK",
        "attempted_at": now.isoformat().replace("+00:00", "Z"),
        "retention_goal_days": 365,
        "retention_policy": "goal_only; no automatic archive purge",
        "max_total_bytes": 2_000_000,
        "min_free_bytes": 100,
        "details": {
            "total_bytes": 500,
            "free_bytes": 500,
            "freshest_market_timestamp": now.isoformat().replace("+00:00", "Z"),
            "book_errors": [],
            "book_quality": {"BTC_USDC": [], "BTC_USDC-11NOV26": []},
            "settlement_errors": [],
            "observed_coverage_start_utc": "2026-10-12T08:00:00Z",
            "observed_coverage_end_utc": "2026-10-12T09:00:00Z",
            "archive_segment_count": 1,
            "invalid_archive_manifest_count": 0,
            "archive_integrity_status": "NO_ARCHIVES",
        },
    }
    (dated_dir / DEFAULT_COLLECTOR_STATUS_RELATIVE_PATH.name).write_text(json.dumps(collector))

    report = project_status(tmp_path, now=now)

    assert report["readiness_status"] == "UNKNOWN"
    assert report["components"]["collector"]["status"] == "HEALTHY"
    assert report["components"]["collector"]["unit"] == "btcquant-carry-dated-collector.service"
    assert report["components"]["disk"] == {
        "status": "OK",
        "used_bytes": 500,
        "free_bytes": 500,
        "quota_bytes": 2_000_000,
        "minimum_free_bytes": 100,
    }
    assert report["components"]["market_data"]["status"] == "HEALTHY"
    assert report["economic_evidence"] == "INSUFFICIENT_EVIDENCE"


def test_status_path_is_configurable_and_must_be_absolute(tmp_path):
    custom = tmp_path / "isolated.json"
    custom.write_text(json.dumps(_snapshot()), encoding="utf-8")
    report = project_status(tmp_path, environ={STATUS_ENV: str(custom)})
    assert report["status"] == "AVAILABLE"

    invalid = project_status(tmp_path, environ={STATUS_ENV: "relative.json"})
    assert invalid["status"] == "UNKNOWN"
    assert invalid["reason_code"] == "STATUS_PATH_MUST_BE_ABSOLUTE"


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda item: item.update(schema_version=2), "UNSUPPORTED_SCHEMA_VERSION"),
        (lambda item: item.pop("free_cash"), "REQUIRED_KEYS_MISSING"),
        (lambda item: item.update(paper_only=False), "STRATEGY_OR_MODE_MISMATCH"),
        (lambda item: item.update(status="SOME_NEW_STATE"), "UNKNOWN_STATE"),
        (lambda item: item.update(capital=4000), "CAPITAL_NOT_DECIMAL_STRING"),
        (lambda item: item.update(accounting_uncertain="false"), "ACCOUNTING_FLAG_INVALID"),
    ],
)
def test_invalid_contract_fails_closed(tmp_path, mutate, reason):
    snapshot = _snapshot()
    mutate(snapshot)
    with pytest.raises(ValueError, match=reason):
        validate_snapshot(snapshot)


def test_old_snapshot_and_future_clock_skew_are_not_ready(tmp_path):
    now = datetime.now(UTC)
    old = _snapshot(now=now - timedelta(seconds=MAX_SNAPSHOT_AGE_SECONDS + 1))
    path = tmp_path / "carry-dated-experiment" / "carry_dated_status.json"
    path.parent.mkdir()
    path.write_text(json.dumps(old), encoding="utf-8")
    assert project_status(tmp_path, now=now)["status"] == "STALE"

    ahead = _snapshot(now=now + timedelta(minutes=2))
    path.write_text(json.dumps(ahead), encoding="utf-8")
    report = project_status(tmp_path, now=now)
    assert report["status"] == "UNKNOWN"
    assert report["reason_code"] == "STATUS_CLOCK_SKEW"


@pytest.mark.parametrize(
    ("state", "uncertain", "incident", "expected"),
    [
        ("UNBALANCED", False, None, "UNBALANCED"),
        ("OPEN", True, None, "ACCOUNTING_UNCERTAIN"),
        ("OPEN", False, {"kind": "settlement_collision"}, "INCIDENT_OPEN"),
    ],
)
def test_risk_states_remain_explicit(tmp_path, state, uncertain, incident, expected):
    snapshot = _snapshot()
    snapshot.update(status=state, accounting_uncertain=uncertain, incident=incident)
    path = tmp_path / "carry-dated-experiment" / "carry_dated_status.json"
    path.parent.mkdir()
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    report = project_status(tmp_path, now=datetime.now(UTC))
    assert report["status"] == expected
    assert report["readiness_status"] == "UNKNOWN"


def test_duplicate_json_keys_fail_closed(tmp_path):
    path = tmp_path / "carry-dated-experiment" / "carry_dated_status.json"
    path.parent.mkdir()
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")

    report = project_status(tmp_path)
    assert report["status"] == "UNKNOWN"
    assert report["reason_code"] == "DUPLICATE_JSON_KEY"


def test_dashboard_endpoint_reads_only_separate_dated_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "STATE", tmp_path)
    path = tmp_path / "carry-dated-experiment" / "carry_dated_status.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_snapshot()), encoding="utf-8")

    response = dashboard_app.app.test_client().get(
        "/api/carry-dated", environ_base={"REMOTE_ADDR": "127.0.0.1"}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["kind"] == "CARRY_DATED_PAPER_STATUS"
    assert payload["snapshot"]["strategy"] == "carry_btc_dated"
    assert payload["snapshot"]["paper_only"] is True
