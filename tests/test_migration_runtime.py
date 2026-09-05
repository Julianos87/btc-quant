"""Regression: schema migration runs from the TARGET release venv, not current."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import venv
from pathlib import Path

from btcquant.deployment import inspect_sqlite
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore
from test_deployment_protocol import _make_realistic_v4_fixture

ROOT = Path(__file__).resolve().parents[1]
MIGRATE_SH = ROOT / "deploy" / "migrate.sh"
UPDATE_SH = ROOT / "deploy" / "update.sh"
TARGET_SHA = "a" * 40
OTHER_SHA = "b" * 40


def _write_exec(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _tools(root: Path, *, active: bool = False) -> Path:
    tools = root / "tools"
    tools.mkdir(exist_ok=True)
    _write_exec(
        tools / "id",
        """\
        #!/bin/sh
        if [ "$1" = "-u" ]; then echo 0; exit 0; fi
        exec /usr/bin/id "$@"
        """,
    )
    state = "active" if active else "inactive"
    _write_exec(
        tools / "systemctl",
        f"""\
        #!/bin/sh
        prop=""
        while [ "$#" -gt 0 ]; do
          case "$1" in
            --property=*) prop=${{1#--property=}} ;;
            --property) shift; prop=$1 ;;
          esac
          shift
        done
        case "$prop" in
          LoadState) echo loaded ;;
          ActiveState) echo {state} ;;
          *) exit 1 ;;
        esac
        """,
    )
    return tools


def _old_current(root: Path) -> Path:
    old = root / "releases" / ("c" * 40)
    python = old / "venv" / "bin" / "python"
    marker = old / "old-python.invoked"
    _write_exec(
        python,
        f"""\
        #!/usr/bin/env bash
        printf 'OLD_CURRENT_PYTHON\\n' >> {marker}
        echo "No module named btcquant.entrypoints.migrate" >&2
        exit 1
        """,
    )
    (root / "current").symlink_to(old)
    return old


def _target_release(root: Path, *, sha: str = TARGET_SHA, manifest_sha: str | None = None) -> Path:
    target = root / "releases" / sha
    deploy = target / "deploy"
    deploy.mkdir(parents=True)
    (deploy / "migrate.sh").write_text(MIGRATE_SH.read_text(encoding="utf-8"), encoding="utf-8")
    (deploy / "migrate.sh").chmod(0o755)
    venv.create(target / "venv", with_pip=False, symlinks=True)
    py_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = target / "venv" / "lib" / py_version / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    parent_site = Path(sys.prefix) / "lib" / py_version / "site-packages"
    (site / "btcquant-src.pth").write_text(
        f"{ROOT / 'src'}\n{parent_site}\nimport btcquant_migrate_harness\n",
        encoding="utf-8",
    )
    (site / "btcquant_migrate_harness.py").write_text(
        textwrap.dedent(
            """\
            import json
            import os
            import sys
            from pathlib import Path

            proof = os.environ.get("BTCQUANT_MIGRATE_PROOF")
            if proof:
                import btcquant.execution.state_store as state_store

                Path(proof).write_text(
                    json.dumps(
                        {
                            "executable": sys.executable,
                            "state_store": state_store.__file__,
                            "schema": state_store.SCHEMA_VERSION,
                        }
                    ),
                    encoding="utf-8",
                )
            quiescence = os.environ.get("BTCQUANT_TEST_QUIESCENCE")
            handles = os.environ.get("BTCQUANT_TEST_HANDLES")
            if quiescence or handles:
                import btcquant.deployment as deployment

                if quiescence == "ok":
                    deployment.systemd_writer_quiescence_failures = lambda: []
                elif quiescence == "fail":
                    deployment.systemd_writer_quiescence_failures = lambda: [
                        "btcquant-trend.service: état active"
                    ]
                if handles == "ok":
                    deployment.open_database_handle_failures = lambda *args, **kwargs: []
                elif handles == "fail":
                    deployment.open_database_handle_failures = lambda *args, **kwargs: [
                        "btcquant.db-wal still open"
                    ]
            """
        ),
        encoding="utf-8",
    )
    (target / "release-manifest.json").write_text(
        json.dumps({"git_sha": manifest_sha if manifest_sha is not None else sha}),
        encoding="utf-8",
    )
    return target


def _run_migrate(
    root: Path,
    target: Path,
    *,
    sha: str = TARGET_SHA,
    extra_env: dict[str, str] | None = None,
    active: bool = False,
) -> subprocess.CompletedProcess[str]:
    tools = _tools(root, active=active)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PATH"] = f"{tools}{os.pathsep}{env['PATH']}"
    env["BTCQUANT_ROOT"] = str(root)
    env["BTCQUANT_CURRENT"] = str(root / "current")
    env["BTCQUANT_DATABASE"] = str(root / "state" / "btcquant.db")
    env["BTCQUANT_DEPLOY_LOCK_HELD"] = "true"
    env["BTCQUANT_TEST_QUIESCENCE"] = (
        extra_env.get("BTCQUANT_TEST_QUIESCENCE", "ok") if extra_env else "ok"
    )
    env["BTCQUANT_TEST_HANDLES"] = (
        extra_env.get("BTCQUANT_TEST_HANDLES", "ok") if extra_env else "ok"
    )
    env["BTCQUANT_MIGRATE_PROOF"] = str(root / "runtime-proof.json")
    if extra_env:
        env.update(extra_env)
    backup = root / "backups" / f"pre-migration-{sha}.db"
    backup.parent.mkdir(exist_ok=True)
    return subprocess.run(
        [
            "bash",
            str(target / "deploy" / "migrate.sh"),
            "--sha",
            sha,
            "--confirm-migration",
            "--backup",
            str(backup),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_pre_fix_contract_is_gone_from_scripts() -> None:
    migrate = MIGRATE_SH.read_text(encoding="utf-8")
    update = UPDATE_SH.read_text(encoding="utf-8")
    assert '"${CURRENT}/venv/bin/python"' not in migrate
    assert "Python de migration absent dans current." not in migrate
    _, _, after_lock = update.partition('bash "${TARGET}/deploy/migrate.sh"')
    before_lock, _, invoke = update.partition("BTCQUANT_DEPLOY_LOCK_HELD=true")
    del after_lock, before_lock
    migrate_env, _, _ = invoke.partition('bash "${TARGET}/deploy/migrate.sh"')
    assert "BTCQUANT_CURRENT=" not in migrate_env


def test_old_current_without_migrate_module_target_migrates_schema4(tmp_path: Path) -> None:
    old = _old_current(tmp_path)
    target = _target_release(tmp_path)
    database = tmp_path / "state" / "btcquant.db"
    database.parent.mkdir()
    _make_realistic_v4_fixture(database)
    current_before = (tmp_path / "current").resolve()
    old_mtime = (old / "venv" / "bin" / "python").stat().st_mtime_ns

    result = _run_migrate(tmp_path, target)

    assert result.returncode == 0, result.stderr
    assert "OLD_CURRENT_PYTHON" not in result.stderr
    assert not (old / "old-python.invoked").exists()
    assert inspect_sqlite(database).metadata_schema_version == SCHEMA_VERSION
    assert SCHEMA_VERSION == 14
    proof = json.loads((tmp_path / "runtime-proof.json").read_text(encoding="utf-8"))
    assert Path(proof["executable"]).resolve() == (target / "venv" / "bin" / "python").resolve()
    assert str(target / "venv") in proof["executable"]
    assert (
        Path(proof["state_store"]).resolve()
        == (ROOT / "src/btcquant/execution/state_store.py").resolve()
    )
    assert proof["schema"] == SCHEMA_VERSION
    assert (tmp_path / "current").resolve() == current_before == old.resolve()
    assert (old / "venv" / "bin" / "python").stat().st_mtime_ns == old_mtime
    backup = tmp_path / "backups" / f"pre-migration-{TARGET_SHA}.db"
    assert backup.is_file()
    assert inspect_sqlite(backup).metadata_schema_version == 4


def test_target_venv_missing_is_refused(tmp_path: Path) -> None:
    _old_current(tmp_path)
    target = _target_release(tmp_path)
    (target / "venv" / "bin" / "python").unlink()
    (tmp_path / "state").mkdir()
    result = _run_migrate(tmp_path, target)
    assert result.returncode != 0
    assert "Python de migration absent dans la release cible" in result.stderr


def test_target_manifest_missing_is_refused(tmp_path: Path) -> None:
    _old_current(tmp_path)
    target = _target_release(tmp_path)
    (target / "release-manifest.json").unlink()
    (tmp_path / "state").mkdir()
    result = _run_migrate(tmp_path, target)
    assert result.returncode != 0
    assert "manifeste de release absent" in result.stderr


def test_target_manifest_sha_mismatch_is_refused(tmp_path: Path) -> None:
    _old_current(tmp_path)
    target = _target_release(tmp_path, manifest_sha=OTHER_SHA)
    (tmp_path / "state").mkdir()
    result = _run_migrate(tmp_path, target)
    assert result.returncode != 0
    assert "migration release SHA != requested target SHA" in result.stderr


def test_requested_target_sha_exact_passes_identity_guard(tmp_path: Path) -> None:
    _old_current(tmp_path)
    target = _target_release(tmp_path, sha=TARGET_SHA, manifest_sha=TARGET_SHA)
    database = tmp_path / "state" / "btcquant.db"
    database.parent.mkdir()
    _make_realistic_v4_fixture(database)
    result = _run_migrate(tmp_path, target, sha=TARGET_SHA)
    assert result.returncode == 0, result.stderr
    assert inspect_sqlite(database).metadata_schema_version == SCHEMA_VERSION


def test_writers_active_refused_before_python(tmp_path: Path) -> None:
    old = _old_current(tmp_path)
    target = _target_release(tmp_path)
    (tmp_path / "state").mkdir()
    result = _run_migrate(tmp_path, target, active=True)
    assert result.returncode != 0
    assert "writer/timer actif" in result.stderr
    assert not (old / "old-python.invoked").exists()
    assert not (tmp_path / "backups" / f"pre-migration-{TARGET_SHA}.db").exists()


def test_open_db_handle_refused_without_backup(tmp_path: Path) -> None:
    _old_current(tmp_path)
    target = _target_release(tmp_path)
    database = tmp_path / "state" / "btcquant.db"
    database.parent.mkdir()
    _make_realistic_v4_fixture(database)
    result = _run_migrate(
        tmp_path,
        target,
        extra_env={"BTCQUANT_TEST_HANDLES": "fail", "BTCQUANT_TEST_QUIESCENCE": "ok"},
    )
    assert result.returncode == 4
    assert "descripteur DB/WAL/SHM encore ouvert" in result.stderr
    assert not (tmp_path / "backups" / f"pre-migration-{TARGET_SHA}.db").exists()
    assert inspect_sqlite(database).metadata_schema_version == 4


def test_schema7_database_is_idempotent_no_op(tmp_path: Path) -> None:
    _old_current(tmp_path)
    target = _target_release(tmp_path)
    database = tmp_path / "state" / "btcquant.db"
    database.parent.mkdir()
    StateStore(database)
    assert inspect_sqlite(database).metadata_schema_version == SCHEMA_VERSION
    result = _run_migrate(tmp_path, target)
    assert result.returncode == 0, result.stderr
    assert f"Schéma déjà à jour: {SCHEMA_VERSION}" in result.stdout
    assert inspect_sqlite(database).metadata_schema_version == SCHEMA_VERSION


def test_python_quiescence_failure_preserves_schema4_and_skips_backup(tmp_path: Path) -> None:
    _old_current(tmp_path)
    target = _target_release(tmp_path)
    database = tmp_path / "state" / "btcquant.db"
    database.parent.mkdir()
    _make_realistic_v4_fixture(database)
    result = _run_migrate(
        tmp_path,
        target,
        extra_env={"BTCQUANT_TEST_QUIESCENCE": "fail", "BTCQUANT_TEST_HANDLES": "ok"},
    )
    assert result.returncode == 4
    assert "quiescence non prouvée" in result.stderr
    assert not (tmp_path / "backups" / f"pre-migration-{TARGET_SHA}.db").exists()
    assert inspect_sqlite(database).metadata_schema_version == 4
