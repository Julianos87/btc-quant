"""BTCQUANT_ROOT source-unit contract, exhaustive inventory, path-safety."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from btcquant.backup import PathUnsafe, assert_writer_recovery_clear
from btcquant.entrypoints import carry, trend, watchdog
from btcquant.execution.state_store import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]

# Every deploy/btcquant-*.service MUST appear here. A new runtime consumer
# omitted from this inventory fails the audit instead of silently skipping.
CLASS_A_PYTHON_RUNTIME = "A_python_runtime"
CLASS_B_WRAPPER = "B_wrapper"
CLASS_C_NO_RUNTIME_ROOT = "C_no_runtime_root"

SERVICE_CLASSIFICATION: dict[str, str] = {
    "btcquant-dashboard.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-trend.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-carry.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-shadow.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-watchdog.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-digest.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-weekly.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-compact.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-hyperliquid-testnet.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-hyperliquid-watchdog.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-backup.service": CLASS_A_PYTHON_RUNTIME,
    "btcquant-rebalance.service": CLASS_B_WRAPPER,
    "btcquant-rebalance-pending.service": CLASS_B_WRAPPER,
}

A_PYTHON_HARDENING = (
    "User=btcquant",
    "NoNewPrivileges=true",
    "PrivateTmp=true",
    "ProtectSystem=strict",
    "ReadWritePaths=/opt/btcquant/state",
    "CapabilityBoundingSet=",
    "RestrictSUIDSGID=true",
    "LockPersonality=true",
)


def _service_text(name: str) -> str:
    return (ROOT / "deploy" / name).read_text(encoding="utf-8")


def _deployed_services() -> list[str]:
    return sorted(path.name for path in (ROOT / "deploy").glob("btcquant-*.service"))


def test_exhaustive_service_inventory_has_zero_unclassified_units() -> None:
    deployed = _deployed_services()
    classified = set(SERVICE_CLASSIFICATION)
    assert set(deployed) == classified
    assert not (set(deployed) - classified)
    assert not (classified - set(deployed))
    assert set(SERVICE_CLASSIFICATION.values()) <= {
        CLASS_A_PYTHON_RUNTIME,
        CLASS_B_WRAPPER,
        CLASS_C_NO_RUNTIME_ROOT,
    }


def test_class_a_units_set_explicit_btcquant_root() -> None:
    for name, kind in SERVICE_CLASSIFICATION.items():
        if kind != CLASS_A_PYTHON_RUNTIME:
            continue
        text = _service_text(name)
        assert "Environment=BTCQUANT_ROOT=/opt/btcquant" in text, name
        assert text.count("Environment=BTCQUANT_ROOT=/opt/btcquant") == 1, name
        assert ".service.d" not in text, name
        assert "btcquant-root.conf" not in text, name


def test_class_b_wrappers_delegate_runtime_root_to_rebalance_root() -> None:
    wrapper = (ROOT / "deploy" / "rebalance-root.sh").read_text(encoding="utf-8")
    assert "ROOT=/opt/btcquant" in wrapper
    assert 'BTCQUANT_ROOT="${CURRENT}"' not in wrapper
    assert wrapper.count('BTCQUANT_ROOT="${ROOT}"') == 2
    assert '"${CURRENT}/venv/bin/btcquant-rebalance"' in wrapper
    for name, kind in SERVICE_CLASSIFICATION.items():
        if kind != CLASS_B_WRAPPER:
            continue
        text = _service_text(name)
        assert "ExecStart=/usr/local/libexec/btcquant-rebalance" in text, name
        assert "User=root" in text, name


def test_backup_unit_keeps_protecthome_read_only() -> None:
    text = _service_text("btcquant-backup.service")
    assert "ProtectHome=read-only" in text
    assert "ProtectHome=true" not in text
    assert "Environment=BTCQUANT_ROOT=/opt/btcquant" in text
    assert (
        "ReadWritePaths=/opt/btcquant/state /opt/btcquant/backups -/opt/btcquant/backups-repo"
        in text
    )


def test_systemd_hardening_is_preserved() -> None:
    for name, kind in SERVICE_CLASSIFICATION.items():
        text = _service_text(name)
        if kind == CLASS_B_WRAPPER:
            assert "NoNewPrivileges=true" in text
            assert "ProtectSystem=strict" in text
            assert "ProtectHome=true" in text
            continue
        for needle in A_PYTHON_HARDENING:
            assert needle in text, f"{name} missing {needle}"
        if name == "btcquant-backup.service":
            assert "ProtectHome=read-only" in text
        else:
            assert "ProtectHome=true" in text, name
        assert "User=root" not in text, name


def test_runtime_root_resolves_opt_btcquant_not_physical_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "opt" / "btcquant"
    release = runtime_root / "releases" / ("a" * 40)
    state = runtime_root / "state"
    state.mkdir(parents=True)
    release.mkdir(parents=True)
    (release / "state").symlink_to(Path("../../state"))
    current = runtime_root / "current"
    current.symlink_to(release)

    monkeypatch.delenv("BTCQUANT_ROOT", raising=False)
    monkeypatch.chdir(release)
    importlib.reload(trend)
    assert trend.ROOT == release.resolve()
    assert (trend.ROOT / "state").is_symlink()

    monkeypatch.setenv("BTCQUANT_ROOT", str(runtime_root))
    importlib.reload(trend)
    importlib.reload(carry)
    importlib.reload(watchdog)
    try:
        assert trend.ROOT == runtime_root.resolve()
        assert carry.ROOT == runtime_root.resolve()
        assert watchdog.ROOT == runtime_root.resolve()
        assert watchdog.STATE == (runtime_root / "state").resolve()
        assert not watchdog.STATE.is_symlink()
    finally:
        monkeypatch.delenv("BTCQUANT_ROOT", raising=False)
        monkeypatch.chdir(ROOT)
        importlib.reload(trend)
        importlib.reload(carry)
        importlib.reload(watchdog)


def test_path_safety_fails_through_release_state_symlink(tmp_path: Path) -> None:
    runtime_root = tmp_path / "opt" / "btcquant"
    release = runtime_root / "releases" / ("a" * 40)
    state = runtime_root / "state"
    state.mkdir(parents=True)
    release.mkdir(parents=True)
    linked = release / "state"
    linked.symlink_to(Path("../../state"))
    assert linked.is_symlink()
    with pytest.raises(PathUnsafe, match="symlink in controlled path"):
        assert_writer_recovery_clear(linked)


def test_path_safety_and_recovery_pass_with_explicit_root(tmp_path: Path) -> None:
    runtime_root = tmp_path / "opt" / "btcquant"
    state = runtime_root / "state"
    state.mkdir(parents=True)
    assert_writer_recovery_clear(state)
    assert state.resolve() == (runtime_root / "state").resolve()
    assert not state.is_symlink()


def test_compact_equity_separates_app_root_from_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    runtime_root = tmp_path / "opt" / "btcquant"
    runtime_root.mkdir(parents=True)
    monkeypatch.setenv("BTCQUANT_ROOT", str(runtime_root))
    spec = importlib.util.spec_from_file_location(
        "compact_equity_under_test",
        ROOT / "scripts" / "compact_equity.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.APP_ROOT == ROOT
    assert module.RUNTIME_ROOT == runtime_root.resolve()
    assert module.ROOT == runtime_root.resolve()
    assert module.STATE == runtime_root.resolve() / "state"
    assert module.APP_ROOT != module.RUNTIME_ROOT


def test_systemd_analyze_verify_source_units() -> None:
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze not installed")
    units = sorted((ROOT / "deploy").glob("btcquant-*.service"))
    assert units
    result = subprocess.run(
        [analyzer, "verify", *[str(path) for path in units]],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        cwd=str(ROOT),
    )
    combined = result.stdout + result.stderr
    assert "Failed to prepare XML" not in combined
    assert "Unit " + " is invalid" not in combined
    for name in SERVICE_CLASSIFICATION:
        assert f"{name}: Failed to load configuration" not in combined


def test_effective_environment_from_source_units_contains_runtime_root() -> None:
    for name, kind in SERVICE_CLASSIFICATION.items():
        if kind != CLASS_A_PYTHON_RUNTIME:
            continue
        env: dict[str, str] = {}
        for line in _service_text(name).splitlines():
            if line.startswith("Environment="):
                assignment = line.split("=", 1)[1]
                key, value = assignment.split("=", 1)
                env[key] = value
        assert env.get("BTCQUANT_ROOT") == "/opt/btcquant", name


def test_schema_version_is_unchanged() -> None:
    assert SCHEMA_VERSION == 8
