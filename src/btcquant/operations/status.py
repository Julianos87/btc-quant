"""Read-only operational governance status.

This module deliberately observes existing release, qualification, maturity,
SQLite, service and backup contracts.  It has no write-capable code path.
Unknown evidence is retained as UNKNOWN and never promoted to PASS.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from collections.abc import Callable

from btcquant.execution.paper_technical_qualification import active_paper_release
from btcquant.execution.readiness import paper_maturity_status
from btcquant.execution.readonly_state_db import open_state_db_readonly
from btcquant.execution.state_store import StateStore

PASS = "PASS"
WATCH = "WATCH"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"

# Keep this aligned with deploy/preflight.sh.  This command only observes it.
OFFICIAL_MIN_FREE_BYTES = 1_048_576 * 1024
ADVISORY_FREE_BYTES = OFFICIAL_MIN_FREE_BYTES * 2
BACKUP_FRESH_SECONDS = 26 * 60 * 60
BACKUP_STALE_SECONDS = 7 * 24 * 60 * 60
STATUS_API_VERSION = 1

SERVICE_UNITS = {
    "trend": "btcquant-trend.service",
    "carry": "btcquant-carry.service",
    "shadow": "btcquant-shadow.service",
    "dashboard": "btcquant-dashboard.service",
}
TESTNET_UNIT = "btcquant-hyperliquid-testnet.service"


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def _status_from(values: list[str]) -> str:
    if FAIL in values:
        return FAIL
    if UNKNOWN in values:
        return UNKNOWN
    if WATCH in values:
        return WATCH
    return PASS


def _progress(value: object, required: object) -> float | None:
    try:
        denominator = float(str(required))
        numerator = float(str(value))
    except (TypeError, ValueError):
        return None
    if denominator <= 0:
        return None
    return round(max(0.0, min(100.0, numerator / denominator * 100.0)), 2)


def _run_systemctl(*args: str) -> str:
    result = subprocess.run(
        ["systemctl", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "systemctl probe failed")
    return result.stdout


def _parse_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def _http_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("health payload is not an object")
    return payload


def _semantic_health(root: Path, getter: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    del root  # reserved for a future local socket contract; no filesystem fallback
    result: dict[str, Any] = {}
    checks: list[str] = []
    try:
        health = getter("http://127.0.0.1:8666/healthz")
        health_pass = health.get("status") == "ok" and health.get("kind") == "PROCESS_LIVENESS"
        result["healthz"] = {"status": PASS if health_pass else FAIL, "payload": health}
        checks.append(PASS if health_pass else FAIL)
    except (OSError, ValueError, TypeError, urllib.error.URLError) as error:
        result["healthz"] = {"status": UNKNOWN, "reason": type(error).__name__}
        checks.append(UNKNOWN)
    try:
        ready = getter("http://127.0.0.1:8666/readyz")
        ready_pass = ready.get("status") == "ready" and ready.get("ready") is True
        result["readyz"] = {"status": PASS if ready_pass else FAIL, "payload": ready}
        checks.append(PASS if ready_pass else FAIL)
    except (OSError, ValueError, TypeError, urllib.error.URLError) as error:
        result["readyz"] = {"status": UNKNOWN, "reason": type(error).__name__}
        checks.append(UNKNOWN)
    result["status"] = _status_from(checks)
    return result


def _service_state(
    runner: Callable[..., str],
) -> dict[str, Any]:
    components: dict[str, Any] = {}
    statuses: list[str] = []
    for component, unit in SERVICE_UNITS.items():
        try:
            values = _parse_key_values(
                runner("show", unit, "--property=ActiveState,SubState,NRestarts", "--no-pager")
            )
            active = values.get("ActiveState") == "active"
            state = PASS if active else FAIL
            components[component] = {
                "unit": unit,
                "status": state,
                "active_state": values.get("ActiveState"),
                "sub_state": values.get("SubState"),
                "restarts": values.get("NRestarts"),
            }
            statuses.append(state)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            components[component] = {
                "unit": unit,
                "status": UNKNOWN,
                "reason": type(error).__name__,
            }
            statuses.append(UNKNOWN)
    return {"status": _status_from(statuses), "components": components}


def _read_release(root: Path, source_repository: Path | None) -> dict[str, Any]:
    try:
        release_path, manifest = active_paper_release(root)
        current = root / "current"
        previous = root / "previous"
        dashboard = root / "dashboard-current"
        data: dict[str, Any] = {
            "status": PASS,
            "current_path": str(current),
            "active_release": str(release_path),
            "active_sha": manifest.get("git_sha"),
            "active_tree": manifest.get("git_tree"),
            "schema_version": manifest.get("schema_version_required"),
            "manifest_format_version": manifest.get("manifest_format_version"),
            "manifest_valid": True,
            "previous_target": str(previous.resolve()) if previous.exists() else None,
            "dashboard_target": str(dashboard.resolve()) if dashboard.exists() else None,
        }
    except Exception as error:  # release validation already owns detailed failure reasons
        return {"status": UNKNOWN, "reason": type(error).__name__, "manifest_valid": False}

    source = source_repository
    if source is None:
        candidate = Path("/home/btcquant/btc-quant")
        source = candidate if candidate.exists() else None
    if source is None:
        data["source"] = {"status": UNKNOWN, "reason": "source_repository_unavailable"}
        return data
    try:
        sha = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        data["source"] = {
            "status": PASS,
            "repository": str(source),
            "sha": sha,
            "tree": tree,
            "runtime_drift": sha != data.get("active_sha") or tree != data.get("active_tree"),
        }
        if data["source"]["runtime_drift"]:
            data["source"]["status"] = WATCH
    except (OSError, subprocess.SubprocessError) as error:
        data["source"] = {"status": UNKNOWN, "reason": type(error).__name__}
    return data


def _read_qualification(root: Path, release: dict[str, Any]) -> dict[str, Any]:
    database = root / "state" / "btcquant.db"
    try:
        store = StateStore(database, initialize=False, read_only=True)
        record = store.latest_paper_technical_qualification_record()
        if record is None:
            return {"status": UNKNOWN, "reason": "technical_qualification_missing"}
        payload = record["payload"]
        matches = (
            payload.get("status") == "PAPER_TECHNICAL_QUALIFIED"
            and payload.get("release_sha") == release.get("active_sha")
            and payload.get("release_tree") == release.get("active_tree")
            and payload.get("schema_version") == release.get("schema_version")
        )
        return {
            "status": PASS if matches else FAIL,
            "record_id": record.get("id"),
            "qualified_at": payload.get("qualified_at"),
            "release_sha": payload.get("release_sha"),
            "release_tree": payload.get("release_tree"),
            "schema_version": payload.get("schema_version"),
            "matches_active_release": matches,
        }
    except Exception as error:
        return {"status": UNKNOWN, "reason": type(error).__name__}


def _read_maturity(root: Path, now: datetime) -> dict[str, Any]:
    database = root / "state" / "btcquant.db"
    try:
        store = StateStore(database, initialize=False, read_only=True)
        campaign = store.active_qualification_campaign() or store.latest_passed_qualification()
        maturity = paper_maturity_status(store, root=root, now=now)
    except Exception as error:
        return {"status": UNKNOWN, "reason": type(error).__name__}
    status = maturity.get("status")
    binding = maturity.get("binding_status")
    if binding in {"FAIL", "MISMATCH"}:
        state = FAIL
    elif binding == "UNKNOWN":
        state = UNKNOWN
    elif maturity.get("qualified"):
        state = PASS
    elif status == "PAPER_MATURITY_IN_PROGRESS":
        state = WATCH
    else:
        state = UNKNOWN
    dimensions = {
        "duration": {
            "value": maturity.get("observation_age_days"),
            "required": maturity.get("required_observation_days"),
        },
        "terminal_orders": {
            "value": maturity.get("terminal_orders"),
            "required": maturity.get("required_terminal_orders"),
        },
        "trend_terminal_orders": {
            "value": maturity.get("trend_terminal_orders"),
            "required": maturity.get("required_trend_terminal_orders"),
        },
        "closed_trades": {
            "value": maturity.get("closed_trades"),
            "required": maturity.get("required_closed_trades"),
        },
    }
    for item in dimensions.values():
        item["progress_percent"] = _progress(item["value"], item["required"])
    valid_dimensions = [
        (name, item["progress_percent"])
        for name, item in dimensions.items()
        if isinstance(item["progress_percent"], (int, float))
    ]
    limiting = min(valid_dimensions, key=lambda pair: pair[1])[0] if valid_dimensions else None
    return {
        "status": state,
        "campaign_status": status,
        "campaign_id": maturity.get("campaign_id"),
        "protocol_version": maturity.get("protocol_version"),
        "binding_status": binding,
        "binding": maturity.get("binding"),
        "started_at": campaign.get("started_at") if campaign else None,
        "dimensions": dimensions,
        "limiting_dimension": limiting,
        "reason_code": maturity.get("reason_code"),
        "unresolved_orders": maturity.get("unresolved_orders"),
        "reconciliation_required": maturity.get("reconciliation_required"),
        "open_critical_incidents": maturity.get("open_critical_incidents"),
    }


def _read_database(root: Path) -> dict[str, Any]:
    database = (root / "state" / "btcquant.db").resolve()
    if not database.is_file():
        return {"status": UNKNOWN, "reason": "paper_database_missing", "path": str(database)}
    testnet = root / "state" / "btcquant-testnet.db"
    try:
        if testnet.exists() and database.samefile(testnet):
            return {
                "status": FAIL,
                "reason": "paper_and_testnet_database_are_identical",
                "path": str(database),
            }
    except OSError as error:
        return {"status": UNKNOWN, "reason": type(error).__name__, "path": str(database)}
    try:
        with open_state_db_readonly(database) as connection:
            schema_row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            schema = int(schema_row[0]) if schema_row else None
            integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
            integrity = integrity_row is not None and integrity_row[0] == "ok"
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            unresolved = int(
                connection.execute(
                    "SELECT COUNT(*) FROM orders WHERE local_state != 'TERMINAL' "
                    "AND NOT (order_type='STOP' AND status='OPEN')"
                ).fetchone()[0]
            )
            reconciliation = 0
            for row in connection.execute("SELECT payload FROM engine_state"):
                try:
                    payload = json.loads(str(row[0]))
                except (TypeError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict) and payload.get("reconciliation_required") is True:
                    reconciliation += 1
            critical = int(
                connection.execute(
                    "SELECT COUNT(*) FROM incidents WHERE status='OPEN' AND severity='CRITICAL'"
                ).fetchone()[0]
            )
        safety = (
            integrity
            and not foreign_keys
            and unresolved == 0
            and reconciliation == 0
            and critical == 0
        )
        return {
            "status": PASS if safety and schema == 14 else FAIL,
            "path": str(database),
            "schema_version": schema,
            "expected_schema_version": 14,
            "integrity": PASS if integrity else FAIL,
            "foreign_key_errors": len(foreign_keys),
            "unresolved_orders": unresolved,
            "reconciliation_required": reconciliation,
            "open_critical_incidents": critical,
        }
    except Exception as error:
        return {"status": UNKNOWN, "path": str(database), "reason": type(error).__name__}


def _read_backup(root: Path, now: datetime) -> dict[str, Any]:
    backups = (
        sorted(
            (root / "backups").glob("*.tar.gz.enc"),
            key=lambda item: item.stat().st_mtime,
        )
        if (root / "backups").is_dir()
        else []
    )
    if not backups:
        return {"status": UNKNOWN, "reason": "encrypted_backup_missing"}
    latest = backups[-1]
    age_seconds = max(0.0, now.timestamp() - latest.stat().st_mtime)
    verification: dict[str, Any] | None = None
    try:
        store = StateStore(root / "state" / "btcquant.db", initialize=False, read_only=True)
        record = store.latest_paper_technical_qualification_record()
        if record:
            candidate = record["payload"].get("backup_verification")
            if isinstance(candidate, dict) and candidate.get("archive_name") == latest.name:
                verification = candidate
    except Exception:
        verification = None
    if verification is None:
        state = UNKNOWN
    elif verification.get("status") != PASS:
        state = FAIL
    elif age_seconds <= BACKUP_FRESH_SECONDS:
        state = PASS
    elif age_seconds <= BACKUP_STALE_SECONDS:
        state = WATCH
    else:
        state = FAIL
    return {
        "status": state,
        "latest_archive": latest.name,
        "age_hours": round(age_seconds / 3600, 2),
        "verification": verification
        or {"status": UNKNOWN, "reason": "no_matching_verified_record"},
        "freshness_policy": {"fresh_hours": 26, "stale_days": 7},
    }


def _read_capacity(root: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(root)
        if usage.free < OFFICIAL_MIN_FREE_BYTES:
            state = FAIL
        elif usage.free < ADVISORY_FREE_BYTES:
            state = WATCH
        else:
            state = PASS
        return {
            "status": state,
            "used_percent": round((usage.used / usage.total) * 100, 2) if usage.total else None,
            "free_bytes": usage.free,
            "free_gib": round(usage.free / 1024**3, 2),
            "official_min_free_gib": round(OFFICIAL_MIN_FREE_BYTES / 1024**3, 2),
            "retention": {
                "mode": "DRY_RUN_ONLY",
                "deletions_performed": 0,
                "unknown_or_unverified_preserved": True,
                "policy_reference": "docs/BACKUP_DISASTER_RECOVERY.md",
            },
        }
    except OSError as error:
        return {"status": UNKNOWN, "reason": type(error).__name__}


def _read_safety(root: Path, runner: Callable[..., str]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    states: list[str] = []
    try:
        active = runner("is-active", TESTNET_UNIT).strip()
        enabled = runner("is-enabled", TESTNET_UNIT).strip()
        details.update({"service_active": active, "service_enabled": enabled})
        safe = active not in {"active", "activating"} and enabled not in {"enabled", "static"}
        states.append(PASS if safe else FAIL)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        details["service_probe"] = type(error).__name__
        states.append(UNKNOWN)
    marker_candidates = [
        root / "state" / "HYPERLIQUID_TESTNET_APPROVED",
        root / "state" / "HYPERLIQUID_TESTNET_EXECUTION_ENABLED",
        root / "state" / "TESTNET_APPROVED",
        root / "state" / "TESTNET_EXECUTION_ENABLED",
    ]
    present = [str(path) for path in marker_candidates if path.exists()]
    details["markers_present"] = present
    states.append(FAIL if present else PASS)
    return {"status": _status_from(states), **details}


def collect_status(
    root: Path,
    *,
    source_repository: Path | None = None,
    now: datetime | None = None,
    systemctl: Callable[..., str] = _run_systemctl,
    json_getter: Callable[[str], dict[str, Any]] = _http_json,
) -> dict[str, Any]:
    """Collect one deterministic, read-only operational status snapshot."""

    observed_at = _now(now)
    release = _read_release(root, source_repository)
    qualification = _read_qualification(root, release)
    maturity = _read_maturity(root, observed_at)
    database = _read_database(root)
    services = _service_state(systemctl)
    health = _semantic_health(root, json_getter)
    backup = _read_backup(root, observed_at)
    capacity = _read_capacity(root)
    safety = _read_safety(root, systemctl)
    domains = {
        "release": release,
        "qualification": qualification,
        "maturity": maturity,
        "database": database,
        "services": services,
        "health": health,
        "backup": backup,
        "capacity": capacity,
        "testnet_safety": safety,
    }
    required_states = [str(domains[name].get("status", UNKNOWN)) for name in domains]
    overall = _status_from(required_states)
    return {
        "api_schema_version": STATUS_API_VERSION,
        "kind": "BTCQUANT_OPERATIONAL_STATUS",
        "observed_at": observed_at.isoformat(),
        "overall": overall,
        "exit_code": 0 if overall in {PASS, WATCH} else 1 if overall == FAIL else 2,
        "read_only": True,
        "domains": domains,
    }


def render_human(report: dict[str, Any]) -> str:
    """Render a compact operator view while retaining machine-readable JSON."""

    domains = report.get("domains", {})
    lines = [
        f"BTCQuant operational status: {report.get('overall', UNKNOWN)}",
        f"Observed: {report.get('observed_at', 'UNKNOWN')}",
        "Read-only: YES",
    ]
    for name in sorted(domains):
        domain = domains[name]
        lines.append(f"{name}: {domain.get('status', UNKNOWN)}")
    maturity = domains.get("maturity", {})
    dimensions = maturity.get("dimensions", {})
    if dimensions:
        lines.append("Maturity progress:")
        for name in sorted(dimensions):
            item = dimensions[name]
            lines.append(
                f"  {name}: {item.get('value', 'UNKNOWN')}/{item.get('required', 'UNKNOWN')} "
                f"({item.get('progress_percent', 'UNKNOWN')}%)"
            )
    lines.append(f"Limiting maturity dimension: {maturity.get('limiting_dimension', 'UNKNOWN')}")
    lines.append(
        "Retention: DRY_RUN_ONLY; deletions performed = "
        f"{domains.get('capacity', {}).get('retention', {}).get('deletions_performed', 0)}"
    )
    return "\n".join(lines)
