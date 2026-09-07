from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_unit_uses_only_the_independent_release_pointer() -> None:
    service = (ROOT / "deploy" / "btcquant-dashboard.service").read_text(encoding="utf-8")

    assert "WorkingDirectory=/opt/btcquant/dashboard-current" in service
    assert "ExecStart=/opt/btcquant/dashboard-current/venv/bin/gunicorn" in service
    assert "ExecStart=/opt/btcquant/current/venv/bin/gunicorn" not in service
    assert "127.0.0.1:8666" in service


def test_dashboard_release_switch_is_exact_sha_and_does_not_switch_trading_links() -> None:
    helper = (ROOT / "deploy" / "switch-dashboard-release.sh").read_text(encoding="utf-8")

    assert "DASHBOARD_CURRENT" in helper
    assert "DASHBOARD_PREVIOUS" in helper
    assert '"${ROOT}"/releases/*' in helper
    assert "^[0-9a-f]{40}$" in helper
    assert "release-manifest.json" in helper
    assert "mv -Tf" in helper
    assert "flock -n 9" in helper
    assert '[ -e "${DASHBOARD_CURRENT}" ] || [ -L "${DASHBOARD_CURRENT}" ]' in helper
    assert '"${ROOT}/current"' not in helper
    assert '"${ROOT}/previous"' not in helper
    assert "systemctl" not in helper


def test_deploy_flows_call_dashboard_switch_without_engine_switch() -> None:
    install = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    assert "switch-dashboard-release.sh" in install
    assert "switch_dashboard_release" in update
    assert "DASHBOARD_UNIT_SOURCE" in update
