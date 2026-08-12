"""Régressions de sécurité qui ne nécessitent ni réseau ni systemd."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

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
    assert "sync --frozen" in release
    assert "release-manifest.json" in release
    assert '[ "${#RELEASE_ID}" -ne 40 ]' in release
    assert 'chown -R root:btcquant "${STAGING}"' in release
    assert 'chown -R root:root "${STAGING}"' not in release
    assert '"1s|^#!${STAGING}/venv/bin/python|#!${TARGET}/venv/bin/python|"' in release
    assert 'grep -RIl "^#!${STAGING}/" "${STAGING}/venv/bin"' in release


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
