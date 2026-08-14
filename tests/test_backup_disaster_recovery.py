"""Temporary-only Lot 7 backup, restore and recovery-drill tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from btcquant.backup import (
    DB_WRITER_SERVICES,
    DB_WRITER_TIMERS,
    BackupBusy,
    BackupError,
    BackupSource,
    BackupVerification,
    ClockSkew,
    ExportRefused,
    InsufficientDisk,
    ManifestInvalid,
    PathUnsafe,
    RecoveryRequired,
    ResearchRecoveryRequired,
    RetentionPolicy,
    SchemaIncompatible,
    SourceUnavailable,
    WritersNotQuiesced,
    advance_recovery_marker,
    assert_research_recovery_clear,
    assert_shadow_recovery_clear,
    assert_writer_recovery_clear,
    backup_age_metrics,
    backup_freshness,
    create_backup_set,
    export_verified_backup_set,
    latest_valid_backup,
    prune_backup_sets,
    record_restore_drill,
    require_writer_quiescence,
    restore_to_staging,
    sha256_file,
    verify_backup_set,
    write_recovery_marker,
)
from btcquant.execution.state_store import StateStore


APP_SHA = "a" * 40
SOURCE_IDENTITY = "github.com/example/btcquant.git"


def _make_db(path: Path, *, records: int = 2, schema: int = 6) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.executescript(
            f"""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE parent(id INTEGER PRIMARY KEY);
            CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));
            INSERT INTO metadata VALUES ('schema_version', '{schema}');
            INSERT INTO parent VALUES (1);
            """
        )
        connection.executemany(
            "INSERT INTO child VALUES (?, 1)", [(index,) for index in range(records)]
        )
        connection.commit()


def _source(path: Path, logical: str = "trading_state") -> BackupSource:
    return BackupSource(logical, path, "AUTHORITATIVE_TRADING_STATE")


def _create(root: Path, source: Path, *, when: datetime | None = None) -> Path:
    return create_backup_set(
        [_source(source)],
        root,
        app_git_sha=APP_SHA,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
        now=when,
    )


def _rewrite_manifest(directory: Path, mutate) -> None:
    path = directory / "backup-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    core = dict(manifest)
    core.pop("manifest_sha256", None)
    payload = (json.dumps(core, sort_keys=True, separators=(",", ":")) + "\n").encode()
    manifest["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (directory / "backup-manifest.sha256").write_text(
        f"{sha256_file(path)}  backup-manifest.json\n", encoding="ascii"
    )


def test_backup_uses_online_api_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    before_hash = sha256_file(source)
    backup = _create(tmp_path / "backups", source)

    assert sha256_file(source) == before_hash
    with sqlite3.connect(source) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    result = verify_backup_set(backup, expected_app_schema_version=6)
    assert result.trusted is True
    assert result.manifest["external_exchange_state_included"] is False
    assert result.manifest["entries"][0]["backup_method"] == "SQLITE_ONLINE_BACKUP_API"
    assert result.manifest["entries"][0]["sqlite_integrity_check"] == "ok"


def test_backup_captures_wal_commits_without_copying_wal(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source, records=1)
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO child VALUES (99, 1)")
    writer.commit()
    backup = _create(tmp_path / "backups", source)
    writer.close()

    copied = backup / "trading_state.sqlite3"
    with sqlite3.connect(copied) as connection:
        assert connection.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 2
    assert not any(path.name.endswith(("-wal", "-shm")) for path in backup.iterdir())


def test_failed_before_publish_leaves_no_published_set(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)

    def fail(phase: str) -> None:
        if phase == "after-manifest":
            raise RuntimeError("simulated power loss")

    with pytest.raises(RuntimeError, match="power loss"):
        create_backup_set(
            [_source(source)],
            tmp_path / "backups",
            app_git_sha=APP_SHA,
            app_schema_version=6,
            source_identity=SOURCE_IDENTITY,
            failure_hook=fail,
        )
    assert list((tmp_path / "backups").iterdir()) == [tmp_path / "backups" / ".backup.lock"]


def test_manifest_hash_tamper_and_unknown_version_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    backup = _create(tmp_path / "backups", source)
    (backup / "trading_state.sqlite3").write_bytes(b"tampered")
    with pytest.raises(Exception, match="HASH_MISMATCH|integrity"):
        verify_backup_set(backup)

    backup = _create(tmp_path / "backups-2", source)
    _rewrite_manifest(backup, lambda manifest: manifest.update(backup_schema_version=999))
    with pytest.raises(ManifestInvalid, match="unknown backup schema"):
        verify_backup_set(backup)


def test_manifest_path_traversal_and_symlink_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    backup = _create(tmp_path / "backups", source)
    _rewrite_manifest(
        backup, lambda manifest: manifest["entries"][0].update(backup_filename="../escape")
    )
    with pytest.raises(PathUnsafe):
        verify_backup_set(backup)

    backup = _create(tmp_path / "backups-2", source)
    entry = backup / "trading_state.sqlite3"
    entry.unlink()
    entry.symlink_to(source)
    with pytest.raises(Exception, match="HASH_MISMATCH|symlink"):
        verify_backup_set(backup)


def test_latest_valid_skips_corrupt_newer_set_and_retention_keeps_newest(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    root = tmp_path / "backups"
    first = _create(root, source, when=datetime(2026, 1, 1, tzinfo=UTC))
    second = _create(root, source, when=datetime(2026, 1, 2, tzinfo=UTC))
    (second / "trading_state.sqlite3").write_bytes(b"corrupt")
    assert latest_valid_backup(root).backup_id == first.name

    valid = _create(root, source, when=datetime(2026, 1, 3, tzinfo=UTC))
    removed = prune_backup_sets(
        root,
        RetentionPolicy(recent_count=1, daily_count=0, weekly_count=0, monthly_count=0),
    )
    assert valid.name not in removed
    assert valid.exists()
    # Corrupt/unknown sets are quarantined by omission, never deleted by
    # retention. This preserves forensic evidence and avoids destructive
    # cleanup of a set whose state is not trusted.
    assert second.exists()
    assert first.name in removed


def test_retention_does_not_delete_only_verified_backup(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    root = tmp_path / "backups"
    backup = _create(root, source)
    assert prune_backup_sets(root, RetentionPolicy(0, 0, 0, 0)) == []
    assert backup.exists()


def test_free_space_gate_and_source_unavailable_are_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    with pytest.raises(InsufficientDisk):
        create_backup_set(
            [_source(source)],
            tmp_path / "backups",
            app_git_sha=APP_SHA,
            app_schema_version=6,
            source_identity=SOURCE_IDENTITY,
            disk_usage_fn=lambda _: type("Usage", (), {"free": 0})(),
        )
    with pytest.raises(SourceUnavailable):
        _create(tmp_path / "backups-2", tmp_path / "missing.db")


def test_nonblocking_single_flight_lock(tmp_path: Path) -> None:
    from btcquant.backup import BackupLock

    root = tmp_path / "backups"
    with BackupLock(root):
        with pytest.raises(BackupBusy):
            _create(root, tmp_path / "missing.db")


def test_restore_is_staging_only_and_creates_trading_recovery_gate(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    store = StateStore(source)
    store.save_engine_state(
        "trend", {"equity": 1000.0, "cash": 1000.0, "slots": {}, "halted": False}
    )
    backup = _create(tmp_path / "backups", source)
    runtime = tmp_path / "runtime"
    restored = restore_to_staging(
        backup,
        tmp_path / "restore-staging",
        runtime_root=tmp_path / "restore-staging",
        expected_app_schema_version=6,
    )
    runtime = tmp_path / "restore-staging"
    assert restored.staging_path.is_dir()
    restored_db = restored.staging_path / "btcquant.db"
    assert StateStore(restored_db, initialize=False, read_only=True).path == restored_db
    assert restored.recovery_marker == runtime / "recovery-state.json"
    with pytest.raises(RecoveryRequired):
        assert_writer_recovery_clear(runtime)
    assert (
        json.loads(restored.recovery_marker.read_text())["external_exchange_state_included"]
        is False
    )


def test_restore_refuses_newer_schema_and_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    backup = _create(tmp_path / "backups", source)
    with pytest.raises(SchemaIncompatible):
        restore_to_staging(
            backup,
            tmp_path / "restore-v5",
            runtime_root=tmp_path / "runtime",
            expected_app_schema_version=5,
        )
    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(Exception, match="destination"):
        restore_to_staging(
            backup,
            destination,
            runtime_root=tmp_path / "runtime-2",
            expected_app_schema_version=6,
        )


def test_recovery_marker_requires_positive_reconciliation_and_exact_transitions(
    tmp_path: Path,
) -> None:
    write_recovery_marker(
        tmp_path,
        backup_id="backup-1",
        restored_app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    with pytest.raises(RecoveryRequired, match="invalid recovery transition"):
        advance_recovery_marker(
            tmp_path, "RECOVERY_CLEARED", evidence={"reconciliation_marker_verified": True}
        )
    evidence = {
        "exchange_reachable": True,
        "local_orders_reconciled": True,
        "external_orders_reconciled": True,
        "positions_reconciled": True,
        "stops_reconciled": True,
        "no_unbalanced_state": True,
        "no_ambiguity": True,
        "accounting_checkpoint_compatible": True,
    }
    advance_recovery_marker(tmp_path, "RECONCILIATION_VERIFIED", evidence=evidence)
    advance_recovery_marker(
        tmp_path, "RECOVERY_CLEARED", evidence={"reconciliation_marker_verified": True}
    )
    assert_writer_recovery_clear(tmp_path)
    with pytest.raises(RecoveryRequired):
        advance_recovery_marker(tmp_path, "RECOVERY_REQUIRED", evidence={})


def test_research_restore_gate_is_separate_and_fail_closed(tmp_path: Path) -> None:
    write_recovery_marker(
        tmp_path,
        backup_id="governance-1",
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


def test_clock_skew_and_backup_freshness_are_explicit(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    future = datetime.now(UTC) + timedelta(hours=1)
    backup = _create(tmp_path / "backups", source, when=future)
    with pytest.raises(ClockSkew):
        verify_backup_set(backup, now=datetime.now(UTC))
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert (
        backup_freshness(
            now - timedelta(seconds=10),
            last_verified_at=None,
            now=now,
            fresh_after_seconds=30,
            stale_after_seconds=60,
        )
        == "FRESH_BACKUP"
    )
    assert (
        backup_freshness(
            now - timedelta(seconds=50),
            last_verified_at=None,
            now=now,
            fresh_after_seconds=30,
            stale_after_seconds=60,
        )
        == "STALE_BACKUP"
    )
    assert (
        backup_freshness(
            now + timedelta(seconds=1),
            last_verified_at=None,
            now=now,
            fresh_after_seconds=30,
            stale_after_seconds=60,
        )
        == "UNKNOWN"
    )
    assert (
        backup_freshness(
            now - timedelta(seconds=100),
            last_verified_at=now - timedelta(seconds=1),
            now=now,
            fresh_after_seconds=30,
            stale_after_seconds=60,
        )
        == "UNKNOWN"
    )
    ages = backup_age_metrics(
        now - timedelta(days=30),
        last_verified_at=now - timedelta(seconds=5),
        restore_drill_at=None,
        now=now,
    )
    assert ages["backup_age_seconds"] == 30 * 24 * 60 * 60
    assert ages["verification_age_seconds"] == 5
    assert ages["restore_drill_age_seconds"] is None


def test_manifest_application_schema_is_not_entry_schema_max(tmp_path: Path) -> None:
    trading = tmp_path / "trading.db"
    shadow = tmp_path / "shadow.db"
    _make_db(trading, schema=6)
    _make_db(shadow, schema=1)
    backup = create_backup_set(
        [
            BackupSource("trading_state", trading, "AUTHORITATIVE_TRADING_STATE"),
            BackupSource("shadow_state", shadow, "AUTHORITATIVE_SHADOW_STATE"),
        ],
        tmp_path / "backups",
        app_git_sha=APP_SHA,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    verification = verify_backup_set(backup, expected_app_schema_version=6)
    assert verification.app_schema_version == 6
    assert {entry["sqlite_schema_version"] for entry in verification.manifest["entries"]} == {1, 6}


def test_shadow_only_manifest_keeps_application_schema_independent(tmp_path: Path) -> None:
    shadow = tmp_path / "shadow.db"
    _make_db(shadow, schema=1)
    backup = create_backup_set(
        [BackupSource("shadow_state", shadow, "AUTHORITATIVE_SHADOW_STATE")],
        tmp_path / "backups",
        app_git_sha=APP_SHA,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    assert verify_backup_set(backup, expected_app_schema_version=6).app_schema_version == 6


def test_newer_application_schema_is_refused_even_when_entries_are_old(tmp_path: Path) -> None:
    shadow = tmp_path / "shadow.db"
    _make_db(shadow, schema=1)
    backup = create_backup_set(
        [BackupSource("shadow_state", shadow, "AUTHORITATIVE_SHADOW_STATE")],
        tmp_path / "backups",
        app_git_sha=APP_SHA,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    _rewrite_manifest(backup, lambda manifest: manifest.update(app_schema_version=7))
    with pytest.raises(SchemaIncompatible):
        verify_backup_set(backup, expected_app_schema_version=6)


def test_older_application_schema_is_staging_migration_required(tmp_path: Path) -> None:
    shadow = tmp_path / "shadow.db"
    _make_db(shadow, schema=1)
    backup = create_backup_set(
        [BackupSource("shadow_state", shadow, "AUTHORITATIVE_SHADOW_STATE")],
        tmp_path / "backups",
        app_git_sha=APP_SHA,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    _rewrite_manifest(backup, lambda manifest: manifest.update(app_schema_version=5))
    verification = verify_backup_set(backup, expected_app_schema_version=6)
    assert verification.app_schema_version == 5
    result = restore_to_staging(
        backup,
        tmp_path / "staging",
        runtime_root=tmp_path / "runtime",
        expected_app_schema_version=6,
    )
    assert result.migration_required is True


@pytest.mark.parametrize("invalid_schema", [None, True, 0, -1, "6"])
def test_manifest_application_schema_must_be_positive_integer(
    tmp_path: Path, invalid_schema: object
) -> None:
    shadow = tmp_path / "shadow.db"
    _make_db(shadow, schema=1)
    backup = create_backup_set(
        [BackupSource("shadow_state", shadow, "AUTHORITATIVE_SHADOW_STATE")],
        tmp_path / "backups",
        app_git_sha=APP_SHA,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    _rewrite_manifest(backup, lambda manifest: manifest.update(app_schema_version=invalid_schema))
    with pytest.raises(ManifestInvalid):
        verify_backup_set(backup)


@pytest.mark.parametrize("invalid_sha", ["a" * 7, "main", "g" * 40])
def test_manifest_requires_full_lowercase_git_sha(tmp_path: Path, invalid_sha: str) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    with pytest.raises(ManifestInvalid):
        create_backup_set(
            [BackupSource("trading_state", source, "AUTHORITATIVE_TRADING_STATE")],
            tmp_path / "backups",
            app_git_sha=invalid_sha,
            app_schema_version=6,
            source_identity=SOURCE_IDENTITY,
        )


def test_restore_verification_requires_explicit_successful_drill(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    backup = _create(tmp_path / "backups", source)
    initial = verify_backup_set(backup)
    assert initial.trusted is True
    assert initial.restore_verified is False
    started = datetime(2026, 1, 1, 10, tzinfo=UTC)
    completed = started + timedelta(seconds=3)
    drills = tmp_path / "drills"
    record_restore_drill(
        drills,
        drill_id="drill-001",
        backup_id=initial.backup_id,
        manifest_sha256=str(initial.manifest["manifest_sha256"]),
        started_at=started,
        completed_at=completed,
        result="PASS",
        integrity="PASS",
        application_open_test="PASS",
        recovery_gate_present=True,
    )
    drilled = verify_backup_set(backup, restore_drill_root=drills)
    assert drilled.restore_verified is True
    assert drilled.restore_drill_at == completed


def test_creation_uses_configured_disk_safety_margin(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _make_db(source)
    with pytest.raises(InsufficientDisk):
        create_backup_set(
            [BackupSource("trading_state", source, "AUTHORITATIVE_TRADING_STATE")],
            tmp_path / "backups",
            app_git_sha=APP_SHA,
            app_schema_version=6,
            source_identity=SOURCE_IDENTITY,
            disk_usage_fn=lambda _: type("Usage", (), {"free": 10 * 1024 * 1024})(),
        )


def test_writer_startup_fails_closed_when_trading_restore_is_unreconciled(tmp_path: Path) -> None:
    from btcquant.execution.carry_runner import CarryRunner
    from btcquant.execution.runner import LiveRunner
    from btcquant.risk import RiskConfig

    write_recovery_marker(
        tmp_path / "state",
        backup_id="trading-1",
        restored_app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )

    class NoopBroker:
        external_execution = False

    risk = RiskConfig(initial_capital=1000.0, max_drawdown_halt=0.5, daily_loss_limit=0.5)
    with pytest.raises(RecoveryRequired):
        LiveRunner(
            [],
            NoopBroker(),
            risk,
            "paper",
            "BTC/USDT",
            tmp_path / "state" / "btcquant.db",
        )
    with pytest.raises(RecoveryRequired):
        CarryRunner(state_file=tmp_path / "state" / "btcquant.db")


def test_research_governance_restore_creates_separate_search_gate(tmp_path: Path) -> None:
    source = tmp_path / "governance.db"
    _make_db(source)
    backup = create_backup_set(
        [BackupSource("governance_state", source, "AUTHORITATIVE_RESEARCH_GOVERNANCE")],
        tmp_path / "backups",
        app_git_sha=APP_SHA,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    result = restore_to_staging(
        backup,
        tmp_path / "research-staging",
        runtime_root=tmp_path / "research-runtime",
        expected_app_schema_version=6,
    )
    assert result.recovery_marker is None
    assert (
        result.research_recovery_marker
        == tmp_path / "research-runtime" / "research-recovery-state.json"
    )
    with pytest.raises(ResearchRecoveryRequired):
        assert_research_recovery_clear(tmp_path / "research-runtime")


def test_capture_skew_is_recorded_as_degraded_and_not_trusted(tmp_path: Path) -> None:
    source_a = tmp_path / "a.db"
    source_b = tmp_path / "b.db"
    _make_db(source_a)
    _make_db(source_b)

    def delay_after_first(phase: str) -> None:
        if phase == "after-entry:trading_state":
            time.sleep(0.01)

    backup = create_backup_set(
        [
            BackupSource("trading_state", source_a, "AUTHORITATIVE_TRADING_STATE"),
            BackupSource("shadow_state", source_b, "AUTHORITATIVE_SHADOW_STATE"),
        ],
        tmp_path / "backups",
        app_git_sha=APP_SHA,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
        max_capture_skew_seconds=0.0,
        failure_hook=delay_after_first,
    )
    verification = verify_backup_set(backup)
    assert verification.state == "DEGRADED"
    assert verification.trusted is False
    assert verification.restore_verified is False


def test_quiescence_requires_all_writer_services_timers_and_open_handles() -> None:
    services = {name: "inactive" for name in DB_WRITER_SERVICES}
    timers = {name: "inactive" for name in DB_WRITER_TIMERS}
    require_writer_quiescence(services, timers)
    services["btcquant-carry.service"] = "active"
    with pytest.raises(WritersNotQuiesced, match="MIGRATION_REFUSED_WRITERS_ACTIVE"):
        require_writer_quiescence(services, timers)
    services["btcquant-carry.service"] = "inactive"
    with pytest.raises(WritersNotQuiesced, match="open_handles"):
        require_writer_quiescence(services, timers, open_handles=["/state/btcquant.db-wal"])
    with pytest.raises(WritersNotQuiesced, match="missing_services"):
        require_writer_quiescence({}, timers)


def test_restore_drill_record_is_non_secret_and_auditable(tmp_path: Path) -> None:
    started = datetime(2026, 1, 1, 10, tzinfo=UTC)
    completed = started + timedelta(seconds=3)
    record = record_restore_drill(
        tmp_path / "drills",
        drill_id="drill-001",
        backup_id="backup-001",
        manifest_sha256="a" * 64,
        started_at=started,
        completed_at=completed,
        result="PASS",
        integrity="PASS",
        application_open_test="PASS",
        recovery_gate_present=True,
    )
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["backup_id"] == "backup-001"
    assert payload["recovery_gate_present"] is True
    assert payload["manifest_sha256"] == "a" * 64
    assert "secret" not in record.read_text(encoding="utf-8").lower()
    with pytest.raises(BackupError, match="restore drill ID already exists"):
        record_restore_drill(
            tmp_path / "drills",
            drill_id="drill-001",
            backup_id="backup-001",
            manifest_sha256="a" * 64,
            started_at=started,
            completed_at=completed,
            result="PASS",
            integrity="PASS",
            application_open_test="PASS",
            recovery_gate_present=True,
        )


def test_shadow_restore_has_separate_writer_gate_and_ro_access(tmp_path: Path) -> None:
    from btcquant.execution.shadow import ShadowStore

    source = tmp_path / "shadow.db"
    _make_db(source)
    backup = create_backup_set(
        [BackupSource("shadow_state", source, "AUTHORITATIVE_SHADOW_STATE")],
        tmp_path / "backups",
        app_git_sha=APP_SHA,
        app_schema_version=6,
        source_identity=SOURCE_IDENTITY,
    )
    runtime = tmp_path / "runtime"
    result = restore_to_staging(
        backup,
        tmp_path / "shadow-staging",
        runtime_root=tmp_path / "shadow-staging",
        expected_app_schema_version=6,
    )
    runtime = tmp_path / "shadow-staging"
    shadow_db = result.staging_path / "execution-shadow.db"
    assert ShadowStore(shadow_db, read_only=True).path == shadow_db
    with pytest.raises(RecoveryRequired):
        ShadowStore(shadow_db)
    with pytest.raises(RecoveryRequired):
        assert_shadow_recovery_clear(runtime)


def test_offhost_export_admits_only_completed_verified_backup(tmp_path: Path) -> None:
    source = tmp_path / "btcquant.db"
    _make_db(source)
    backup = create_backup_set(
        [BackupSource("trading_state", source, "AUTHORITATIVE_TRADING_STATE")],
        tmp_path / "backups",
        app_git_sha="a" * 40,
        app_schema_version=6,
        source_identity="temporary",
    )
    calls: list[tuple[Path, Path]] = []

    def exporter(source_path: Path, destination: Path, verification: BackupVerification) -> None:
        assert verification.trusted is True
        calls.append((source_path, destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"encrypted-test-payload")

    destination = tmp_path / "offhost" / "backup.enc"
    result = export_verified_backup_set(backup, destination, exporter=exporter)
    assert result.trusted is True
    assert calls == [(backup, destination)]
    assert destination.read_bytes() == b"encrypted-test-payload"


def test_offhost_export_refuses_degraded_backup_without_calling_exporter(
    tmp_path: Path,
) -> None:
    source = tmp_path / "btcquant.db"
    _make_db(source)
    backup = create_backup_set(
        [BackupSource("trading_state", source, "AUTHORITATIVE_TRADING_STATE")],
        tmp_path / "backups",
        app_git_sha="b" * 40,
        app_schema_version=6,
        source_identity="temporary",
        max_capture_skew_seconds=0,
    )
    called = False

    def exporter(_source: Path, _destination: Path, _verification: object) -> None:
        nonlocal called
        called = True

    with pytest.raises(ExportRefused, match="OFFHOST_EXPORT_REFUSED"):
        export_verified_backup_set(
            backup,
            tmp_path / "offhost" / "backup.enc",
            exporter=exporter,
        )
    assert called is False


@pytest.mark.parametrize("invalid_state", ["CREATING", "FAILED", "QUARANTINED"])
def test_offhost_export_refuses_nonpublishable_states(tmp_path: Path, invalid_state: str) -> None:
    import btcquant.backup as backup_module

    source = tmp_path / "btcquant.db"
    _make_db(source)
    backup = create_backup_set(
        [BackupSource("trading_state", source, "AUTHORITATIVE_TRADING_STATE")],
        tmp_path / "backups",
        app_git_sha="c" * 40,
        app_schema_version=6,
        source_identity="temporary",
    )
    manifest_path = backup / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["state"] = invalid_state
    backup_module._write_manifest(backup, manifest)
    with pytest.raises(ManifestInvalid):
        export_verified_backup_set(
            backup,
            tmp_path / "offhost" / "backup.enc",
            exporter=lambda *_args: pytest.fail("exporter called"),
        )
