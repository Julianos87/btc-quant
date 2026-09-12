from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from btcquant.deployment import (
    REQUIRED_VALIDATION_CHECKS,
    build_release_manifest,
    sha256_file,
    write_release_manifest,
)
from btcquant.execution.paper_technical_qualification import (
    QualificationFailed,
    QualificationProbes,
    collect_paper_technical_evidence,
    _verify_backup,
    record_paper_technical_evidence,
)
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore
from btcquant.execution.testnet_preflight import _technical_qualification

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
TREE = "b" * 40


def _write_validation_attestation(release: Path) -> None:
    payload = {
        "format_version": 1,
        "validation_protocol_version": 1,
        "status": "PASS",
        "git_sha": SHA,
        "git_tree": TREE,
        "schema_version_required": SCHEMA_VERSION,
        "created_at": "2026-09-12T00:00:00+00:00",
        "validation_environment": {
            "kind": "ephemeral_dev_validation_venv",
            "python_version": "3.12.0",
            "runtime_dev_tools_included": False,
            "live_state_visible": False,
            "runtime_symlinks_created_after_validation": True,
        },
        "checks": {name: {"status": "PASS"} for name in REQUIRED_VALIDATION_CHECKS},
    }
    (release / "release-validation.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _root(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    release = runtime / "releases" / SHA
    (release / "environments/paper").mkdir(parents=True)
    (runtime / "state").mkdir()
    (runtime / "backups").mkdir()
    for name in ("uv.lock", "pyproject.toml"):
        shutil.copy2(ROOT / name, release / name)
    shutil.copy2(
        ROOT / "environments/paper/config.yaml",
        release / "environments/paper/config.yaml",
    )
    _write_validation_attestation(release)
    write_release_manifest(
        release,
        build_release_manifest(
            release,
            git_sha=SHA,
            git_tree=TREE,
            origin="github.com/Julianos87/btc-quant.git",
            python_version="3.12",
            uv_version="0.11",
            require_validation_attestation=True,
        ),
    )
    (runtime / "current").symlink_to(release)
    StateStore(runtime / "state/btcquant.db")
    (runtime / "backups/state.tar.gz.enc").write_bytes(b"isolated fixture")
    return runtime


def _probes(*, failed_command: str | None = None) -> QualificationProbes:
    def run(command, cwd):
        del cwd
        joined = " ".join(command)
        return {
            "status": "FAIL" if failed_command and failed_command in joined else "PASS",
            "returncode": 1 if failed_command and failed_command in joined else 0,
            "command": list(command),
        }

    def get_json(url):
        if url.endswith("/healthz"):
            return {"kind": "PROCESS_LIVENESS", "status": "ok"}
        return {"kind": "SERVICE_READINESS", "ready": True, "status": "ready"}

    return QualificationProbes(
        run=run,
        get_json=get_json,
        service_active=lambda unit: bool(unit),
        verify_backup=lambda archive, release: {
            "archive": str(archive),
            "integrity": "ok",
            "restart_safe": True,
            "schema_version": SCHEMA_VERSION,
        },
    )


def _qualification_count(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT payload FROM readiness_reports").fetchall()
    return sum(json.loads(row[0]).get("kind") == "PAPER_TECHNICAL_QUALIFICATION" for row in rows)


def _domain_counts(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "orders",
                "external_fills",
                "financial_fill_applications",
                "engine_state",
                "incidents",
                "events",
            )
        }


def _encrypted_backup_fixture(tmp_path: Path, password: str) -> tuple[Path, Path]:
    payload = tmp_path / "backup-payload" / "state"
    payload.mkdir(parents=True)
    StateStore(payload / "btcquant.db")

    plaintext = tmp_path / "state.tar.gz"
    with tarfile.open(plaintext, "w:gz") as bundle:
        bundle.add(payload, arcname="state")

    archive = tmp_path / "state.tar.gz.enc"
    environment = os.environ.copy()
    environment["BACKUP_ENCRYPTION_KEY"] = password
    encrypted = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-salt",
            "-in",
            str(plaintext),
            "-out",
            str(archive),
            "-pass",
            "env:BACKUP_ENCRYPTION_KEY",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert encrypted.returncode == 0, encrypted.stderr

    release = tmp_path / "release"
    (release / "venv/bin").mkdir(parents=True)
    (release / "scripts").mkdir()
    (release / "venv/bin/python").symlink_to(sys.executable)
    shutil.copy2(ROOT / "scripts/verify_backup.py", release / "scripts/verify_backup.py")
    return archive, release


def test_backup_verifier_fails_closed_without_credential(tmp_path, monkeypatch):
    archive, release = _encrypted_backup_fixture(tmp_path, "dummy-correct-key")
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)

    def unexpected_subprocess(*args, **kwargs):
        pytest.fail(f"verifier must not run without a credential: {args}, {kwargs}")

    monkeypatch.setattr(
        "btcquant.execution.paper_technical_qualification.subprocess.run",
        unexpected_subprocess,
    )
    result = _verify_backup(archive, release)
    assert result == {"status": "FAIL", "reason": "credential_unavailable"}


@pytest.mark.parametrize("value", ["", "   "])
def test_backup_verifier_rejects_empty_credential(tmp_path, monkeypatch, value):
    archive, release = _encrypted_backup_fixture(tmp_path, "dummy-correct-key")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", value)
    result = _verify_backup(archive, release)
    assert result == {"status": "FAIL", "reason": "credential_unavailable"}


def test_backup_verifier_receives_only_minimal_environment(tmp_path, monkeypatch):
    archive, release = _encrypted_backup_fixture(tmp_path, "dummy-correct-key")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "dummy-correct-key")
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "unrelated-secret")
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, '{"integrity":"ok"}', "")

    monkeypatch.setattr("btcquant.execution.paper_technical_qualification.subprocess.run", fake_run)
    result = _verify_backup(archive, release)
    assert result == {"integrity": "ok"}
    assert observed["env"] == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "BACKUP_ENCRYPTION_KEY": "dummy-correct-key",
    }
    assert "unrelated-secret" not in str(observed["command"])
    assert "dummy-correct-key" not in str(observed["command"])


def test_backup_verifier_rejects_secret_in_verifier_output(tmp_path, monkeypatch):
    archive, release = _encrypted_backup_fixture(tmp_path, "dummy-correct-key")
    key = "dummy-correct-key"
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", key)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, f'{{"key":"{key}"}}', "")

    monkeypatch.setattr("btcquant.execution.paper_technical_qualification.subprocess.run", fake_run)
    result = _verify_backup(archive, release)

    assert result == {"status": "FAIL", "reason": "credential_in_verifier_output"}
    assert key not in json.dumps(result)


def test_backup_verifier_decrypts_valid_encrypted_backup_with_dummy_key(tmp_path, monkeypatch):
    archive, release = _encrypted_backup_fixture(tmp_path, "dummy-correct-key")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "dummy-correct-key")
    result = _verify_backup(archive, release)
    assert result["integrity"] == "ok"
    assert result["restart_safe"] is True
    assert result["schema_version"] == SCHEMA_VERSION


def test_backup_verifier_rejects_wrong_key_without_secret_leak(tmp_path, monkeypatch):
    archive, release = _encrypted_backup_fixture(tmp_path, "dummy-correct-key")
    wrong_key = "dummy-wrong-key"
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", wrong_key)
    result = _verify_backup(archive, release)
    assert result["status"] == "FAIL"
    assert wrong_key not in json.dumps(result)


def test_happy_path_collects_derived_evidence_and_records_only_qualification(tmp_path):
    runtime = _root(tmp_path)
    database = runtime / "state/btcquant.db"
    before = _domain_counts(database)

    evidence = collect_paper_technical_evidence(runtime, probes=_probes())
    row_id = record_paper_technical_evidence(runtime, evidence)

    assert row_id > 0
    assert evidence["release_sha"] == SHA
    assert evidence["release_tree"] == TREE
    assert evidence["schema_version"] == SCHEMA_VERSION
    assert evidence["status"] == "PASS"
    assert _domain_counts(database) == before
    assert _qualification_count(database) == 1


def test_inspection_never_writes(tmp_path):
    runtime = _root(tmp_path)
    database = runtime / "state/btcquant.db"
    collect_paper_technical_evidence(runtime, probes=_probes())
    assert _qualification_count(database) == 0


def test_valid_attestation_does_not_run_runtime_dev_tools(tmp_path):
    runtime = _root(tmp_path)
    probes = replace(
        _probes(),
        run=lambda command, cwd: pytest.fail(
            f"runtime validation tool unexpectedly executed: {command} in {cwd}"
        ),
    )
    evidence = collect_paper_technical_evidence(runtime, probes=probes)
    assert evidence["full_test_results"]["source"] == "release-validation.json"


def test_cli_contract_has_no_manual_pass_or_release_identity_flags() -> None:
    source = (ROOT / "src/btcquant/entrypoints/paper_qualification.py").read_text(encoding="utf-8")
    for forbidden in (
        "--sha",
        "--tree",
        "--tests-passed",
        "--health-ok",
        "--schema",
        "--force",
        "--skip-checks",
    ):
        assert forbidden not in source


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing_manifest", "RELEASE_MANIFEST_MISMATCH"),
        ("wrong_tree", "RELEASE_MANIFEST_MISMATCH"),
        ("wrong_environment", "ENVIRONMENT_NOT_PAPER"),
        ("missing_attestation", "RELEASE_MANIFEST_MISMATCH"),
        ("health_failure", "HEALTH_FAILED"),
        ("readiness_failure", "READINESS_FAILED"),
        ("service_failure", "HEALTH_FAILED"),
        ("backup_failure", "BACKUP_VERIFICATION_FAILED"),
    ],
)
def test_failed_evidence_never_writes_qualification(tmp_path, mutation, reason):
    runtime = _root(tmp_path)
    release = (runtime / "current").resolve()
    probes = _probes()
    if mutation == "missing_manifest":
        (release / "release-manifest.json").unlink()
    elif mutation == "wrong_tree":
        manifest = json.loads((release / "release-manifest.json").read_text())
        manifest["git_tree"] = "invalid"
        (release / "release-manifest.json").write_text(json.dumps(manifest))
    elif mutation == "wrong_environment":
        config = release / "environments/paper/config.yaml"
        config.write_text(config.read_text().replace("environment: paper", "environment: testnet"))
        manifest_path = release / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["config_file_sha256"] = sha256_file(config)
        manifest_path.write_text(json.dumps(manifest))
    elif mutation == "missing_attestation":
        (release / "release-validation.json").unlink()
    elif mutation == "health_failure":
        probes = replace(
            probes,
            get_json=lambda url: (
                {"status": "bad", "kind": "PROCESS_LIVENESS"}
                if url.endswith("/healthz")
                else {"status": "ready", "ready": True}
            ),
        )
    elif mutation == "readiness_failure":
        probes = replace(
            probes,
            get_json=lambda url: (
                {"status": "ok", "kind": "PROCESS_LIVENESS"}
                if url.endswith("/healthz")
                else {"status": "not_ready", "ready": False}
            ),
        )
    elif mutation == "service_failure":
        probes = replace(probes, service_active=lambda unit: False)
    elif mutation == "backup_failure":
        probes = replace(probes, verify_backup=lambda archive, release: {"integrity": "bad"})

    with pytest.raises(QualificationFailed, match=reason):
        collect_paper_technical_evidence(runtime, probes=probes)
    assert _qualification_count(runtime / "state/btcquant.db") == 0


@pytest.mark.parametrize(
    ("unsafe_state", "reason"),
    [
        (
            "incident",
            "OPEN_CRITICAL_INCIDENT",
        ),
        (
            "reconciliation",
            "PENDING_RECONCILIATION",
        ),
    ],
)
def test_unsafe_paper_state_never_writes(tmp_path, unsafe_state, reason):
    runtime = _root(tmp_path)
    database = runtime / "state/btcquant.db"
    with sqlite3.connect(database) as connection:
        if unsafe_state == "incident":
            connection.execute(
                "INSERT INTO incidents("
                "fingerprint,engine,severity,kind,message,context,status,"
                "occurrences,first_seen,last_seen"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "x",
                    "trend",
                    "CRITICAL",
                    "test",
                    "x",
                    "{}",
                    "OPEN",
                    1,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
        else:
            connection.execute(
                "INSERT INTO engine_state(engine,payload,updated_at) VALUES(?,?,?)",
                (
                    "trend",
                    json.dumps({"reconciliation_required": True}),
                    "2026-01-01T00:00:00Z",
                ),
            )
    with pytest.raises(QualificationFailed, match=reason):
        collect_paper_technical_evidence(runtime, probes=_probes())
    assert _qualification_count(database) == 0


def test_schema_mismatch_and_testnet_identity_fail_before_write(tmp_path):
    runtime = _root(tmp_path)
    database = runtime / "state/btcquant.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
    with pytest.raises(QualificationFailed, match="DB_SCHEMA_MISMATCH"):
        collect_paper_technical_evidence(runtime, probes=_probes())
    assert _qualification_count(database) == 0

    runtime = _root(tmp_path / "second")
    testnet = runtime / "state/btcquant-testnet.db"
    (runtime / "state/btcquant.db").replace(testnet)
    (runtime / "state/btcquant.db").hardlink_to(testnet)
    with pytest.raises(QualificationFailed, match="ENVIRONMENT_NOT_PAPER"):
        collect_paper_technical_evidence(runtime, probes=_probes())


def test_unresolved_order_prevents_qualification(tmp_path):
    runtime = _root(tmp_path)
    database = runtime / "state/btcquant.db"
    StateStore(database).begin_order(
        "trend",
        "slot-a",
        "intent-a",
        "MARKET",
        "BUY",
        0.01,
        "fixture",
    )

    with pytest.raises(QualificationFailed, match="UNRESOLVED_ORDERS"):
        collect_paper_technical_evidence(runtime, probes=_probes())
    assert _qualification_count(database) == 0


def test_record_write_failure_cannot_leave_a_qualification(tmp_path, monkeypatch):
    runtime = _root(tmp_path)
    database = runtime / "state/btcquant.db"
    evidence = collect_paper_technical_evidence(runtime, probes=_probes())

    def fail_record(*args, **kwargs):
        del args, kwargs
        raise sqlite3.OperationalError("injected write failure")

    monkeypatch.setattr(StateStore, "record_paper_technical_qualification", fail_record)
    with pytest.raises(sqlite3.OperationalError, match="injected write failure"):
        record_paper_technical_evidence(runtime, evidence)
    assert _qualification_count(database) == 0


def test_same_sha_rerun_is_unambiguous_and_new_sha_does_not_match(tmp_path):
    runtime = _root(tmp_path)
    database = runtime / "state/btcquant.db"
    first = collect_paper_technical_evidence(runtime, probes=_probes())
    record_paper_technical_evidence(runtime, first)
    second = collect_paper_technical_evidence(runtime, probes=_probes())
    record_paper_technical_evidence(runtime, second)

    assert _qualification_count(database) == 2
    assert _technical_qualification(database, expected_git_sha=SHA, expected_tree=TREE) == (
        True,
        "PAPER_TECHNICAL_QUALIFIED",
        None,
    )
    passed, _, reason = _technical_qualification(
        database, expected_git_sha="c" * 40, expected_tree=TREE
    )
    assert passed is False
    assert reason == "PAPER_TECHNICAL_RELEASE_MISMATCH"


def test_active_release_change_after_collection_prevents_record(tmp_path):
    runtime = _root(tmp_path)
    evidence = collect_paper_technical_evidence(runtime, probes=_probes())
    manifest_path = runtime / "current/release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["git_tree"] = "c" * 40
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(QualificationFailed, match="RELEASE_MANIFEST_MISMATCH"):
        record_paper_technical_evidence(runtime, evidence)
    assert _qualification_count(runtime / "state/btcquant.db") == 0
