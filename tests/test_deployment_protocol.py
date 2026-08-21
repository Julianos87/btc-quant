from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from btcquant.deployment import (
    DeploymentAlreadyRunning,
    DeploymentProtocolError,
    atomic_switch_release,
    backup_sqlite_database,
    build_release_manifest,
    checkpoint_sqlite_wal,
    DB_WRITER_TIMERS,
    DB_WRITER_UNITS,
    deployment_lock,
    inspect_sqlite,
    migration_auto_rollback_allowed,
    migration_rollback_disposition,
    open_database_handle_failures,
    restore_sqlite_database,
    validate_canonical_repository,
    validate_release_manifest,
    writer_quiescence_failures,
)
from btcquant.entrypoints.migrate import main as migrate_main
from btcquant.execution.errors import MigrationRequiredError
from btcquant.execution.state_store import StateStore


def _release(root, name: str):
    release = root / "releases" / name
    release.mkdir(parents=True)
    for filename, content in {
        "uv.lock": "lock\n",
        "pyproject.toml": "[project]\nversion = '0.1.0'\n",
        "requirements.txt": "btcquant==0.1.0\n",
        "sbom.cdx.json": "{}\n",
    }.items():
        (release / filename).write_text(content, encoding="utf-8")
    config = release / "environments" / "paper"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("exchange: hyperliquid\n", encoding="utf-8")
    return release


def test_deployment_lock_is_non_blocking(tmp_path):
    lock = tmp_path / "deploy.lock"
    with deployment_lock(lock):
        with pytest.raises(DeploymentAlreadyRunning):
            with deployment_lock(lock):
                pass


def test_atomic_switch_updates_current_then_previous(tmp_path):
    old = _release(tmp_path, "a" * 40)
    previous = _release(tmp_path, "c" * 40)
    new = _release(tmp_path, "b" * 40)
    os.symlink(old, tmp_path / "current")
    os.symlink(previous, tmp_path / "previous")

    old_target, new_target = atomic_switch_release(tmp_path, new)

    assert old_target == old
    assert new_target == new
    assert (tmp_path / "current").resolve() == new
    assert (tmp_path / "previous").resolve() == old


def test_atomic_switch_restores_both_links_if_previous_fails(tmp_path, monkeypatch):
    old = _release(tmp_path, "a" * 40)
    previous = _release(tmp_path, "c" * 40)
    new = _release(tmp_path, "b" * 40)
    os.symlink(old, tmp_path / "current")
    os.symlink(previous, tmp_path / "previous")
    import btcquant.deployment as deployment

    original = deployment._replace_link
    failed = False

    def fail_previous(path, target):
        nonlocal failed
        if path.name == "previous" and not failed:
            failed = True
            raise OSError("simulated power loss")
        return original(path, target)

    monkeypatch.setattr(deployment, "_replace_link", fail_previous)
    with pytest.raises(DeploymentProtocolError, match="switch annulé"):
        atomic_switch_release(tmp_path, new)

    assert (tmp_path / "current").resolve() == old
    assert (tmp_path / "previous").resolve() == previous


def test_manifest_is_complete_and_secret_free(tmp_path):
    release = _release(tmp_path, "a" * 40)
    manifest = build_release_manifest(
        release,
        git_sha="a" * 40,
        git_tree="b" * 40,
        origin="https://github.com/example/btc-quant.git",
        python_version="3.12.0",
        uv_version="0.11.0",
        release_created_at="2026-08-12T00:00:00+00:00",
    )
    from btcquant.deployment import write_release_manifest

    write_release_manifest(release, manifest)
    assert validate_release_manifest(release, "a" * 40)["git_sha"] == "a" * 40
    assert "secret" not in json.dumps(manifest).lower()
    (release / "uv.lock").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(DeploymentProtocolError, match="Hash de provenance"):
        validate_release_manifest(release, "a" * 40)

    (release / ".env").write_text("TOKEN=do-not-read\n", encoding="utf-8")
    with pytest.raises(DeploymentProtocolError, match="Fichiers sensibles"):
        build_release_manifest(
            release,
            git_sha="a" * 40,
            git_tree="b" * 40,
            origin="origin",
            python_version="3.12.0",
            uv_version="0.11.0",
        )


def test_sqlite_backup_uses_integrity_check_and_metadata(tmp_path):
    database = tmp_path / "source.db"
    StateStore(database)
    backup = tmp_path / "backups" / "source.db"

    result = backup_sqlite_database(database, backup, target_git_sha="a" * 40)

    assert result["target_git_sha"] == "a" * 40
    assert result["source_schema_version"] == 6
    assert result["integrity_check"] == "ok"
    assert inspect_sqlite(backup).integrity_check == "ok"
    assert (
        json.loads((tmp_path / "backups" / "source.db.manifest.json").read_text())["backup_sha256"]
        == result["backup_sha256"]
    )


def test_migration_cli_requires_confirmation_and_then_is_idempotent(tmp_path):
    database = tmp_path / "legacy.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE funding_ledger")
        connection.execute("UPDATE metadata SET value='5' WHERE key='schema_version'")
        connection.commit()

    with pytest.raises(MigrationRequiredError):
        StateStore(database)
    assert (
        migrate_main(
            [
                "--database",
                str(database),
                "--backup",
                str(tmp_path / "backup.db"),
                "--target-git-sha",
                "a" * 40,
            ]
        )
        == 3
    )
    assert (
        migrate_main(
            [
                "--database",
                str(database),
                "--backup",
                str(tmp_path / "backup.db"),
                "--target-git-sha",
                "a" * 40,
                "--confirm-migration",
            ]
        )
        == 0
    )
    assert inspect_sqlite(database).metadata_schema_version == 6
    assert (
        migrate_main(
            [
                "--database",
                str(database),
                "--backup",
                str(tmp_path / "backup-second.db"),
                "--target-git-sha",
                "a" * 40,
            ]
        )
        == 0
    )


def test_backup_rejects_short_sha(tmp_path):
    database = tmp_path / "source.db"
    StateStore(database)
    with pytest.raises(DeploymentProtocolError, match="SHA cible"):
        backup_sqlite_database(database, tmp_path / "backup.db", target_git_sha="abc")


def test_atomic_switch_reports_manual_recovery_when_rollback_fails(tmp_path, monkeypatch):
    old = _release(tmp_path, "a" * 40)
    previous = _release(tmp_path, "c" * 40)
    new = _release(tmp_path, "b" * 40)
    os.symlink(old, tmp_path / "current")
    os.symlink(previous, tmp_path / "previous")
    import btcquant.deployment as deployment

    original = deployment._replace_link
    calls = 0

    def fail_switch_and_restore(path, target):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("rollback unavailable")
        return original(path, target)

    monkeypatch.setattr(deployment, "_replace_link", fail_switch_and_restore)
    with pytest.raises(DeploymentProtocolError, match="intervention manuelle"):
        atomic_switch_release(tmp_path, new)


def test_database_newer_than_code_is_rejected(tmp_path):
    database = tmp_path / "newer.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE metadata SET value='7' WHERE key='schema_version'")
        connection.commit()
    with pytest.raises(RuntimeError, match="plus récente"):
        StateStore(database)


def test_deployment_scripts_expose_fail_closed_guards():
    update = Path("deploy/update.sh").read_text(encoding="utf-8")
    create = Path("deploy/create-release.sh").read_text(encoding="utf-8")
    validate = Path("deploy/validate-release.sh").read_text(encoding="utf-8")
    migrate = Path("deploy/migrate.sh").read_text(encoding="utf-8")
    preflight = Path("deploy/preflight.sh").read_text(encoding="utf-8")
    assert "flock -n 9" in update
    assert "DEPLOY_REMOTE" in update
    assert "merge-base --is-ancestor" in update
    assert "--untracked-files=all" in update
    assert "--frozen" in create
    assert "--exclude /data" in create
    assert "--exclude .pytest_cache" in create
    assert "--exclude .hypothesis" in create
    assert "validate-release.sh" in create
    assert "release-manifest.json" in create
    assert create.index("validate-release.sh") < create.index("ln -s ../../state")
    assert create.index("load_config('environments/paper/config.yaml')") < create.index(
        "ln -s ../../state"
    )
    assert "pytest" in validate
    assert "check_baseline_provenance.py" in validate
    assert "pip-audit" in validate
    assert 'VALIDATION_ROOT="$(mktemp -d /tmp/btcquant-release-validation.' in validate
    assert 'VALIDATION_BIN="${VALIDATION_ROOT}/bin"' in validate
    assert 'VALIDATION_ENV="${RELEASE}/.validation-venv"' not in validate
    assert "-u BTCQUANT_ROOT" in validate
    assert "-u BTCQUANT_CURRENT" in validate
    assert "-u BTCQUANT_DATABASE" in validate
    assert "-u BTCQUANT_CLONE" in validate
    assert 'HYPOTHESIS_STORAGE_DIRECTORY="${VALIDATION_ROOT}/hypothesis"' in validate
    assert "-u BTCQUANT_ROOT" in create
    assert "--validate-existing" in create
    assert "refus de réutilisation" in create
    assert "--smoke" in create
    assert "--quarantine-new" in create
    assert "RELEASE BUILD REFUSED" in create
    assert "NEW_RELEASE_CREATED=1" in create
    assert create.index('mv "${STAGING}" "${TARGET}"') < create.index("--smoke")
    assert "-p no:cacheprovider" in validate
    assert validate.count("--no-cache") >= 2
    assert '--cache-dir "${MYPY_CACHE}"' in validate
    assert 'PIP_AUDIT_CACHE="${VALIDATION_ROOT}/pip-audit"' in validate
    assert 'COVERAGE_FILE="${COVERAGE_FILE}"' in validate
    assert "for transient in" in validate
    assert "--confirm-migration" in migrate
    assert "Migration explicite requise" in preflight
    assert "BTCQUANT_MIGRATION_PENDING" in preflight
    assert "MIGRATION_REFUSED" in migrate
    assert "BASH_SOURCE[0]" in migrate
    assert "MIGRATION_PYTHON" in migrate
    assert "MIGRATION_RELEASE" in migrate
    assert '"${CURRENT}/venv/bin/python"' not in migrate
    assert "Python de migration absent dans la release cible" in migrate
    assert "migration release SHA != requested target SHA" in migrate
    assert "BTCQUANT_CURRENT n'est pas consulté" in migrate
    migrate_invocation = update.split('bash "${TARGET}/deploy/migrate.sh"', 1)[0]
    migrate_env = migrate_invocation.rsplit("BTCQUANT_DEPLOY_LOCK_HELD", 1)[-1]
    assert "BTCQUANT_CURRENT=" not in migrate_env


V4_ORDERS_SQL = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engine TEXT NOT NULL,
    slot TEXT NOT NULL,
    intent_id TEXT NOT NULL UNIQUE,
    broker_order_id TEXT,
    order_type TEXT NOT NULL,
    side TEXT NOT NULL,
    requested_qty REAL NOT NULL CHECK(requested_qty >= 0),
    reference_price REAL,
    filled_qty REAL NOT NULL DEFAULT 0 CHECK(filled_qty >= 0),
    price REAL,
    fee REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN (
        'PENDING', 'OPEN', 'FILLED', 'PARTIAL', 'REJECTED',
        'FAILED', 'CANCELED', 'UNBALANCED', 'RECOVERED_ABORTED'
    )),
    reason TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _make_realistic_v4_fixture(path: Path) -> None:
    """Construit la forme v4 observée: les colonnes v5/v6 sont absentes."""

    StateStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_orders_logical_order_key")
        connection.execute("DROP INDEX idx_orders_status")
        connection.execute("ALTER TABLE orders RENAME TO orders_v6")
        connection.execute(V4_ORDERS_SQL)
        connection.execute(
            """
            INSERT INTO orders(
                id, engine, slot, intent_id, broker_order_id, order_type, side,
                requested_qty, reference_price, filled_qty, price, fee, status,
                reason, error, created_at, updated_at
            )
            SELECT id, engine, slot, intent_id, broker_order_id, order_type, side,
                   requested_qty, reference_price, filled_qty, price, fee, status,
                   reason, error, created_at, updated_at
            FROM orders_v6
            """
        )
        connection.execute("DROP TABLE orders_v6")
        connection.execute("CREATE INDEX idx_orders_status ON orders(status)")
        connection.execute("DROP TABLE funding_ledger")
        connection.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
        connection.execute(
            "INSERT INTO engine_state(engine,payload,updated_at) VALUES('carry','{\"equity\":10000}', '2026-08-12T00:00:00Z')"
        )
        connection.execute(
            """INSERT INTO positions(
                engine,slot,status,cash,entry_time,entry_price,qty,stop_price,
                direction,bars_held,best_close,stop_order_id,entry_fee,last_bar_ts,updated_at
            ) VALUES('carry','main','OPEN',10000,'2026-08-11T00:00:00Z',100000,0.1,95000,1,2,101000,NULL,1,'2026-08-12T00:00:00Z','2026-08-12T00:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO orders(
                engine,slot,intent_id,broker_order_id,order_type,side,requested_qty,
                reference_price,filled_qty,price,fee,status,reason,error,created_at,updated_at
            ) VALUES('trend','slot-a','legacy-intent','broker-1','MARKET','buy',1,100000,1,100000,1,'FILLED','entry',NULL,'2026-08-11T00:00:00Z','2026-08-11T00:00:01Z')"""
        )
        connection.execute(
            """INSERT INTO events(ts,engine,event_type,aggregate_type,aggregate_id,payload,correlation_id)
               VALUES('2026-08-11T00:00:01Z','trend','order_filled','order','legacy-intent','{}','corr-1')"""
        )
        connection.execute(
            """INSERT INTO incidents(fingerprint,engine,severity,kind,message,context,status,first_seen,last_seen)
               VALUES('fixture:incident','trend','WARNING','test','fixture','{}','OPEN','2026-08-11T00:00:00Z','2026-08-11T00:00:00Z')"""
        )
        connection.execute(
            """INSERT INTO trades(exit_ts,entry_ts,strategy,direction,qty,entry_price,exit_price,pnl,bars_held,reason)
               VALUES('2026-08-12T00:00:00Z','2026-08-11T00:00:00Z','fixture','LONG',0.1,100000,101000,100,2,'exit')"""
        )
        connection.execute(
            "INSERT INTO equity_samples(engine,ts,equity) VALUES('carry','2026-08-12T00:00:00Z',10000)"
        )
        connection.execute(
            "INSERT INTO flows(ts,kind,trend_flow,carry_flow) VALUES('2026-08-01T00:00:00Z','deposit',100,100)"
        )
        connection.execute(
            "INSERT INTO capital_deposits(deposit_id,amount,status,requested_at) VALUES('fixture:1',200,'APPLIED','2026-08-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO qualification_campaigns(protocol_version,status,policy,started_at) VALUES(1,'CANCELED','{}','2026-08-01T00:00:00Z')"
        )
        connection.commit()


def _table_columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def test_writer_quiescence_requires_every_unit_and_timer_to_be_proven_stopped():
    inactive_units = {unit: "inactive" for unit in DB_WRITER_UNITS}
    inactive_timers = {timer: "inactive" for timer in DB_WRITER_TIMERS}
    assert writer_quiescence_failures(inactive_units, inactive_timers) == []
    active_units = dict(inactive_units)
    active_units["btcquant-dashboard.service"] = "active"
    assert any(
        "btcquant-dashboard.service" in item
        for item in writer_quiescence_failures(active_units, inactive_timers)
    )
    missing_timer = dict(inactive_timers)
    del missing_timer["btcquant-backup.timer"]
    assert any(
        "btcquant-backup.timer" in item
        for item in writer_quiescence_failures(inactive_units, missing_timer)
    )


def test_quiescence_refusal_leaves_database_and_release_state_untouched(tmp_path, monkeypatch):
    database = tmp_path / "v5.db"
    StateStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE funding_ledger")
        connection.execute("UPDATE metadata SET value='5' WHERE key='schema_version'")
        connection.commit()
    backup = tmp_path / "backup.db"
    monkeypatch.setattr(
        "btcquant.entrypoints.migrate.systemd_writer_quiescence_failures",
        lambda: ["btcquant-trend.service: état active"],
    )
    assert (
        migrate_main(
            [
                "--database",
                str(database),
                "--backup",
                str(backup),
                "--target-git-sha",
                "a" * 40,
                "--confirm-migration",
                "--require-quiescence",
            ]
        )
        == 4
    )
    assert not backup.exists()
    assert inspect_sqlite(database).metadata_schema_version == 5


def test_v4_to_v6_migration_preserves_realistic_data_and_is_idempotent(tmp_path):
    database = tmp_path / "v4.db"
    _make_realistic_v4_fixture(database)
    backup = tmp_path / "backups" / "pre.db"
    before = inspect_sqlite(database)
    assert before.metadata_schema_version == 4

    args = [
        "--database",
        str(database),
        "--backup",
        str(backup),
        "--target-git-sha",
        "a" * 40,
        "--confirm-migration",
    ]
    assert migrate_main(args) == 0
    after = inspect_sqlite(database)
    assert after.metadata_schema_version == 6
    assert after.integrity_check == "ok"
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='funding_ledger'"
            ).fetchone()[0]
            == 1
        )
    assert {
        "logical_order_key",
        "remaining_qty",
        "local_state",
        "external_state",
    } <= _table_columns(database, "orders")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM orders").fetchone()[0] == 1
        assert connection.execute("SELECT intent_id FROM orders").fetchone()[0] == "legacy-intent"
        assert (
            connection.execute("SELECT payload FROM engine_state WHERE engine='carry'").fetchone()[
                0
            ]
            == '{"equity":10000}'
        )
        assert connection.execute("SELECT count(*) FROM incidents").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='idx_orders_logical_order_key'"
            ).fetchone()[0]
            == 1
        )
    migrated_hash = after.sha256
    assert migrate_main(args) == 0
    assert inspect_sqlite(database).metadata_schema_version == 6
    assert inspect_sqlite(database).sha256 == migrated_hash


def test_v4_backup_restore_returns_old_schema_and_data(tmp_path):
    database = tmp_path / "v4.db"
    _make_realistic_v4_fixture(database)
    backup = tmp_path / "pre.db"
    backup_sqlite_database(database, backup, target_git_sha="b" * 40)
    StateStore(database, allow_migration=True)
    restored = tmp_path / "restored.db"
    shutil.copy2(database, restored)
    result = restore_sqlite_database(backup, restored)
    assert result["schema_version"] == 4
    assert inspect_sqlite(restored).integrity_check == "ok"
    with sqlite3.connect(restored) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "4"
        )
        assert connection.execute("SELECT intent_id FROM orders").fetchone()[0] == "legacy-intent"
        assert (
            connection.execute("SELECT payload FROM engine_state WHERE engine='carry'").fetchone()[
                0
            ]
            == '{"equity":10000}'
        )


def test_wal_checkpoint_and_backup_are_sqlite_managed(tmp_path):
    database = tmp_path / "wal.db"
    StateStore(database).save_engine_state("carry", {"equity": 123.0})
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        connection.execute(
            "INSERT INTO events(ts,engine,event_type,payload) VALUES('t','carry','wal','{}')"
        )
        connection.commit()
    result = checkpoint_sqlite_wal(database)
    assert result[0] == 0
    backup = tmp_path / "wal-backup.db"
    info = backup_sqlite_database(database, backup, target_git_sha="c" * 40)
    assert info["integrity_check"] == "ok"
    with sqlite3.connect(backup) as connection:
        assert (
            connection.execute("SELECT count(*) FROM events WHERE event_type='wal'").fetchone()[0]
            == 1
        )


def test_migration_failure_before_v5_rolls_back_atomically(tmp_path, monkeypatch):
    database = tmp_path / "v4-fail-v5.db"
    _make_realistic_v4_fixture(database)
    monkeypatch.setattr(
        StateStore,
        "_ensure_order_safety_schema",
        staticmethod(lambda connection: (_ for _ in ()).throw(RuntimeError("v5 injected"))),
    )
    with pytest.raises(RuntimeError, match="v5 injected"):
        StateStore(database, allow_migration=True)
    assert inspect_sqlite(database).metadata_schema_version == 4
    assert "logical_order_key" not in _table_columns(database, "orders")
    assert "funding_ledger" not in {
        row[0]
        for row in sqlite3.connect(database).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def test_migration_failure_after_v5_before_v6_rolls_back_atomically(tmp_path, monkeypatch):
    database = tmp_path / "v4-fail-v6.db"
    _make_realistic_v4_fixture(database)

    def fail_v6(cls, connection):
        raise RuntimeError("v6 injected")

    monkeypatch.setattr(StateStore, "_migrate_v6", classmethod(fail_v6))
    with pytest.raises(RuntimeError, match="v6 injected"):
        StateStore(database, allow_migration=True)
    assert inspect_sqlite(database).metadata_schema_version == 4
    assert "logical_order_key" not in _table_columns(database, "orders")
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='funding_ledger'"
            ).fetchone()[0]
            == 0
        )


def test_migration_failure_inside_v6_rolls_back_atomically(tmp_path, monkeypatch):
    database = tmp_path / "v4-fail-inside-v6.db"
    _make_realistic_v4_fixture(database)
    original = StateStore._ensure_funding_accounting_schema

    def partial_then_fail(connection):
        original(connection)
        raise RuntimeError("v6 partial injected")

    monkeypatch.setattr(
        StateStore, "_ensure_funding_accounting_schema", staticmethod(partial_then_fail)
    )
    with pytest.raises(RuntimeError, match="v6 partial injected"):
        StateStore(database, allow_migration=True)
    assert inspect_sqlite(database).metadata_schema_version == 4
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='funding_ledger'"
            ).fetchone()[0]
            == 0
        )


def test_canonical_remote_identity_requires_explicit_alias_mapping():
    expected = "https://github.com/Julianos87/btc-quant.git"
    with pytest.raises(DeploymentProtocolError, match="Alias remote non configuré"):
        validate_canonical_repository("github-backup:Julianos87/btc-quant.git", expected)
    assert validate_canonical_repository(
        "github-backup:Julianos87/btc-quant.git",
        expected,
        allowed_aliases={"github-backup": "github.com"},
    )
    assert validate_canonical_repository("git@github.com:Julianos87/btc-quant.git", expected)
    assert validate_canonical_repository("ssh://git@github.com/Julianos87/btc-quant.git", expected)
    with pytest.raises(DeploymentProtocolError, match="Repository canonique"):
        validate_canonical_repository(
            "github-backup:attacker/btc-quant.git",
            expected,
            allowed_aliases={"github-backup": "github.com"},
        )


def test_open_database_handle_gate_is_fail_closed(tmp_path):
    database = tmp_path / "btcquant.db"
    database.touch()
    proc = tmp_path / "proc" / "1234" / "fd"
    proc.mkdir(parents=True)
    os.symlink(database, proc / "7")
    assert open_database_handle_failures((database,), proc_root=tmp_path / "proc") == [
        f"pid 1234 holds {database}"
    ]
    (proc / "7").unlink()
    assert open_database_handle_failures((database,), proc_root=tmp_path / "proc") == []
    assert open_database_handle_failures((database,), proc_root=tmp_path / "missing")


def test_pre_writer_migration_failure_restores_v4_backup_and_old_release(tmp_path):
    database = tmp_path / "v4.db"
    _make_realistic_v4_fixture(database)
    backup = tmp_path / "pre.db"
    backup_sqlite_database(database, backup, target_git_sha="d" * 40)
    old = _release(tmp_path, "a" * 40)
    new = _release(tmp_path, "b" * 40)
    os.symlink(old, tmp_path / "current")
    os.symlink(old, tmp_path / "previous")
    StateStore(database, allow_migration=True)
    atomic_switch_release(tmp_path, new)
    assert (
        migration_rollback_disposition(db_migrated=True, target_writers_started=False)
        == "AUTO_DB_RESTORE_AND_CODE_ROLLBACK"
    )
    restored = restore_sqlite_database(backup, database)
    assert restored["schema_version"] == 4
    assert inspect_sqlite(database).integrity_check == "ok"
    atomic_switch_release(tmp_path, old)
    assert (tmp_path / "current").resolve() == old


def test_first_writer_started_refuses_db_restore_and_old_code(tmp_path):
    database = tmp_path / "v4.db"
    _make_realistic_v4_fixture(database)
    backup = tmp_path / "pre.db"
    backup_sqlite_database(database, backup, target_git_sha="e" * 40)
    StateStore(database, allow_migration=True)
    assert inspect_sqlite(database).metadata_schema_version == 6
    assert not migration_auto_rollback_allowed(db_migrated=True, target_writers_started=True)
    assert (
        migration_rollback_disposition(db_migrated=True, target_writers_started=True)
        == "MANUAL_RECOVERY_REQUIRED"
    )
    assert backup.exists()
    assert inspect_sqlite(database).metadata_schema_version == 6


def test_durable_write_after_writer_frontier_also_requires_manual_recovery(tmp_path):
    database = tmp_path / "v4.db"
    _make_realistic_v4_fixture(database)
    StateStore(database, allow_migration=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO events(ts,engine,event_type,payload) VALUES('t','carry','post-write','{}')"
        )
        connection.commit()
    assert (
        migration_rollback_disposition(db_migrated=True, target_writers_started=True)
        == "MANUAL_RECOVERY_REQUIRED"
    )
    assert inspect_sqlite(database).metadata_schema_version == 6


def test_migration_rollback_frontier_is_irreversible_after_writer_start():
    assert migration_auto_rollback_allowed(db_migrated=False, target_writers_started=False)
    assert migration_auto_rollback_allowed(db_migrated=True, target_writers_started=False)
    assert not migration_auto_rollback_allowed(db_migrated=True, target_writers_started=True)
    assert (
        migration_rollback_disposition(db_migrated=False, target_writers_started=False)
        == "AUTO_CODE_ROLLBACK"
    )


def test_source_git_boundary_is_process_local_and_path_scoped():
    for name in ("create-release.sh", "install.sh"):
        script = (Path("deploy") / name).read_text(encoding="utf-8")
        assert "source_git()" in script
        assert 'git -c "safe.directory=${SOURCE}" -C "${SOURCE}" "$@"' in script
        assert "safe.directory=*" not in script
        assert "git config --global" not in script
        assert "git config --system" not in script
        assert "git config --local" not in script
        assert 'git -C "${SOURCE}"' not in script


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root for the ownership boundary")
def test_process_local_source_git_handles_foreign_owned_checkout(tmp_path):
    """Reproduce the VPS root/btcquant Git boundary without persistent config."""
    import pwd
    import subprocess

    try:
        owner = pwd.getpwnam("btcquant")
    except KeyError:
        pytest.skip("btcquant user is unavailable")

    source = tmp_path / "source"
    other = tmp_path / "other"
    for repository in (source, other):
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        (repository / "README").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "README"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        for path in repository.rglob("*"):
            os.chown(path, owner.pw_uid, owner.pw_gid)
        os.chown(repository, owner.pw_uid, owner.pw_gid)

    plain = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert plain.returncode != 0
    assert "dubious ownership" in plain.stderr

    scoped = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={source}",
            "-C",
            str(source),
            "rev-parse",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert scoped.returncode == 0
    assert len(scoped.stdout.strip()) == 40

    wrong_scope = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={source}",
            "-C",
            str(other),
            "rev-parse",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert wrong_scope.returncode != 0
    assert "dubious ownership" in wrong_scope.stderr


def test_validate_release_isolates_and_cleans_validation_artifacts(tmp_path):
    import subprocess
    import textwrap

    release = tmp_path / "release"
    tool_dir = tmp_path / "tools"
    log_dir = tmp_path / "success-log"
    release.mkdir()
    tool_dir.mkdir()
    log_dir.mkdir()

    (release / "requirements.txt").write_text("fixture==1\n", encoding="utf-8")
    (release / "dashboard/static").mkdir(parents=True)
    (release / "dashboard/static/dashboard.js").write_text(
        "const dashboard = true;\n", encoding="utf-8"
    )
    (release / "dashboard/static/effects.js").write_text(
        "const effects = true;\n", encoding="utf-8"
    )
    for relative in (
        "deploy/install.sh",
        "deploy/update.sh",
        "deploy/create-release.sh",
        "deploy/preflight.sh",
        "deploy/migrate.sh",
        "deploy/rebalance-root.sh",
        "deploy/resolve-uv.sh",
        "deploy/start-hyperliquid-testnet.sh",
        "deploy/stop-hyperliquid-testnet.sh",
        "scripts/backup_state.sh",
        "scripts/check_sbom.py",
        "scripts/check_baseline_provenance.py",
    ):
        path = release / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    def write_executable(path: Path, content: str) -> None:
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
        path.chmod(0o755)

    write_executable(
        tool_dir / "uv",
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        if [ "$1" = "sync" ]; then
          mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
          printf '%s\\n' "$UV_PROJECT_ENVIRONMENT" > "$FAKE_LOG/validation-env"
          for tool in pytest ruff mypy pip-audit python; do
            cp "$FAKE_TOOL_DIR/$tool" "$UV_PROJECT_ENVIRONMENT/bin/$tool"
          done
        elif [ "$1" = "run" ]; then
          shift
          while [ "$#" -gt 0 ]; do
            if [ "$1" = "mypy" ]; then
              shift
              exec "$FAKE_TOOL_DIR/mypy" "$@"
            fi
            shift
          done
          exit 51
        elif [ "$1" = "export" ]; then
          output=""
          while [ "$#" -gt 0 ]; do
            if [ "$1" = "--output-file" ]; then
              shift
              output="$1"
            fi
            shift
          done
          cp "$FAKE_REQUIREMENTS" "$output"
        fi
        """,
    )
    write_executable(
        tool_dir / "pytest",
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        case " $* " in *" -p no:cacheprovider "*) ;; *) exit 11 ;; esac
        case "$COVERAGE_FILE" in "$RELEASE"/*|"") exit 12 ;; esac
        if [ -n "${BTCQUANT_ROOT:-}" ] || [ -n "${BTCQUANT_CURRENT:-}" ] \
          || [ -n "${BTCQUANT_DATABASE:-}" ] || [ -n "${BTCQUANT_CLONE:-}" ]; then
          printf 'runtime root leaked into pytest\\n' >&2
          exit 15
        fi
        case "${HYPOTHESIS_STORAGE_DIRECTORY:-}" in
          "$RELEASE"/*|"") exit 16 ;;
        esac
        touch "$COVERAGE_FILE"
        if [ "$FAKE_FAIL" = 1 ]; then exit 14; fi
        """,
    )
    write_executable(
        tool_dir / "ruff",
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        case " $* " in *" --no-cache "*) ;; *) exit 21 ;; esac
        [ ! -e "$RELEASE/.ruff_cache" ]
        """,
    )
    write_executable(
        tool_dir / "mypy",
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        cache=""
        previous=""
        for arg in "$@"; do
          if [ "$previous" = 1 ]; then cache="$arg"; previous=""; fi
          if [ "$arg" = "--cache-dir" ]; then previous=1; fi
        done
        [ -n "$cache" ]
        case "$cache" in "$RELEASE"/*) exit 31 ;; esac
        mkdir -p "$cache"
        """,
    )
    write_executable(
        tool_dir / "pip-audit",
        """\
        #!/usr/bin/env bash
        set -euo pipefail
        cache=""
        previous=""
        for arg in "$@"; do
          if [ "$previous" = 1 ]; then cache="$arg"; previous=""; fi
          if [ "$arg" = "--cache-dir" ]; then previous=1; fi
        done
        [ -n "$cache" ]
        case "$cache" in "$RELEASE"/*) exit 41 ;; esac
        mkdir -p "$cache"
        """,
    )
    write_executable(tool_dir / "python", "#!/usr/bin/env bash\nexit 0\n")

    uv = tool_dir / "uv"
    before = {
        path.relative_to(release): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    }
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "FAKE_LOG": str(log_dir),
            "FAKE_TOOL_DIR": str(tool_dir),
            "FAKE_REQUIREMENTS": str(release / "requirements.txt"),
            "RELEASE": str(release),
            "FAKE_FAIL": "0",
            "BTCQUANT_ROOT": str(runtime_root),
            "BTCQUANT_CURRENT": str(runtime_root / "current"),
            "BTCQUANT_DATABASE": str(runtime_root / "state" / "btcquant.db"),
            "BTCQUANT_CLONE": str(runtime_root / "clone"),
        }
    )
    command = ["bash", "deploy/validate-release.sh", str(release), str(uv)]
    success = subprocess.run(
        command,
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert success.returncode == 0, success.stderr
    validation_env = Path((log_dir / "validation-env").read_text().strip())
    assert not validation_env.is_relative_to(release)
    assert not validation_env.parent.exists()
    assert {
        path.relative_to(release): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    } == before
    for transient in (
        ".validation-venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".coverage",
        ".uv-cache",
        ".hypothesis",
    ):
        assert not (release / transient).exists()

    failure_log = tmp_path / "failure-log"
    failure_log.mkdir()
    failure_environment = environment.copy()
    failure_environment.update({"FAKE_LOG": str(failure_log), "FAKE_FAIL": "1"})
    failure = subprocess.run(
        command,
        cwd=Path.cwd(),
        env=failure_environment,
        capture_output=True,
        text=True,
    )
    assert failure.returncode != 0
    failed_validation_env = Path((failure_log / "validation-env").read_text().strip())
    assert not failed_validation_env.parent.exists()
    assert {
        path.relative_to(release): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    } == before
