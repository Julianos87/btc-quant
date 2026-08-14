"""Adversarial Lot 7.3 tests; all state is temporary and local."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import btcquant.backup as backup_module
from btcquant.backup import (
    BackupSource,
    ExportRefused,
    BackupLock,
    ManifestInvalid,
    HashMismatch,
    PathUnsafe,
    RecoveryRequired,
    ResearchRecoveryRequired,
    _write_manifest,
    advance_recovery_marker,
    assert_research_recovery_clear,
    assert_writer_recovery_clear,
    create_backup_set,
    export_verified_backup_set,
    restore_to_staging,
    sha256_file,
    verify_backup_set,
    write_recovery_marker,
)
from btcquant.execution.state_store import StateStore
from btcquant.research.governance import GovernanceIncomplete
from btcquant.research.governance_store import GovernanceStore
from btcquant.research.quant_policy import proposed_policy_v1

BASE_SHA = "9924bd9182375f9d247dfcd6cb7931b5adc10e17"
SOURCE_IDENTITY = "github.com/example/btcquant.git"


def _make_db(path: Path, marker_value: str = "6") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata VALUES ('schema_version', ?)", (marker_value,))
        connection.commit()


def _backup(tmp_path: Path) -> Path:
    source = tmp_path / "source.db"
    _make_db(source)
    return create_backup_set(
        [BackupSource("trading_state", source, "AUTHORITATIVE_TRADING_STATE")],
        tmp_path / "backups",
        app_git_sha="a" * 40,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )


def test_payload_tamper_after_verify_is_not_exportable(tmp_path: Path) -> None:
    backup = _backup(tmp_path)
    assert verify_backup_set(backup).trusted is True
    payload = backup / "trading_state.sqlite3"
    payload.write_bytes(b"different bytes under the same filename")
    with pytest.raises(HashMismatch):
        verify_backup_set(backup)
    with pytest.raises((ExportRefused, HashMismatch)):
        export_verified_backup_set(
            backup,
            tmp_path / "offhost.enc",
            exporter=lambda *_args: pytest.fail("tampered set exported"),
        )


def test_manifest_metadata_tamper_is_not_trusted_even_with_rehashed_manifest(
    tmp_path: Path,
) -> None:
    backup = _backup(tmp_path)
    manifest = json.loads((backup / "backup-manifest.json").read_text(encoding="utf-8"))
    manifest["total_bytes"] = int(manifest["total_bytes"]) + 1
    _write_manifest(backup, manifest)
    with pytest.raises(Exception, match="MANIFEST_INVALID|mismatch"):
        verify_backup_set(backup)


def test_optional_absence_and_source_symlink_are_explicit(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    optional_missing = tmp_path / "optional.db"
    backup = create_backup_set(
        [
            BackupSource("trading_state", source, "AUTHORITATIVE_TRADING_STATE"),
            BackupSource(
                "shadow_state",
                optional_missing,
                "AUTHORITATIVE_SHADOW_STATE",
                optional=True,
            ),
        ],
        tmp_path / "backups",
        app_git_sha="b" * 40,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    entries = verify_backup_set(backup).manifest["entries"]
    absent = next(item for item in entries if item["logical_name"] == "shadow_state")
    assert absent["source_status"] == "OPTIONAL_NOT_PRESENT"
    assert absent["sha256"] is None

    link = tmp_path / "source-link.db"
    link.symlink_to(source)
    with pytest.raises(PathUnsafe):
        create_backup_set(
            [BackupSource("trading_state", link, "AUTHORITATIVE_TRADING_STATE")],
            tmp_path / "symlink-backups",
            app_git_sha="c" * 40,
            app_schema_version=6,
            source_identity=SOURCE_IDENTITY,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"marker_schema_version": 1, "status": "UNKNOWN"},
    ],
)
def test_malformed_or_unknown_recovery_marker_blocks_writer(tmp_path: Path, payload: dict) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "recovery-state.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RecoveryRequired):
        assert_writer_recovery_clear(state)


def test_future_recovery_marker_and_marker_symlink_fail_closed(tmp_path: Path) -> None:
    future_root = tmp_path / "future"
    write_recovery_marker(
        future_root,
        backup_id="future",
        restored_app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
        restored_at=__import__("datetime")
        .datetime.now(__import__("datetime").UTC)
        .replace(year=2099),
    )
    with pytest.raises(RecoveryRequired):
        assert_writer_recovery_clear(future_root)

    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    (symlink_root / "recovery-state.json").symlink_to(target)
    with pytest.raises(PathUnsafe):
        assert_writer_recovery_clear(symlink_root)


def test_state_store_writer_gate_is_central_and_read_only_reader_remains_available(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    db = state / "btcquant.db"
    _make_db(db)
    write_recovery_marker(
        state,
        backup_id="trading",
        restored_app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    with pytest.raises(RecoveryRequired):
        StateStore(db, initialize=False)
    assert StateStore(db, initialize=False, read_only=True).path == db


def test_restore_writes_recovery_marker_before_atomic_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = _backup(tmp_path)
    destination = tmp_path / "restored"
    runtime = tmp_path / "runtime"
    observed: list[bool] = []
    original_replace = backup_module.os.replace

    def checked_replace(
        source: str | bytes | os.PathLike[str], target: str | bytes | os.PathLike[str]
    ) -> None:
        if Path(source).name.endswith(".staging") and Path(target) == destination:
            observed.append((runtime / "recovery-state.json").is_file())
        original_replace(source, target)

    monkeypatch.setattr(backup_module.os, "replace", checked_replace)
    before = {path.name: sha256_file(path) for path in backup.iterdir() if path.is_file()}
    result = restore_to_staging(
        backup,
        destination,
        runtime_root=runtime,
        expected_app_schema_version=6,
    )
    after = {path.name: sha256_file(path) for path in backup.iterdir() if path.is_file()}
    assert observed == [True]
    assert result.staging_path == destination
    assert before == after
    assert (runtime / "recovery-state.json").is_file()


def test_research_recovery_marker_blocks_real_search_even_with_frozen_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "governance.sqlite3"
    recovery_root = tmp_path / "research-runtime"
    monkeypatch.setenv("BTCQUANT_GOVERNANCE_DB", str(db_path))
    monkeypatch.setenv("BTCQUANT_RECOVERY_ROOT", str(recovery_root))
    policy = proposed_policy_v1()
    with GovernanceStore(db_path) as store:
        approved = policy.approve(store, base_git_sha=BASE_SHA)
        frozen = approved.freeze(store, base_git_sha=BASE_SHA)
        write_recovery_marker(
            recovery_root,
            backup_id="governance",
            restored_app_schema_version=1,
            source_identity=SOURCE_IDENTITY,
            research=True,
        )
        with pytest.raises(GovernanceIncomplete, match="recovery"):
            frozen.validate_for_real_search(store, expected_base_git_sha=BASE_SHA)


def test_research_marker_never_allows_direct_clear(tmp_path: Path) -> None:
    write_recovery_marker(
        tmp_path,
        backup_id="governance",
        restored_app_schema_version=1,
        source_identity=SOURCE_IDENTITY,
        research=True,
    )
    with pytest.raises(ResearchRecoveryRequired):
        assert_research_recovery_clear(tmp_path)
    with pytest.raises(RecoveryRequired):
        advance_recovery_marker(
            tmp_path,
            "RECOVERY_CLEARED",
            evidence={"reconciliation_marker_verified": True},
            research=True,
        )


def test_lock_manifest_and_transition_symlink_tampering_fail_closed(tmp_path: Path) -> None:
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir()
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("", encoding="utf-8")
    (lock_root / ".backup.lock").symlink_to(lock_target)
    with pytest.raises(PathUnsafe):
        with BackupLock(lock_root):
            pass

    backup = _backup(tmp_path / "manifest")
    manifest_path = backup / "backup-manifest.json"
    manifest_copy = tmp_path / "manifest-copy.json"
    manifest_copy.write_bytes(manifest_path.read_bytes())
    manifest_path.unlink()
    manifest_path.symlink_to(manifest_copy)
    with pytest.raises(ManifestInvalid):
        verify_backup_set(backup)

    marker_root = tmp_path / "marker"
    write_recovery_marker(
        marker_root,
        backup_id="marker",
        restored_app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    marker_path = marker_root / "recovery-state.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["transitions"].append("malformed")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(RecoveryRequired):
        assert_writer_recovery_clear(marker_root)
