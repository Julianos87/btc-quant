"""Evidence-driven producer for durable PAPER technical qualification.

The storage primitive is deliberately the final operation. Operators cannot
provide release identity or PASS assertions: facts come from the active
immutable release, fixed local checks, the PAPER database, and runtime probes.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from btcquant.config import load_config, runtime_execution_from_config
from btcquant.deployment import (
    DeploymentProtocolError,
    sha256_file,
    validate_release_manifest,
)

from .state_store import SCHEMA_VERSION, StateStore

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REQUIRED_SERVICES = (
    "btcquant-trend.service",
    "btcquant-carry.service",
    "btcquant-shadow.service",
    "btcquant-dashboard.service",
)


class QualificationFailed(RuntimeError):
    """Evidence failed before any qualification write."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class QualificationProbes:
    run: Callable[[Sequence[str], Path], Mapping[str, Any]]
    get_json: Callable[[str], Mapping[str, Any]]
    service_active: Callable[[str], bool]
    verify_backup: Callable[[Path, Path], Mapping[str, Any]]


def _run(command: Sequence[str], cwd: Path) -> Mapping[str, Any]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    return {
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "returncode": result.returncode,
        "command": list(command),
    }


def _get_json(url: str) -> Mapping[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310
        if response.status != 200:
            raise QualificationFailed("HEALTH_FAILED", f"{url} returned HTTP {response.status}")
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise QualificationFailed("HEALTH_FAILED", f"{url} did not return a JSON object")
    return value


def _service_active(unit: str) -> bool:
    return (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            check=False,
            timeout=10,
        ).returncode
        == 0
    )


def _verify_backup(archive: Path, release: Path) -> Mapping[str, Any]:
    result = subprocess.run(
        [
            str(release / "venv/bin/python"),
            str(release / "scripts/verify_backup.py"),
            str(archive),
        ],
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        return {"status": "FAIL", "reason": "verification_failed"}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "FAIL", "reason": "malformed_verifier_output"}
    return (
        payload
        if isinstance(payload, dict)
        else {
            "status": "FAIL",
            "reason": "malformed_verifier_output",
        }
    )


DEFAULT_PROBES = QualificationProbes(_run, _get_json, _service_active, _verify_backup)


def _require(condition: bool, reason: str, detail: str) -> None:
    if not condition:
        raise QualificationFailed(reason, detail)


def _active_release(root: Path) -> tuple[Path, dict[str, Any]]:
    current = root / "current"
    _require(current.is_symlink(), "RELEASE_MANIFEST_MISMATCH", "current is not a symlink")
    release = current.resolve(strict=True)
    _require(
        release.parent == (root / "releases").resolve(),
        "RELEASE_MANIFEST_MISMATCH",
        "active release is outside the immutable releases directory",
    )
    _require(
        _SHA_RE.fullmatch(release.name) is not None,
        "RELEASE_MANIFEST_MISMATCH",
        "release directory is not a SHA",
    )
    try:
        manifest = dict(validate_release_manifest(release, release.name))
    except DeploymentProtocolError as error:
        raise QualificationFailed("RELEASE_MANIFEST_MISMATCH", str(error)) from error
    tree = manifest.get("git_tree")
    _require(
        isinstance(tree, str) and _SHA_RE.fullmatch(tree) is not None,
        "RELEASE_MANIFEST_MISMATCH",
        "manifest tree is invalid",
    )
    _require(
        manifest.get("schema_version_required") == SCHEMA_VERSION,
        "DB_SCHEMA_MISMATCH",
        "release schema contract differs",
    )
    config_path = release / "environments/paper/config.yaml"
    _require(
        manifest.get("config_file_sha256") == sha256_file(config_path),
        "RELEASE_MANIFEST_MISMATCH",
        "PAPER config hash differs from the release manifest",
    )
    return release, manifest


def _paper_database(root: Path, release: Path) -> Path:
    try:
        config = load_config(release / "environments/paper/config.yaml")
        execution = runtime_execution_from_config(config)
    except (OSError, TypeError, ValueError) as error:
        raise QualificationFailed("ENVIRONMENT_NOT_PAPER", "invalid PAPER config") from error
    _require(
        config.get("environment") == "paper" and execution.mode == "paper",
        "ENVIRONMENT_NOT_PAPER",
        "active config is not PAPER",
    )
    configured = Path(execution.require_state_file())
    database = (configured if configured.is_absolute() else root / configured).resolve()
    expected = (root / "state/btcquant.db").resolve()
    _require(
        database == expected,
        "ENVIRONMENT_NOT_PAPER",
        "configured database is not canonical PAPER state",
    )
    testnet = root / "state/btcquant-testnet.db"
    if testnet.exists() and database.exists():
        _require(
            not database.samefile(testnet),
            "ENVIRONMENT_NOT_PAPER",
            "PAPER and TESTNET database are identical",
        )
    _require(database.is_file(), "DB_INTEGRITY_FAILED", "PAPER database is missing")
    return database


def _database_evidence(
    database: Path, expected_schema: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        schema_row = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        schema = int(schema_row[0]) if schema_row else None
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        unresolved = int(
            connection.execute(
                "SELECT COUNT(*) FROM orders WHERE local_state != 'TERMINAL' "
                "AND NOT (order_type = 'STOP' AND status = 'OPEN')"
            ).fetchone()[0]
        )
        incidents = int(
            connection.execute(
                "SELECT COUNT(*) FROM incidents WHERE status='OPEN' AND severity='CRITICAL'"
            ).fetchone()[0]
        )
        states = connection.execute("SELECT engine, payload FROM engine_state").fetchall()
    reconciliation = 0
    for engine, raw in states:
        try:
            state = json.loads(str(raw))
        except json.JSONDecodeError as error:
            raise QualificationFailed(
                "PENDING_RECONCILIATION", f"malformed engine state for {engine}"
            ) from error
        if not isinstance(state, dict) or state.get("reconciliation_required") is True:
            reconciliation += 1
    _require(
        schema == expected_schema,
        "DB_SCHEMA_MISMATCH",
        f"observed={schema}, required={expected_schema}",
    )
    _require(
        integrity == "ok" and not foreign_keys,
        "DB_INTEGRITY_FAILED",
        f"integrity={integrity}, foreign_key_errors={len(foreign_keys)}",
    )
    _require(unresolved == 0, "UNRESOLVED_ORDERS", str(unresolved))
    _require(reconciliation == 0, "PENDING_RECONCILIATION", str(reconciliation))
    _require(incidents == 0, "OPEN_CRITICAL_INCIDENT", str(incidents))
    migration = {
        "status": "PASS",
        "schema_version": schema,
        "required_schema_version": expected_schema,
        "migration_required": False,
        "integrity": integrity,
        "foreign_key_errors": 0,
    }
    safety = {
        "unresolved_orders": unresolved,
        "reconciliation_required_engines": reconciliation,
        "open_critical_incidents": incidents,
    }
    return migration, safety


def _fixed_test_evidence(
    release: Path, probes: QualificationProbes
) -> tuple[dict[str, Any], dict[str, Any]]:
    python = str(release / "venv/bin/python")
    checks = (
        ("full_suite", (python, "-m", "pytest", "-q")),
        ("ruff", (str(release / "venv/bin/ruff"), "check", ".")),
        ("format", (str(release / "venv/bin/ruff"), "format", "--check", ".")),
        ("mypy", (str(release / "venv/bin/mypy"), "src/btcquant")),
    )
    results: dict[str, Any] = {}
    for name, command in checks:
        try:
            result = dict(probes.run(command, release))
        except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
            raise QualificationFailed("TEST_QUALIFICATION_FAILED", name) from error
        _require(result.get("status") == "PASS", "TEST_QUALIFICATION_FAILED", name)
        results[name] = result
    rollback_command = (
        python,
        "-m",
        "pytest",
        "-q",
        "tests/test_deployment_protocol.py",
        "tests/test_security_hardening.py",
    )
    try:
        rollback = dict(probes.run(rollback_command, release))
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        raise QualificationFailed("TEST_QUALIFICATION_FAILED", "rollback protocol") from error
    _require(
        rollback.get("status") == "PASS",
        "TEST_QUALIFICATION_FAILED",
        "rollback protocol",
    )
    return (
        {"status": "PASS", "checks": results},
        {"status": "PASS", "kind": "isolated_protocol_tests", "result": rollback},
    )


def _runtime_evidence(probes: QualificationProbes, safety: Mapping[str, Any]) -> dict[str, Any]:
    try:
        health = dict(probes.get_json("http://127.0.0.1:8666/healthz"))
    except (OSError, TypeError, ValueError) as error:
        raise QualificationFailed("HEALTH_FAILED", "healthz unavailable") from error
    try:
        ready = dict(probes.get_json("http://127.0.0.1:8666/readyz"))
    except (OSError, TypeError, ValueError) as error:
        raise QualificationFailed("READINESS_FAILED", "readyz unavailable") from error
    _require(
        health.get("status") == "ok" and health.get("kind") == "PROCESS_LIVENESS",
        "HEALTH_FAILED",
        "healthz semantic payload failed",
    )
    _require(
        ready.get("status") == "ready" and ready.get("ready") is True,
        "READINESS_FAILED",
        "readyz semantic payload failed",
    )
    services = {unit: probes.service_active(unit) for unit in _REQUIRED_SERVICES}
    _require(all(services.values()), "HEALTH_FAILED", "required PAPER service inactive")
    return {
        "status": "PASS",
        "healthz": health,
        "readyz": ready,
        "services": services,
        **dict(safety),
    }


def collect_paper_technical_evidence(
    root: Path, *, probes: QualificationProbes = DEFAULT_PROBES
) -> dict[str, Any]:
    """Collect and validate every fact without writing qualification state."""

    root = root.resolve(strict=True)
    release, manifest = _active_release(root)
    database = _paper_database(root, release)
    try:
        migration, safety = _database_evidence(database, int(manifest["schema_version_required"]))
    except QualificationFailed:
        raise
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise QualificationFailed(
            "DB_INTEGRITY_FAILED", "PAPER database evidence unavailable"
        ) from error
    full_tests, rollback = _fixed_test_evidence(release, probes)
    runtime = _runtime_evidence(probes, safety)
    backups = sorted(
        (root / "backups").glob("*.tar.gz.enc"),
        key=lambda path: path.stat().st_mtime,
    )
    _require(bool(backups), "BACKUP_VERIFICATION_FAILED", "no encrypted backup")
    try:
        backup_result = dict(probes.verify_backup(backups[-1], release))
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        raise QualificationFailed("BACKUP_VERIFICATION_FAILED", "backup verifier failed") from error
    _require(
        backup_result.get("integrity") == "ok"
        and backup_result.get("restart_safe") is True
        and backup_result.get("schema_version") == SCHEMA_VERSION,
        "BACKUP_VERIFICATION_FAILED",
        "latest encrypted backup is not restart-safe and schema-compatible",
    )
    return {
        "kind": "PAPER_TECHNICAL_QUALIFICATION_EVIDENCE",
        "producer_version": 1,
        "observed_at": datetime.now(UTC).isoformat(),
        "release_sha": manifest["git_sha"],
        "release_tree": manifest["git_tree"],
        "schema_version": manifest["schema_version_required"],
        "full_test_results": full_tests,
        "staging_run": {
            "status": "PASS",
            "active_release": str(release),
            "manifest_valid": True,
            "manifest_hashes_verified": True,
        },
        "migration": migration,
        "rollback_rehearsal": rollback,
        "production_health": runtime,
        "backup_verification": {
            "status": "PASS",
            "archive_name": backups[-1].name,
            "schema_version": backup_result.get("schema_version"),
            "integrity": "ok",
            "restart_safe": True,
        },
        "status": "PASS",
    }


def record_paper_technical_evidence(root: Path, evidence: Mapping[str, Any]) -> int:
    """Persist a freshly collected PASS bundle as the only final mutation."""

    if evidence.get("status") != "PASS":
        raise QualificationFailed("EVIDENCE_INCOMPLETE", "final evidence gate did not pass")
    root = root.resolve(strict=True)
    release, manifest = _active_release(root)
    _require(
        evidence.get("release_sha") == manifest["git_sha"]
        and evidence.get("release_tree") == manifest["git_tree"],
        "RELEASE_MANIFEST_MISMATCH",
        "active release changed after evidence collection",
    )
    database = _paper_database(root, release)
    store = StateStore(database, initialize=False)
    return store.record_paper_technical_qualification(
        release_sha=str(evidence["release_sha"]),
        release_tree=str(evidence["release_tree"]),
        schema_version=int(evidence["schema_version"]),
        full_test_results=evidence["full_test_results"],
        staging_run=evidence["staging_run"],
        migration=evidence["migration"],
        rollback_rehearsal=evidence["rollback_rehearsal"],
        production_health=evidence["production_health"],
        backup_verification=evidence["backup_verification"],
        qualified_at=str(evidence["observed_at"]),
    )
