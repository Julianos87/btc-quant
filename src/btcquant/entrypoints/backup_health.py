"""Check backup freshness and archive-bound verification evidence."""

from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from btcquant.backup_verification import read_backup_verification_record

MAX_AGE_SECONDS = 36 * 60 * 60


def _backup_timer_active(runner: Callable[..., subprocess.CompletedProcess[str]]) -> bool:
    try:
        result = runner(
            ["systemctl", "is-active", "--quiet", "btcquant-backup.timer"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def assess_backup_health(
    root: Path,
    *,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str]:
    """Return health for the newest encrypted archive without writing state."""

    if not _backup_timer_active(runner):
        return False, "backup timer is inactive or unavailable"
    backup_dir = root / "backups"
    archives = (
        sorted(
            backup_dir.glob("state-*.tar.gz.enc"),
            key=lambda item: item.stat().st_mtime,
        )
        if backup_dir.is_dir()
        else []
    )
    if not archives:
        return False, "no encrypted backup is available"

    latest = archives[-1]
    archive_delta = (now or datetime.now(UTC)).timestamp() - latest.stat().st_mtime
    if archive_delta < -300:
        return False, "latest backup timestamp is in the future"
    age_seconds = max(0.0, archive_delta)
    if age_seconds > MAX_AGE_SECONDS:
        return False, f"latest backup is {int(age_seconds // 3600)} h old (limit 36 h)"
    evidence = read_backup_verification_record(latest, now=now)
    if evidence.get("status") != "PASS":
        reason = str(evidence.get("reason") or "verification evidence unavailable")
        return False, f"latest backup verification is not valid ({reason})"
    return True, "Backup health OK"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BTCQUANT_ROOT", "/opt/btcquant")),
    )
    args = parser.parse_args()
    healthy, message = assess_backup_health(args.root)
    print(message)
    if not healthy:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
