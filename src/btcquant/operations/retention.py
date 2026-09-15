"""Deterministic, non-destructive retention planning for operator status.

This module deliberately plans only. It never removes, renames, or mutates
files. Unknown, unreadable, symlinked, or unverified artifacts are preserved.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_SCAN_ENTRIES = 20_000
RETENTION_MODE = "DRY_RUN_ONLY"


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def _bounded_size(path: Path) -> tuple[int | None, str]:
    """Return ``(bytes, measurement)`` without following symlinks."""

    if path.is_symlink():
        return None, "UNKNOWN_SYMLINK"
    try:
        if path.is_file():
            return path.stat(follow_symlinks=False).st_size, "COMPLETE"
        if not path.is_dir():
            return 0, "COMPLETE"
    except OSError:
        return None, "UNKNOWN_UNREADABLE"

    total = 0
    scanned = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            return total, "UNKNOWN_UNREADABLE"
        scanned += len(entries)
        if scanned > MAX_SCAN_ENTRIES:
            return total, "UNKNOWN_SCAN_LIMIT"
        for entry in entries:
            entry_path = Path(entry.path)
            if entry.is_symlink():
                return total, "UNKNOWN_SYMLINK"
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry_path)
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                return total, "UNKNOWN_UNREADABLE"
    return total, "COMPLETE"


def measure_footprints(root: Path) -> dict[str, Any]:
    """Measure bounded local footprints used by the read-only status command."""

    root = root.resolve()
    footprints: dict[str, Any] = {}
    for name, path in (
        ("releases", root / "releases"),
        ("backups", root / "backups"),
        ("state", root / "state"),
    ):
        size, measurement = _bounded_size(path)
        footprints[name] = {
            "path": str(path),
            "bytes": size,
            "gib": round(size / 1024**3, 3) if size is not None else None,
            "measurement": measurement,
        }

    state = root / "state"
    wal_bytes = 0
    wal_status = "COMPLETE"
    for path in (state / "btcquant.db-wal", state / "btcquant.db-shm"):
        size, measurement = _bounded_size(path)
        if size is None:
            wal_status = measurement
        else:
            wal_bytes += size
    footprints["sqlite_wal_shm"] = {
        "path": str(state),
        "bytes": wal_bytes if wal_status == "COMPLETE" else None,
        "gib": round(wal_bytes / 1024**3, 3) if wal_status == "COMPLETE" else None,
        "measurement": wal_status,
    }

    logs = [path for path in sorted(state.glob("*.log")) if not path.is_symlink()]
    log_bytes = 0
    log_status = "COMPLETE"
    for path in logs:
        size, measurement = _bounded_size(path)
        if size is None:
            log_status = measurement
        else:
            log_bytes += size
    footprints["application_logs"] = {
        "path": str(state),
        "bytes": log_bytes if log_status == "COMPLETE" else None,
        "gib": round(log_bytes / 1024**3, 3) if log_status == "COMPLETE" else None,
        "measurement": log_status,
        "files": [str(path) for path in logs],
    }
    footprints["journal"] = {
        "bytes": None,
        "gib": None,
        "measurement": "NOT_MEASURED",
        "reason": "outside_service_root",
    }

    temporary_names = (".pytest_cache", ".mypy_cache", ".ruff_cache", ".coverage", "tmp")
    temporary = [root / name for name in temporary_names if (root / name).exists()]
    temporary_size = 0
    temporary_status = "COMPLETE"
    for path in temporary:
        size, measurement = _bounded_size(path)
        if size is None:
            temporary_status = measurement
        else:
            temporary_size += size
    footprints["temporary_artifacts"] = {
        "path": str(root),
        "bytes": temporary_size if temporary_status == "COMPLETE" else None,
        "gib": round(temporary_size / 1024**3, 3) if temporary_status == "COMPLETE" else None,
        "measurement": temporary_status,
        "entries": [str(path) for path in temporary],
    }
    return footprints


def _age_seconds(path: Path, now: datetime) -> float | None:
    try:
        return round(max(0.0, now.timestamp() - path.stat(follow_symlinks=False).st_mtime), 3)
    except OSError:
        return None


def _item(
    *,
    path: Path,
    artifact_type: str,
    now: datetime,
    classification: str,
    reason: str,
    eligible: bool,
) -> dict[str, Any]:
    size, measurement = _bounded_size(path)
    return {
        "path": str(path),
        "artifact_type": artifact_type,
        "age_seconds": _age_seconds(path, now),
        "size_bytes": size,
        "measurement": measurement,
        "classification": classification,
        "protection_reason": reason,
        "eligible_for_cleanup": eligible and measurement == "COMPLETE",
    }


def plan_retention(
    root: Path,
    *,
    now: datetime | None = None,
    protected_release_sha: str | None = None,
    latest_verified_backup: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic dry-run plan; never delete anything."""

    root = root.resolve()
    observed = _utc_now(now)
    releases = root / "releases"
    protected_releases: set[Path] = set()
    for link in (root / "current", root / "previous"):
        if link.exists() or link.is_symlink():
            try:
                protected_releases.add(link.resolve(strict=True))
            except OSError:
                pass
    if protected_release_sha and len(protected_release_sha) == 40:
        protected_releases.add((releases / protected_release_sha).resolve())

    items: list[dict[str, Any]] = []
    if releases.is_dir() and not releases.is_symlink():
        for path in sorted(releases.iterdir(), key=lambda item: item.name):
            try:
                resolved = path.resolve(strict=True)
                confined = resolved.is_relative_to(root)
            except OSError:
                resolved = path
                confined = False
            if path.is_symlink() or not confined:
                items.append(
                    _item(
                        path=path,
                        artifact_type="release",
                        now=observed,
                        classification="UNKNOWN",
                        reason="symlink_or_outside_root",
                        eligible=False,
                    )
                )
            elif resolved in protected_releases:
                items.append(
                    _item(
                        path=path,
                        artifact_type="release",
                        now=observed,
                        classification="PROTECTED",
                        reason="current_previous_or_active_maturity_release",
                        eligible=False,
                    )
                )
            else:
                items.append(
                    _item(
                        path=path,
                        artifact_type="release",
                        now=observed,
                        classification="OLD_RELEASE_CANDIDATE",
                        reason="not_referenced_by_current_protection_set",
                        eligible=True,
                    )
                )

    backups = root / "backups"
    if backups.is_dir() and not backups.is_symlink():
        for path in sorted(backups.glob("*.tar.gz.enc"), key=lambda item: item.name):
            verified = latest_verified_backup is not None and path.name == latest_verified_backup
            items.append(
                _item(
                    path=path,
                    artifact_type="encrypted_backup",
                    now=observed,
                    classification="PROTECTED" if verified else "UNKNOWN",
                    reason="latest_verified_backup" if verified else "verification_not_proven",
                    eligible=False,
                )
            )

    for name, reason in (
        ("state", "production_database_and_operational_evidence"),
        ("data", "production_data"),
        ("backups-repo", "backup_repository_requires_independent_policy"),
    ):
        path = root / name
        if path.exists() or path.is_symlink():
            items.append(
                _item(
                    path=path,
                    artifact_type=name,
                    now=observed,
                    classification="PROTECTED",
                    reason=reason,
                    eligible=False,
                )
            )

    eligible = [item for item in items if item["eligible_for_cleanup"]]
    known_eligible_bytes = sum(
        int(item["size_bytes"]) for item in eligible if isinstance(item["size_bytes"], int)
    )
    return {
        "mode": RETENTION_MODE,
        "observed_at": observed.isoformat(),
        "deletions_performed": 0,
        "unknown_or_unverified_preserved": True,
        "items": items,
        "eligible_count": len(eligible),
        "eligible_bytes": known_eligible_bytes,
        "protected_count": sum(item["classification"] == "PROTECTED" for item in items),
        "unknown_count": sum(item["classification"] == "UNKNOWN" for item in items),
    }
