"""Release venv launcher relocation, post-move smoke, reuse and quarantine."""

from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_relocator():
    spec = importlib.util.spec_from_file_location(
        "relocate_venv_launchers",
        ROOT / "scripts" / "relocate_venv_launchers.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relocator = _load_relocator()


def _write_executable(path: Path, content: str | bytes, *, mode: int = 0o755) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    path.chmod(mode)


def _venv_bin(prefix: Path) -> Path:
    bindir = prefix / "venv" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    return bindir


def _plant_python(bindir: Path, interpreter: str) -> None:
    real = Path(sys.executable).resolve()
    target = bindir / interpreter
    if not target.exists():
        target.symlink_to(real)
    python = bindir / "python"
    if interpreter != "python" and not python.exists():
        python.symlink_to(target.name)


def _python_launcher(prefix: Path, interpreter: str, name: str, script: str) -> Path:
    bindir = _venv_bin(prefix)
    _plant_python(bindir, interpreter)
    path = bindir / name
    _write_executable(
        path,
        f"#!{prefix.as_posix()}/venv/bin/{interpreter}\n{script}",
    )
    return path


HELP_SCRIPT = "import sys\nprint('usage: ok')\nsys.exit(0)\n"
GUNICORN_SCRIPT = "import sys\nprint('gunicorn (version 26.0.0)')\nsys.exit(0)\n"


@pytest.mark.parametrize("interpreter", ["python", "python3", "python3.12"])
def test_rewrites_venv_python_interpreter_variants(tmp_path: Path, interpreter: str) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    launcher = _python_launcher(staging, interpreter, "gunicorn", GUNICORN_SCRIPT)
    relocator.rewrite_launchers(staging / "venv", staging, final)
    first = launcher.read_bytes().splitlines()[0].decode("ascii")
    assert first == f"#!{final.as_posix()}/venv/bin/{interpreter}"
    assert str(staging) not in first


def test_unrelated_shell_shebang_is_unchanged(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    bindir = _venv_bin(staging)
    shell = bindir / "wrapper"
    original = "#!/bin/sh\nexec true\n"
    _write_executable(shell, original)
    env_bash = bindir / "hook"
    _write_executable(env_bash, "#!/usr/bin/env bash\nexit 0\n")
    relocator.rewrite_launchers(staging / "venv", staging, final)
    assert shell.read_text(encoding="utf-8") == original
    assert env_bash.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")


def test_binary_file_is_not_byte_rewritten(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    bindir = _venv_bin(staging)
    payload = b"\x7fELF" + str(staging).encode() + b"\x00binary"
    binary = bindir / "python"
    _write_executable(binary, payload)
    relocator.rewrite_launchers(staging / "venv", staging, final)
    assert binary.read_bytes() == payload


def test_legacy_stale_assertion_missed_shebang_prefix() -> None:
    """The previous guard expected the first line to start with OLD_PREFIX.

    A real shebang starts with ``#!``, so ``#!<OLD>/venv/bin/python3`` escaped.
    """

    old_prefix = "/tmp/btcquant-staging"
    first_line = f"#!{old_prefix}/venv/bin/python3".encode()
    assert not first_line.startswith(old_prefix.encode())
    assert first_line.startswith(b"#!")
    assert old_prefix.encode() in first_line


def test_stale_old_prefix_after_rewrite_is_refused(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    leftover = _python_launcher(staging, "python3", "gunicorn", GUNICORN_SCRIPT)
    relocator.rewrite_launchers(staging / "venv", staging, final)
    leftover.write_text(
        f"#!{staging.as_posix()}/venv/bin/python3\n{GUNICORN_SCRIPT}",
        encoding="utf-8",
    )
    leftover.chmod(0o755)
    with pytest.raises(ValueError, match="still references staging"):
        relocator.assert_no_stale_prefix(staging / "venv", staging)


def test_post_move_gunicorn_and_console_launchers_are_executable(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "releases" / ("a" * 40)
    final.parent.mkdir(parents=True)
    for name, script in (
        ("gunicorn", GUNICORN_SCRIPT),
        ("btcquant-trend", HELP_SCRIPT),
        ("btcquant-carry", HELP_SCRIPT),
        ("btcquant-readiness", HELP_SCRIPT),
        ("btcquant-carry-cutover", HELP_SCRIPT),
        ("btcquant-shadow", HELP_SCRIPT),
    ):
        _python_launcher(staging, "python3.12", name, script)
    relocator.rewrite_launchers(staging / "venv", staging, final)
    staging.rename(final)
    relocator.assert_release_python_shebangs(final)
    relocator.smoke_exec_launchers(final)
    gunicorn = (final / "venv" / "bin" / "gunicorn").read_text(encoding="utf-8")
    assert gunicorn.splitlines()[0] == f"#!{final.as_posix()}/venv/bin/python3.12"
    assert str(staging) not in gunicorn


def test_invalid_new_target_is_quarantined_and_not_reported(tmp_path: Path) -> None:
    root = tmp_path / "opt" / "btcquant"
    releases = root / "releases"
    current = releases / ("b" * 40)
    target = releases / ("c" * 40)
    current.mkdir(parents=True)
    (current / "release-manifest.json").write_text("{}", encoding="utf-8")
    target.mkdir(parents=True)
    (target / "release-manifest.json").write_text("{}", encoding="utf-8")
    (root / "current").symlink_to(current)
    dest = relocator.quarantine_new_release(target, root)
    assert not target.exists()
    assert dest.is_dir()
    assert dest.name.startswith(f".{target.name}.invalid.")
    assert current.is_dir()
    assert (current / "release-manifest.json").is_file()
    assert (root / "current").resolve() == current.resolve()


def test_quarantine_never_deletes_current_or_previous(tmp_path: Path) -> None:
    root = tmp_path / "opt" / "btcquant"
    releases = root / "releases"
    current = releases / ("d" * 40)
    previous = releases / ("e" * 40)
    current.mkdir(parents=True)
    previous.mkdir(parents=True)
    (root / "current").symlink_to(current)
    (root / "previous").symlink_to(previous)
    with pytest.raises(ValueError, match="active current"):
        relocator.quarantine_new_release(current, root)
    with pytest.raises(ValueError, match="previous"):
        relocator.quarantine_new_release(previous, root)
    assert current.is_dir()
    assert previous.is_dir()


def test_invalid_existing_release_is_refused_not_mutated(tmp_path: Path) -> None:
    release = tmp_path / "releases" / ("f" * 40)
    bindir = _venv_bin(release)
    _plant_python(bindir, "python")
    stale = (
        f"#!/tmp/btcquant-pr25-release-9sTq/releases/{'f' * 40}/venv/bin/python\n{GUNICORN_SCRIPT}"
    )
    gunicorn = bindir / "gunicorn"
    _write_executable(gunicorn, stale)
    for name in (
        "btcquant-trend",
        "btcquant-carry",
        "btcquant-readiness",
        "btcquant-carry-cutover",
        "btcquant-shadow",
    ):
        _python_launcher(release, "python", name, HELP_SCRIPT)
    before = gunicorn.read_bytes()
    before_mode = gunicorn.stat().st_mode
    with pytest.raises(ValueError, match="does not target this release"):
        relocator.validate_existing_release_launchers(release)
    assert gunicorn.read_bytes() == before
    assert gunicorn.stat().st_mode == before_mode


def test_create_release_refuses_existing_invalid_and_protects_active() -> None:
    create = (ROOT / "deploy" / "create-release.sh").read_text(encoding="utf-8")
    assert "--validate-existing" in create
    assert "Never mutate" in create or "refus de réutilisation" in create
    assert "--quarantine-new" in create
    assert "current/previous" in create
    assert create.index('if [ -e "${TARGET}" ]') < create.index("--validate-existing")
    reuse = create.split('if [ -e "${TARGET}" ]', 1)[1].split("UV_BIN=", 1)[0]
    assert "rewrite_launchers" not in reuse
    assert "--old-prefix" not in reuse


def test_schema_version_is_unchanged() -> None:
    from btcquant.execution.state_store import SCHEMA_VERSION

    assert SCHEMA_VERSION == 9


def test_native_v8_trend_carry_restore_fixture_is_unchanged(tmp_path: Path) -> None:
    """Schema-8 OPEN Trend SOFTWARE + genuine Carry PAPER is loaded, not rewritten."""

    from btcquant.deployment import inspect_sqlite
    from btcquant.execution.state_contract import (
        STOP_PROTECTION_SOFTWARE,
        validate_trend_state,
    )
    from btcquant.execution.state_store import SCHEMA_VERSION, StateStore

    db = tmp_path / "state" / "btcquant.db"
    store = StateStore(db)
    trend_payload = {
        "slots": {
            "trend_ls_20": {
                "cash": 1948.15518412185,
                "position": {
                    "entry_time": "2026-08-20 12:00:23.176115+00:00",
                    "entry_price": 72096.75876036278,
                    "qty": 0.032482014269786356,
                    "stop_price": 72606.22287629344,
                    "direction": 1,
                    "bars_held": 4,
                    "best_close": 74516.0,
                    "initial_qty": 0.027100868637916053,
                    "last_add_price": 72660.32088085434,
                    "pyramid_adds": 1,
                },
                "stop_order_id": None,
                "stop_order_local_id": None,
                "stop_intent_id": None,
                "stop_transition": None,
                "entry_fee": 1.25,
                "last_bar_ts": "2026-08-21 00:00:00+00:00",
                "financial_transition_seq": 2,
            }
        },
        "peak_equity": 2000.0,
        "halted": False,
        "day": "2026-08-21",
        "day_start_equity": 1948.15518412185,
        "daily_lockout": False,
        "reconciliation_required": False,
        "last_funding_ts": None,
        "stop_protection_mode": STOP_PROTECTION_SOFTWARE,
    }
    carry_payload = {
        "equity": 3972.78,
        "in_position": True,
        "execution_state": "OPEN",
        "qty": 0.151,
        "spot_qty": 0.151,
        "perp_qty": -0.453,
        "last_funding_ts": None,
        "peak_equity": 4000.0,
        "day": "2026-08-21",
        "day_start_equity": 3972.78,
        "halted": False,
        "daily_lockout": False,
        "accounting_uncertain": False,
    }
    store.save_engine_state("trend", trend_payload)
    store.save_engine_state("carry", carry_payload)
    restored = StateStore(db, initialize=False, read_only=True)
    assert inspect_sqlite(db).metadata_schema_version == SCHEMA_VERSION == 9
    loaded_trend = restored.load_engine_state("trend")
    loaded_carry = restored.load_engine_state("carry")
    assert loaded_trend == trend_payload
    assert loaded_carry == carry_payload
    validate_trend_state(loaded_trend)
    assert loaded_trend["slots"]["trend_ls_20"]["position"]["stop_price"] == 72606.22287629344
    assert loaded_carry["execution_state"] == "OPEN"
    assert loaded_carry["accounting_uncertain"] is False
    assert loaded_trend["stop_protection_mode"] == STOP_PROTECTION_SOFTWARE


def test_create_release_failed_post_move_uses_cleanup_trap() -> None:
    create = (ROOT / "deploy" / "create-release.sh").read_text(encoding="utf-8")
    assert "NEW_RELEASE_CREATED=0" in create
    assert "NEW_RELEASE_CREATED=1" in create
    assert create.index('mv "${STAGING}" "${TARGET}"') < create.index("NEW_RELEASE_CREATED=1")
    assert create.index("NEW_RELEASE_CREATED=1") < create.index("--smoke")
    assert stat.S_IXUSR & (ROOT / "deploy" / "create-release.sh").stat().st_mode


def test_regular_file_without_execute_bit_is_ignored(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    bindir = _venv_bin(staging)
    note = bindir / "README"
    note.write_text(f"#!{staging}/venv/bin/python3\n", encoding="utf-8")
    relocator.rewrite_launchers(staging / "venv", staging, tmp_path / "final")
    assert str(staging) in note.read_text(encoding="utf-8")
