"""Régressions de sécurité qui ne nécessitent ni réseau ni systemd."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from btcquant import notify

ROOT = Path(__file__).resolve().parents[1]


def test_notification_error_never_logs_telegram_token(monkeypatch, caplog):
    token = "123456:super-secret-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def fail_with_sensitive_url(*_args, **_kwargs):
        raise RuntimeError(f"https://api.telegram.org/bot{token}/sendMessage")

    monkeypatch.setattr(notify.urllib.request, "urlopen", fail_with_sensitive_url)

    with caplog.at_level(logging.WARNING):
        assert notify.notify("test") is False

    assert token not in caplog.text
    assert "RuntimeError" in caplog.text


def test_backup_offsite_requires_encryption_and_checks_roundtrip():
    script = (ROOT / "scripts" / "backup_state.sh").read_text(encoding="utf-8")

    assert '[[ "${ARCHIVE}" == *.enc ]]' in script
    assert "openssl enc -aes-256-cbc -pbkdf2" in script
    assert "openssl enc -d -aes-256-cbc -pbkdf2" in script
    assert 'cmp --silent "${PLAIN_ARCHIVE}" "${ROUNDTRIP_ARCHIVE}"' in script


def test_unprivileged_services_drop_linux_capabilities():
    for name in (
        "btcquant-backup.service",
        "btcquant-carry.service",
        "btcquant-dashboard.service",
        "btcquant-digest.service",
        "btcquant-shadow.service",
        "btcquant-trend.service",
        "btcquant-watchdog.service",
        "btcquant-weekly.service",
    ):
        service = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "NoNewPrivileges=true" in service, name
        assert "ProtectSystem=strict" in service, name
        assert "CapabilityBoundingSet=" in service, name
        assert "RestrictSUIDSGID=true" in service, name
        assert "LockPersonality=true" in service, name


def test_services_execute_only_from_the_atomic_current_release():
    for path in (ROOT / "deploy").glob("*.service"):
        service = path.read_text(encoding="utf-8")
        if "User=root" in service:
            continue
        assert "/opt/btcquant/current" in service, path.name
        assert "/opt/btcquant/venv/" not in service, path.name


def test_dashboard_uses_a_production_wsgi_server():
    service = (ROOT / "deploy" / "btcquant-dashboard.service").read_text(encoding="utf-8")

    assert "/gunicorn " in service
    assert "dashboard.wsgi:app" in service
    assert "dashboard/app.py" not in service


def test_runtime_services_use_installed_entrypoints():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]
    assert scripts["btcquant-trend"] == "btcquant.entrypoints.trend:main"
    assert scripts["btcquant-carry"] == "btcquant.entrypoints.carry:main"
    assert scripts["btcquant-readiness"] == "btcquant.entrypoints.readiness:main"
    assert scripts["btcquant-watchdog"] == "btcquant.entrypoints.watchdog:main"
    assert scripts["btcquant-digest"] == "btcquant.entrypoints.digest:main"
    assert scripts["btcquant-rebalance"] == "btcquant.entrypoints.rebalance:main"
    assert scripts["btcquant-shadow"] == "btcquant.entrypoints.shadow:main"

    trend = (ROOT / "deploy" / "btcquant-trend.service").read_text(encoding="utf-8")
    carry = (ROOT / "deploy" / "btcquant-carry.service").read_text(encoding="utf-8")
    watchdog = (ROOT / "deploy" / "btcquant-watchdog.service").read_text(encoding="utf-8")
    digest = (ROOT / "deploy" / "btcquant-digest.service").read_text(encoding="utf-8")
    weekly = (ROOT / "deploy" / "btcquant-weekly.service").read_text(encoding="utf-8")
    rebalance = (ROOT / "deploy" / "rebalance-root.sh").read_text(encoding="utf-8")
    shadow = (ROOT / "deploy" / "btcquant-shadow.service").read_text(encoding="utf-8")
    release = (ROOT / "deploy" / "create-release.sh").read_text(encoding="utf-8")
    assert "/venv/bin/btcquant-trend " in trend
    assert "/venv/bin/btcquant-carry " in carry
    assert "/venv/bin/btcquant-watchdog" in watchdog
    assert "/venv/bin/btcquant-digest" in digest
    assert "/venv/bin/btcquant-digest --weekly" in weekly
    assert "/venv/bin/btcquant-rebalance" in rebalance
    assert "--deposit-id" in rebalance
    assert "monthly:$(date -u +%Y-%m)" in rebalance
    assert "--check-pending" in rebalance
    assert "/venv/bin/btcquant-shadow " in shadow
    assert "EnvironmentFile=" not in shadow
    assert "HYPERLIQUID_PRIVATE_KEY" not in shadow
    assert "--frozen" in release
    assert "--no-editable" in release
    assert "resolve-uv.sh" in release
    assert "release-manifest.json" in release
    assert '[ "${#RELEASE_ID}" -ne 40 ]' in release
    assert 'chown -R root:btcquant "${STAGING}"' in release
    assert 'chown -R root:root "${STAGING}"' not in release
    assert "relocate_venv_launchers.py" in release
    assert "--old-prefix" in release
    assert "--new-prefix" in release


def test_production_requirements_are_pinned_with_hashes():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "--hash=sha256:" in requirements
    assert "--no-hashes" not in requirements.splitlines()[1]


def test_long_running_services_have_restart_rate_limits():
    for name in (
        "btcquant-trend.service",
        "btcquant-carry.service",
        "btcquant-dashboard.service",
        "btcquant-hyperliquid-testnet.service",
        "btcquant-shadow.service",
    ):
        service = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "StartLimitIntervalSec=600" in service
        assert "StartLimitBurst=5" in service


def test_watchdog_monitors_shadow_every_two_minutes():
    service = (ROOT / "deploy" / "btcquant-watchdog.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy" / "btcquant-watchdog.timer").read_text(encoding="utf-8")

    assert "--shadow-database /opt/btcquant/state/execution-shadow.db" in service
    assert "OnUnitActiveSec=2min" in timer


def test_update_has_atomic_activation_backup_healthcheck_and_rollback():
    script = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    assert "flock -n 9" in script
    assert "--sha" in script
    assert "DEPLOY_REMOTE" in script
    assert "DEPLOY_BRANCH" in script
    assert "--untracked-files=all" in script
    assert "merge-base --is-ancestor" in script
    assert "--migration" in script
    assert "pre-migration-${TARGET_SHA}.db" in script
    assert "migration_abort_on_error" in script
    assert "MANUAL RECOVERY REQUIRED" in script
    assert "systemctl enable --now btcquant-compact.timer" in script
    assert "configure_pending_rebalance_timer" in script
    pending_timer = (ROOT / "deploy" / "btcquant-rebalance-pending.timer").read_text(
        encoding="utf-8"
    )
    pending_service = (ROOT / "deploy" / "btcquant-rebalance-pending.service").read_text(
        encoding="utf-8"
    )
    assert "OnCalendar=*-*-* 04:30:00 UTC" in pending_timer
    assert "--pending-only" in pending_service
    assert "atomic_switch_release" in script
    assert "rollback_on_error" in script
    assert "TARGET_WRITES_STARTED=true" in script
    assert "restart_target_dashboard" in script
    assert "stop_all_writer_processes" in script
    assert "http://127.0.0.1:8666/healthz" in script
    assert "wait_for_dashboard" in script
    assert "for attempt in {1..15}" in script
    assert "systemd-analyze verify" in script
    assert "configure_shadow_service" in script
    assert 'systemd-analyze verify "${CURRENT}/deploy/"*.service' in script


def test_host_preflight_blocks_bad_clock_permissions_disk_and_database():
    script = (ROOT / "deploy" / "preflight.sh").read_text(encoding="utf-8")

    assert "NTPSynchronized" in script
    assert "root:btcquant:640" in script
    assert "1048576" in script
    assert "PRAGMA integrity_check" in script


def _run_uv_resolver(tmp_path, *, uv_bin=None, include_uv=True):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    if include_uv:
        command = fake_bin / "uv"
        command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin)
    if uv_bin is None:
        env.pop("UV_BIN", None)
    else:
        env["UV_BIN"] = uv_bin
    return subprocess.run(
        ["/bin/bash", str(ROOT / "deploy" / "resolve-uv.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_uv_resolution_is_absolute_and_fail_closed(tmp_path):
    resolved = _run_uv_resolver(tmp_path / "default")
    assert resolved.returncode == 0
    assert resolved.stdout.strip() == str((tmp_path / "default" / "bin" / "uv").resolve())

    named = _run_uv_resolver(tmp_path / "named", uv_bin="uv")
    assert named.returncode == 0
    assert named.stdout.strip().endswith("/named/bin/uv")

    absent = _run_uv_resolver(tmp_path / "absent", include_uv=False)
    assert absent.returncode != 0

    invalid = tmp_path / "invalid-uv"
    invalid.write_text("#!/bin/sh\n", encoding="utf-8")
    invalid.chmod(0o644)
    invalid_result = _run_uv_resolver(tmp_path / "invalid", uv_bin=str(invalid), include_uv=False)
    assert invalid_result.returncode != 0


def test_runtime_venv_relocates_without_staging_dependency(tmp_path):
    uv = shutil.which("uv") or os.environ.get("BTCQUANT_TEST_UV") or "/tmp/uv-0.11.19"
    if not Path(uv).is_file() or not os.access(uv, os.X_OK):
        pytest.fail("uv is required for the release relocation integration test")

    staging = tmp_path / "staging"
    final = tmp_path / "releases" / ("a" * 40)
    staging.mkdir()
    (tmp_path / "releases").mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        shutil.copy2(ROOT / name, staging / name)
    shutil.copytree(ROOT / "src", staging / "src")
    shutil.copytree(ROOT / "dashboard", staging / "dashboard")
    shutil.copytree(ROOT / "environments", staging / "environments")
    shutil.copy2(
        ROOT / "scripts" / "relocate_venv_launchers.py", staging / "relocate_venv_launchers.py"
    )

    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(staging / "venv")
    subprocess.run(
        [
            uv,
            "sync",
            "--frozen",
            "--no-dev",
            "--no-editable",
            "--python",
            "3.12",
            "--directory",
            str(staging),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    subprocess.run(
        [
            sys.executable,
            str(staging / "relocate_venv_launchers.py"),
            "--venv",
            str(staging / "venv"),
            "--old-prefix",
            str(staging),
            "--new-prefix",
            str(final),
        ],
        check=True,
    )
    staging.rename(final)
    assert not staging.exists()

    runtime_python = final / "venv" / "bin" / "python"
    imported = subprocess.run(
        [str(runtime_python), "-c", "import btcquant; import dashboard.app"],
        cwd=final,
        check=False,
        capture_output=True,
        text=True,
    )
    assert imported.returncode == 0, imported.stderr

    entrypoint = final / "venv" / "bin" / "btcquant-readiness"
    help_run = subprocess.run(
        [str(entrypoint), "--help"],
        cwd=final,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_run.returncode == 0, help_run.stderr

    assert entrypoint.read_text(encoding="utf-8").splitlines()[0] == f"#!{runtime_python}"
    for launcher in (final / "venv" / "bin").iterdir():
        if launcher.is_file() and not launcher.is_symlink():
            first_line = launcher.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
            assert str(staging) not in first_line


def test_remote_url_is_passed_as_data_not_python_source(tmp_path):
    marker = tmp_path / "injected"
    payload = (
        "github.com/foo/bar.git'); "
        f"__import__('pathlib').Path('{marker}').write_text('INJECTED'); #"
    )
    code = """
import sys
from btcquant.deployment import validate_canonical_repository
validate_canonical_repository(sys.argv[1], sys.argv[2])
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            payload,
            "github.com/Julianos87/btc-quant.git",
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not marker.exists()

    for name in ("update.sh", "install.sh", "create-release.sh", "migrate.sh"):
        script = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "validate_canonical_repository('${REMOTE_URL}'" not in script
        assert 'validate_canonical_repository("${REMOTE_URL}"' not in script
