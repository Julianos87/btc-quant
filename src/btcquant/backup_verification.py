"""Durable, archive-bound evidence for the encrypted daily backup."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VERIFICATION_SUFFIX = ".verification.json"
VERIFICATION_KIND = "BTCQUANT_BACKUP_VERIFICATION"
VERIFICATION_FORMAT_VERSION = 1
_BACKUP_NAME = re.compile(r"state-[0-9]{8}-[0-9]{4}\.tar\.gz\.enc\Z")
_VERIFICATION_CHECKS = {
    "encrypted_round_trip": "PASS",
    "archive_read": "PASS",
    "sqlite_snapshots": "PASS",
}


def verification_path(archive: Path) -> Path:
    return archive.with_name(f"{archive.name}{VERIFICATION_SUFFIX}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_backup_verification_record(
    archive: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a sidecar against the current archive bytes without mutation."""

    record_path = verification_path(archive)
    if _BACKUP_NAME.fullmatch(archive.name) is None or archive.is_symlink():
        return {"status": "FAIL", "reason": "verification_path_is_invalid"}
    if not record_path.exists() and not record_path.is_symlink():
        return {"status": "UNKNOWN", "reason": "verification_record_missing"}
    if record_path.is_symlink():
        return {"status": "FAIL", "reason": "verification_path_is_symlink"}
    if not archive.is_file():
        return {"status": "FAIL", "reason": "verification_archive_invalid"}
    try:
        if record_path.stat().st_size > 16 * 1024:
            return {"status": "FAIL", "reason": "verification_record_too_large"}
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"status": "FAIL", "reason": "verification_record_invalid"}
        stat = archive.stat()
        checks = payload.get("verification_checks")
        verified_at_value = payload.get("verified_at_utc")
        if not isinstance(verified_at_value, str):
            return {"status": "FAIL", "reason": "verification_timestamp_invalid"}
        verified_at = datetime.fromisoformat(verified_at_value.replace("Z", "+00:00"))
        if verified_at.tzinfo is None:
            return {"status": "FAIL", "reason": "verification_timestamp_invalid"}
        current = now or datetime.now(UTC)
        current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
        if (verified_at.astimezone(UTC) - current).total_seconds() > 300:
            return {"status": "FAIL", "reason": "verification_timestamp_in_future"}
        if (
            payload.get("kind") != VERIFICATION_KIND
            or payload.get("format_version") != VERIFICATION_FORMAT_VERSION
            or payload.get("status") != "PASS"
            or payload.get("archive_name") != archive.name
            or payload.get("archive_size_bytes") != stat.st_size
            or payload.get("archive_sha256") != _sha256_file(archive)
            or payload.get("verification_scope")
            != "encrypted_archive_and_sqlite_snapshot_integrity"
            or checks != _VERIFICATION_CHECKS
        ):
            return {"status": "FAIL", "reason": "verification_record_does_not_match_archive"}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "FAIL", "reason": "verification_record_invalid"}
    return {
        "status": "PASS",
        "reason": None,
        "archive_name": archive.name,
        "archive_sha256": payload["archive_sha256"],
        "archive_size_bytes": stat.st_size,
        "verified_at_utc": payload["verified_at_utc"],
        "verification_checks": dict(_VERIFICATION_CHECKS),
        "verification_scope": payload["verification_scope"],
        "source": "archive_verification_sidecar",
    }


__all__ = [
    "VERIFICATION_FORMAT_VERSION",
    "VERIFICATION_KIND",
    "VERIFICATION_SUFFIX",
    "read_backup_verification_record",
    "verification_path",
]
