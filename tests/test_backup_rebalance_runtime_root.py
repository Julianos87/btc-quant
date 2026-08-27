"""Backup APP_ROOT vs RUNTIME_ROOT and rebalance wrapper runtime root."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from btcquant.backup import assert_writer_recovery_clear
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore

ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | 0o755)


def _runtime_layout(tmp_path: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "opt" / "btcquant"
    release = runtime / "releases" / ("b" * 40)
    state = runtime / "state"
    state.mkdir(parents=True)
    (runtime / "backups").mkdir()
    release.mkdir(parents=True)
    (release / "scripts").mkdir()
    (release / "venv" / "bin").mkdir(parents=True)
    (release / "state").symlink_to(Path("../../state"))
    (release / "backups").symlink_to(Path("../../backups"))
    (release / "backups-repo").symlink_to(Path("../../backups-repo"))
    (runtime / "current").symlink_to(release)
    for name in ("backup_state.sh", "backup_database.py"):
        (release / "scripts" / name).write_text(
            (ROOT / "scripts" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        if name.endswith(".sh"):
            (release / "scripts" / name).chmod(0o755)
    return runtime, release


def _seed_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('runtime-real')")
        connection.execute("PRAGMA integrity_check")


def test_backup_refuses_missing_btcquant_root(tmp_path: Path) -> None:
    runtime, release = _runtime_layout(tmp_path)
    _seed_db(runtime / "state" / "btcquant.db")
    env = os.environ.copy()
    env["BACKUP_ENCRYPTION_KEY"] = "fixture-key"
    env.pop("BTCQUANT_ROOT", None)
    result = subprocess.run(
        ["bash", str(release / "scripts" / "backup_state.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "BTCQUANT_ROOT is required" in result.stderr
    assert not list((runtime / "backups").glob("*"))


def test_backup_uses_real_runtime_state_and_release_venv(tmp_path: Path) -> None:
    runtime, release = _runtime_layout(tmp_path)
    _seed_db(runtime / "state" / "btcquant.db")
    log = tmp_path / "python-argv.log"
    _write_executable(
        release / "venv" / "bin" / "python",
        f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "{log}"\nexec /usr/bin/python3 "$@"\n',
    )
    env = os.environ.copy()
    env["BACKUP_ENCRYPTION_KEY"] = "fixture-key-runtime"
    env["BTCQUANT_ROOT"] = str(runtime)
    result = subprocess.run(
        ["bash", str(release / "scripts" / "backup_state.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    argv = log.read_text(encoding="utf-8")
    source = str(runtime / "state" / "btcquant.db")
    assert source in argv
    assert "/scripts/backup_database.py" in argv
    assert str(release / "scripts" / "backup_database.py") in argv
    assert str(release / "state" / "btcquant.db") not in argv.split()
    archives = list((runtime / "backups").glob("state-*.tar.gz.enc"))
    assert len(archives) == 1
    assert not list((runtime / "backups").glob("state-*.tar.gz"))
    assert (runtime / "state" / "btcquant.db").is_file()
    assert not (runtime / "state" / "btcquant.db").is_symlink()
    assert (release / "state").is_symlink()


def test_release_state_symlink_source_is_rejected(tmp_path: Path) -> None:
    runtime, release = _runtime_layout(tmp_path)
    _seed_db(runtime / "state" / "btcquant.db")
    linked = release / "state" / "btcquant.db"
    assert linked.exists()
    dest = tmp_path / "out.db"
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "backup_database.py"),
            str(linked),
            str(dest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "symlink" in result.stderr.lower()
    assert not dest.exists()


def test_real_runtime_state_source_backup_succeeds(tmp_path: Path) -> None:
    runtime, _release = _runtime_layout(tmp_path)
    source = runtime / "state" / "btcquant.db"
    _seed_db(source)
    dest = tmp_path / "copy.db"
    subprocess.run(
        ["python3", str(ROOT / "scripts" / "backup_database.py"), str(source), str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(dest) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "runtime-real"


def test_backup_encrypted_roundtrip_from_runtime_root(tmp_path: Path) -> None:
    runtime, release = _runtime_layout(tmp_path)
    _seed_db(runtime / "state" / "btcquant.db")
    env = os.environ.copy()
    env["BACKUP_ENCRYPTION_KEY"] = "roundtrip-key"
    env["BTCQUANT_ROOT"] = str(runtime)
    result = subprocess.run(
        ["bash", str(release / "scripts" / "backup_state.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    archive = next((runtime / "backups").glob("*.tar.gz.enc"))
    plain = tmp_path / "roundtrip.tar.gz"
    subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-in",
            str(archive),
            "-out",
            str(plain),
            "-pass",
            "env:BACKUP_ENCRYPTION_KEY",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    extract = tmp_path / "extract"
    extract.mkdir()
    with tarfile.open(plain, "r:gz") as tar:
        tar.extractall(extract)
    restored = extract / "state" / "btcquant.db"
    with sqlite3.connect(restored) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "runtime-real"


def test_rebalance_wrapper_pending_and_monthly_pass_runtime_root(tmp_path: Path) -> None:
    wrapper = (ROOT / "deploy" / "rebalance-root.sh").read_text(encoding="utf-8")
    assert 'BTCQUANT_ROOT="${CURRENT}"' not in wrapper
    script = tmp_path / "rebalance-root.sh"
    script.write_text(wrapper, encoding="utf-8")
    script.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "env.log"
    _write_executable(
        fake_bin / "runuser",
        "#!/usr/bin/env bash\n"
        "shift\n"  # -u
        "shift\n"  # btcquant
        "shift\n"  # --
        f'printf \'%s\\n\' "$*" >> "{log}"\n'
        'if [[ "$*" == *--check-pending* ]]; then exit 3; fi\n'
        "exit 0\n",
    )
    _write_executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        fake_bin / "sleep",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["MONTHLY_DEPOSIT"] = "0"

    pending = subprocess.run(
        ["bash", str(script), "--pending-only"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
    )
    assert pending.returncode == 0, pending.stderr + pending.stdout
    monthly = subprocess.run(
        ["bash", str(script), "monthly"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
    )
    assert monthly.returncode == 0, monthly.stderr + monthly.stdout
    recorded = log.read_text(encoding="utf-8")
    assert "BTCQUANT_ROOT=/opt/btcquant" in recorded
    assert "BTCQUANT_ROOT=/opt/btcquant/current" not in recorded
    assert (
        recorded.count("BTCQUANT_ROOT=/opt/btcquant ")
        + recorded.count("BTCQUANT_ROOT=/opt/btcquant\n")
        >= 1
    )
    assert "/opt/btcquant/current/venv/bin/btcquant-rebalance" in recorded
    assert "--check-pending" in recorded
    assert "--apply" in recorded


def test_rebalance_pending_uses_real_runtime_state_not_release_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from btcquant.entrypoints import rebalance as reb

    runtime = tmp_path / "opt" / "btcquant"
    release = runtime / "releases" / ("c" * 40)
    state = runtime / "state"
    state.mkdir(parents=True)
    release.mkdir(parents=True)
    (release / "state").symlink_to(Path("../../state"))
    store = StateStore(state / "btcquant.db")
    store.save_engine_state(
        "trend",
        {
            "slots": {
                "trend_ls_20": {
                    "cash": 1900.0,
                    "position": {
                        "entry_time": "2026-08-20 12:00:23.176115+00:00",
                        "entry_price": 72000.0,
                        "qty": 0.03,
                        "stop_price": 72606.22287629344,
                        "direction": 1,
                        "bars_held": 5,
                        "best_close": 76000.0,
                    },
                    "stop_order_id": None,
                    "entry_fee": 1.0,
                    "last_bar_ts": "2026-08-21 04:00:00+00:00",
                }
            },
            "peak_equity": 2000.0,
            "halted": False,
            "day": "2026-08-21",
            "day_start_equity": 1900.0,
            "daily_lockout": False,
            "reconciliation_required": False,
            "last_funding_ts": None,
        },
    )
    store.save_engine_state(
        "carry",
        {
            "equity": 3972.78,
            "in_position": True,
            "execution_state": "OPEN",
            "qty": 0.15,
            "spot_qty": 0.0,
            "perp_qty": 0.15,
            "last_funding_ts": None,
            "peak_equity": 4000.0,
            "day": "2026-08-21",
            "day_start_equity": 3972.78,
            "halted": False,
            "daily_lockout": False,
            "accounting_uncertain": False,
        },
    )
    monkeypatch.setenv("BTCQUANT_ROOT", str(runtime))
    monkeypatch.chdir(ROOT)
    import importlib

    importlib.reload(reb)
    monkeypatch.setattr(reb, "notify", lambda _message: False)
    monkeypatch.setattr(
        "sys.argv",
        ["btcquant-rebalance", "--check-pending"],
    )
    assert_writer_recovery_clear(state)
    with pytest.raises(SystemExit) as stopped:
        reb.main()
    assert stopped.value.code == 3
    assert reb.STATE == state
    assert not reb.STATE.is_symlink()
    before = (state / "btcquant.db").read_bytes()
    assert (state / "btcquant.db").read_bytes() == before
    importlib.reload(reb)


def test_rebalance_apply_on_flat_uses_runtime_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from btcquant.entrypoints import rebalance as reb

    runtime = tmp_path / "opt" / "btcquant"
    state = runtime / "state"
    state.mkdir(parents=True)
    store = StateStore(state / "btcquant.db")
    store.save_engine_state(
        "trend",
        {
            "slots": {
                f"trend_ls_{n}": {
                    "cash": 2000.0,
                    "position": None,
                    "stop_order_id": None,
                    "entry_fee": 0.0,
                    "last_bar_ts": None,
                }
                for n in (20, 55, 100)
            },
            "peak_equity": 6000.0,
            "halted": False,
            "day": "2026-08-21",
            "day_start_equity": 6000.0,
            "daily_lockout": False,
            "reconciliation_required": False,
            "last_funding_ts": None,
        },
    )
    store.save_engine_state(
        "carry",
        {
            "equity": 4000.0,
            "in_position": False,
            "execution_state": "FLAT",
            "qty": 0.0,
            "spot_qty": 0.0,
            "perp_qty": 0.0,
            "last_funding_ts": None,
            "peak_equity": 4000.0,
            "day": "2026-08-21",
            "day_start_equity": 4000.0,
            "halted": False,
            "daily_lockout": False,
            "accounting_uncertain": False,
        },
    )
    monkeypatch.setenv("BTCQUANT_ROOT", str(runtime))
    monkeypatch.chdir(ROOT)
    import importlib

    importlib.reload(reb)
    monkeypatch.setattr(reb, "notify", lambda _message: False)
    monkeypatch.setattr("sys.argv", ["btcquant-rebalance", "--apply"])
    reb.main()
    restored = StateStore(state / "btcquant.db", initialize=False, read_only=True)
    trend = restored.load_engine_state("trend")
    carry = restored.load_engine_state("carry")
    assert trend is not None and carry is not None
    assert trend["slots"]["trend_ls_20"]["position"] is None
    assert carry["in_position"] is False
    assert SCHEMA_VERSION == 7
    importlib.reload(reb)


def test_schema_stays_seven() -> None:
    assert SCHEMA_VERSION == 7
