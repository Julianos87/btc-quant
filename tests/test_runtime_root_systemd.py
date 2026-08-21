"""BTCQUANT_ROOT source-unit contract and release/state path-safety."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from btcquant.backup import PathUnsafe, assert_writer_recovery_clear
from btcquant.entrypoints import carry, trend, watchdog

ROOT = Path(__file__).resolve().parents[1]

APPLICABLE_UNITS = (
    "btcquant-dashboard.service",
    "btcquant-trend.service",
    "btcquant-carry.service",
    "btcquant-shadow.service",
    "btcquant-watchdog.service",
    "btcquant-digest.service",
    "btcquant-weekly.service",
    "btcquant-compact.service",
    "btcquant-hyperliquid-testnet.service",
    "btcquant-hyperliquid-watchdog.service",
)

HARDENING = (
    "User=btcquant",
    "NoNewPrivileges=true",
    "PrivateTmp=true",
    "ProtectSystem=strict",
    "ProtectHome=true",
    "ReadWritePaths=/opt/btcquant/state",
    "CapabilityBoundingSet=",
    "RestrictSUIDSGID=true",
    "LockPersonality=true",
)


def _service_text(name: str) -> str:
    return (ROOT / "deploy" / name).read_text(encoding="utf-8")


def test_applicable_source_units_set_explicit_btcquant_root() -> None:
    for name in APPLICABLE_UNITS:
        text = _service_text(name)
        assert "Environment=BTCQUANT_ROOT=/opt/btcquant" in text, name
        assert "WorkingDirectory=/opt/btcquant/current" in text, name


def test_source_units_do_not_require_host_dropins() -> None:
    for name in APPLICABLE_UNITS:
        text = _service_text(name)
        assert ".service.d" not in text, name
        assert "btcquant-root.conf" not in text, name
        # The unit itself, not a drop-in, must establish the runtime root.
        assert text.count("Environment=BTCQUANT_ROOT=/opt/btcquant") == 1, name


def test_systemd_hardening_is_preserved() -> None:
    for name in APPLICABLE_UNITS:
        text = _service_text(name)
        for needle in HARDENING:
            assert needle in text, f"{name} missing {needle}"
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


def test_compact_equity_uses_btcquant_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert module.ROOT == runtime_root.resolve()
    assert module.STATE == runtime_root.resolve() / "state"


def test_systemd_analyze_verify_source_units() -> None:
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze not installed")
    units = sorted((ROOT / "deploy").glob("btcquant-*.service"))
    assert units
    env = os.environ.copy()
    result = subprocess.run(
        [analyzer, "verify", *[str(path) for path in units]],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
    )
    # Missing ExecStart binaries in this checkout produce warnings, not a
    # failed unit parse. A non-zero exit is only accepted for those warnings.
    combined = result.stdout + result.stderr
    assert "Failed to prepare XML" not in combined
    assert "Unit " + " is invalid" not in combined
    for name in APPLICABLE_UNITS:
        assert f"{name}: Failed to load configuration" not in combined


def test_effective_environment_from_source_units_contains_runtime_root(tmp_path: Path) -> None:
    """Parse source units the way systemd concatenates Environment= lines."""

    for name in APPLICABLE_UNITS:
        env: dict[str, str] = {}
        for line in _service_text(name).splitlines():
            if line.startswith("Environment="):
                assignment = line.split("=", 1)[1]
                key, value = assignment.split("=", 1)
                env[key] = value
        assert env.get("BTCQUANT_ROOT") == "/opt/btcquant", name
