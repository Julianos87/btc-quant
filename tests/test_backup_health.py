from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from btcquant.backup_verification import verification_path
from btcquant.entrypoints.backup_health import assess_backup_health


def _active_timer(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["systemctl"], returncode=0, stdout="", stderr="")


def _write_backup_verification_fixture(archive: Path) -> None:
    payload = {
        "archive_name": archive.name,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_size_bytes": archive.stat().st_size,
        "format_version": 1,
        "kind": "BTCQUANT_BACKUP_VERIFICATION",
        "status": "PASS",
        "verified_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "verification_checks": {
            "encrypted_round_trip": "PASS",
            "archive_read": "PASS",
            "sqlite_snapshots": "PASS",
        },
        "verification_scope": "encrypted_archive_and_sqlite_snapshot_integrity",
    }
    verification_path(archive).write_text(json.dumps(payload), encoding="utf-8")


def test_backup_health_requires_fresh_archive_and_matching_integrity_evidence(
    tmp_path: Path,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    archive = backups / "state-20261013-0300.tar.gz.enc"
    archive.write_bytes(b"verified bytes")
    _write_backup_verification_fixture(archive)

    healthy, message = assess_backup_health(
        tmp_path,
        now=datetime.now(UTC),
        runner=_active_timer,
    )

    assert healthy is True
    assert message == "Backup health OK"


def test_backup_health_fails_closed_when_archive_changes(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    archive = backups / "state-20261013-0300.tar.gz.enc"
    archive.write_bytes(b"verified bytes")
    _write_backup_verification_fixture(archive)
    archive.write_bytes(b"changed bytes")

    healthy, message = assess_backup_health(
        tmp_path,
        now=datetime.now(UTC),
        runner=_active_timer,
    )

    assert healthy is False
    assert "verification_record_does_not_match_archive" in message


def test_backup_health_fails_when_scheduler_is_inactive(tmp_path: Path) -> None:
    def inactive_timer(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["systemctl"], returncode=3, stdout="", stderr="")

    healthy, message = assess_backup_health(
        tmp_path,
        now=datetime.now(UTC),
        runner=inactive_timer,
    )

    assert healthy is False
    assert "timer is inactive" in message


def test_backup_health_rejects_future_dated_archive(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    archive = backups / "state-20261013-0300.tar.gz.enc"
    archive.write_bytes(b"verified bytes")
    _write_backup_verification_fixture(archive)
    future = datetime.now(UTC).timestamp() + 600
    os.utime(archive, (future, future))

    healthy, message = assess_backup_health(
        tmp_path,
        now=datetime.now(UTC),
        runner=_active_timer,
    )

    assert healthy is False
    assert "timestamp is in the future" in message
