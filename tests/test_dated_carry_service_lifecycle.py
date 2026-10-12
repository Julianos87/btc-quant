from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UNITS = (
    "btcquant-carry-dated-collector.service",
    "btcquant-carry-dated-experimental.service",
)


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | 0o755)


def test_sync_starts_only_isolated_paper_units_and_restarts_active_code(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    (release / "deploy").mkdir(parents=True)
    (release / "environments" / "paper").mkdir(parents=True)
    (release / "venv" / "bin").mkdir(parents=True)
    for unit in UNITS:
        shutil.copyfile(ROOT / "deploy" / unit, release / "deploy" / unit)
    shutil.copyfile(
        ROOT / "environments" / "paper" / "carry_dated_experimental.yaml",
        release / "environments" / "paper" / "carry_dated_experimental.yaml",
    )
    (release / "venv" / "bin" / "python").symlink_to(sys.executable)
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    for unit in UNITS:
        shutil.copyfile(release / "deploy" / unit, unit_dir / unit)
    state = tmp_path / "systemctl-state"
    state.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    _executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        '  enable|restart) action="$1"; shift; for unit in "$@"; do\n'
        f'    if [ "$action" = restart ]; then touch "{state}/$unit"; fi;\n'
        "  done ;;\n"
        '  is-active) [ -f "' + str(state) + '/$3" ] ;;\n'
        '  disable) shift; [ "${1:-}" = --now ] && shift; for unit in "$@"; do rm -f "'
        + str(state)
        + '/$unit"; done ;;\n'
        "  daemon-reload) ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "BTCQUANT_SYSTEMD_UNIT_DIR": str(unit_dir),
        }
    )
    helper = ROOT / "deploy" / "sync-dated-carry-paper.sh"

    started = subprocess.run(
        ["bash", str(helper), str(release), "true"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    commands = log.read_text(encoding="utf-8").splitlines()
    assert commands[:3] == [
        f"enable {' '.join(UNITS)}",
        f"restart {' '.join(UNITS)}",
        f"is-active --quiet {UNITS[0]}",
    ]
    assert (state / UNITS[0]).is_file()
    assert (state / UNITS[1]).is_file()

    log.write_text("", encoding="utf-8")
    restarted = subprocess.run(
        ["bash", str(helper), str(release), "false"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert restarted.returncode == 0, restarted.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"is-active --quiet {UNITS[0]}",
        f"restart {UNITS[0]}",
        f"is-active --quiet {UNITS[1]}",
        f"restart {UNITS[1]}",
    ]

    collector = release / "deploy" / UNITS[0]
    collector.write_text(
        collector.read_text(encoding="utf-8").replace(
            "--min-free-bytes 10737418240", "--min-free-bytes 9663676416"
        ),
        encoding="utf-8",
    )
    log.write_text("", encoding="utf-8")
    refused = subprocess.run(
        ["bash", str(helper), str(release), "true"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "mismatched collector/profile minimum free-space limits" in refused.stderr
    assert not log.read_text(encoding="utf-8")

    profile_path = release / "environments" / "paper" / "carry_dated_experimental.yaml"
    profile_source = (ROOT / "environments" / "paper" / "carry_dated_experimental.yaml").read_text(
        encoding="utf-8"
    )
    collector_source = (ROOT / "deploy" / UNITS[0]).read_text(encoding="utf-8")

    for profile_key, unit_flag, valid_value, invalid_value, error in (
        (
            "market_evidence_min_free_bytes",
            "min-free-bytes",
            "10737418240",
            "2147483648",
            "free-space floor below 10 GiB",
        ),
        (
            "market_evidence_quota_bytes",
            "max-total-bytes",
            "5368709120",
            "4294967296",
            "quota other than the v2 5 GiB budget",
        ),
    ):
        profile_path.write_text(
            profile_source.replace(
                f"{profile_key}: {valid_value}",
                f"{profile_key}: {invalid_value}",
            ),
            encoding="utf-8",
        )
        collector.write_text(
            collector_source.replace(
                f"--{unit_flag} {valid_value}",
                f"--{unit_flag} {invalid_value}",
            ),
            encoding="utf-8",
        )
        log.write_text("", encoding="utf-8")
        refused = subprocess.run(
            ["bash", str(helper), str(release), "true"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert refused.returncode != 0
        assert error in refused.stderr
        assert not log.read_text(encoding="utf-8")


def test_update_syncs_target_services_without_restarting_canonical_engines() -> None:
    update = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")
    assert "--dated-carry-paper" in update
    assert 'sync_dated_carry_services "${TARGET}" true' in update
    assert "systemctl restart btcquant-trend btcquant-carry" in update
    assert "if ${RESTART_ENGINES}; then" in update
    migration = update.index('bash "${TARGET}/deploy/migrate.sh"')
    install_target = update.rfind('install_units "${TARGET}"', 0, migration)
    stop = update.rfind("stop_all_writer_processes", 0, install_target)
    assert stop < install_target < migration


def test_collector_unit_uses_runner_books_and_consistent_storage_quota() -> None:
    unit = (ROOT / "deploy" / "btcquant-carry-dated-collector.service").read_text(encoding="utf-8")
    profile = (ROOT / "environments" / "paper" / "carry_dated_experimental.yaml").read_text(
        encoding="utf-8"
    )
    assert "--books-from-runner" in unit
    assert "--max-total-bytes 5368709120" in unit
    assert "--min-free-bytes 10737418240" in unit
    assert "market_evidence_quota_bytes: 5368709120" in profile
    assert "market_evidence_min_free_bytes: 10737418240" in profile
