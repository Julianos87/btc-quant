"""Fail-closed backup, restore and disaster-recovery primitives.

This module deliberately does not know about production paths or systemd.  A
caller must provide an explicit temporary/staging destination and an explicit
allow-list of sources.  The resulting BackupSet is an immutable directory
identified by its manifest content, not by a timestamp or a mutable alias.

The module is intentionally conservative:

* SQLite sources are copied with the online backup API through a read-only
  connection; no checkpoint, WAL deletion or source write is performed.
* a completed set is published only after every file, hash, manifest and
  integrity check succeeds;
* restore always targets a new staging directory and creates a recovery gate;
* retention never deletes an invalid or unverified set;
* trading and research recovery markers are separate and both fail closed.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

BACKUP_SCHEMA_VERSION = 1
BACKUP_TOOL_VERSION = "lot7-backup-v1"
MANIFEST_NAME = "backup-manifest.json"
MANIFEST_SHA_NAME = "backup-manifest.sha256"
VERIFICATION_NAME = "verification-record.json"
LOCK_NAME = ".backup.lock"

TRADING_RECOVERY_MARKER = "recovery-state.json"
SHADOW_RECOVERY_MARKER = "shadow-recovery-state.json"
RESEARCH_RECOVERY_MARKER = "research-recovery-state.json"

CLASSIFICATIONS = frozenset(
    {
        "AUTHORITATIVE_TRADING_STATE",
        "AUTHORITATIVE_SHADOW_STATE",
        "AUTHORITATIVE_RESEARCH_GOVERNANCE",
        "AUDIT_EVIDENCE",
        "CONFIGURATION",
        "DERIVED_REBUILDABLE",
    }
)
_SECRET_PARTS = {
    ".env",
    "secret",
    "secrets",
    "private-key",
    "private_key",
    "wallet",
    "seed",
    "token",
    "credentials",
}
_ALLOWED_MANIFEST_FILES = frozenset({MANIFEST_NAME, MANIFEST_SHA_NAME, VERIFICATION_NAME})
_RESTORE_FILENAMES = {
    "trading_state": "btcquant.db",
    "shadow_state": "execution-shadow.db",
    "governance_state": "governance.sqlite3",
}
_ALLOWED_LOGICAL_NAMES = {
    "AUTHORITATIVE_TRADING_STATE": {"trading_state"},
    "AUTHORITATIVE_SHADOW_STATE": {"shadow_state"},
    "AUTHORITATIVE_RESEARCH_GOVERNANCE": {"governance_state"},
    "AUDIT_EVIDENCE": {"audit_evidence"},
    "CONFIGURATION": {"configuration"},
    "DERIVED_REBUILDABLE": {"derived_state"},
}
_RECOVERY_STATUSES = frozenset({"RECOVERY_REQUIRED", "RECONCILIATION_VERIFIED", "RECOVERY_CLEARED"})
_RECONCILIATION_EVIDENCE = frozenset(
    {
        "exchange_reachable",
        "local_orders_reconciled",
        "external_orders_reconciled",
        "positions_reconciled",
        "stops_reconciled",
        "no_unbalanced_state",
        "no_ambiguity",
        "accounting_checkpoint_compatible",
    }
)
_RESEARCH_RECONCILIATION_EVIDENCE = frozenset(
    {
        "governance_lineage_reconciled",
        "trial_history_reconciled",
        "holdout_history_reconciled",
        "dataset_usage_reconciled",
        "policy_freeze_consistent",
    }
)
_RECOVERY_TRANSITIONS = {
    "RECOVERY_REQUIRED": {"RECONCILIATION_VERIFIED"},
    "RECONCILIATION_VERIFIED": {"RECOVERY_CLEARED"},
    "RECOVERY_CLEARED": set(),
}


class BackupError(RuntimeError):
    """Base error with a stable operational reason code."""

    reason = "BACKUP_ERROR"

    def __init__(self, message: str = "") -> None:
        super().__init__(f"{self.reason}: {message}" if message else self.reason)


class BackupBusy(BackupError):
    reason = "BACKUP_ALREADY_RUNNING"


class SourceUnavailable(BackupError):
    reason = "SOURCE_UNAVAILABLE"


class WritersNotQuiesced(BackupError):
    reason = "MIGRATION_REFUSED_WRITERS_ACTIVE"


class BackupIntegrityError(BackupError):
    reason = "BACKUP_INTEGRITY_FAILED"


class ManifestInvalid(BackupError):
    reason = "MANIFEST_INVALID"


class HashMismatch(BackupError):
    reason = "BACKUP_HASH_MISMATCH"


class PathUnsafe(BackupError):
    reason = "UNSAFE_BACKUP_PATH"


class SchemaIncompatible(BackupError):
    reason = "SCHEMA_INCOMPATIBLE"


class InsufficientDisk(BackupError):
    reason = "INSUFFICIENT_DISK_SPACE"


class ClockSkew(BackupError):
    reason = "CLOCK_SKEW"


class RetentionRefused(BackupError):
    reason = "RETENTION_REFUSED"


class RestoreRefused(BackupError):
    reason = "RESTORE_REFUSED"


class ExportRefused(BackupError):
    reason = "OFFHOST_EXPORT_REFUSED"


class RecoveryRequired(BackupError):
    reason = "RECOVERY_REQUIRED"


class ResearchRecoveryRequired(BackupError):
    reason = "RESEARCH_RECOVERY_REQUIRED"


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return current.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc(parsed)


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or not value:
        raise PathUnsafe(value)
    if any(part in {"", "."} for part in candidate.parts):
        raise PathUnsafe(value)
    return candidate.as_posix()


def _reject_secret_path(path: Path) -> None:
    lowered = {part.lower() for part in path.parts}
    name = path.name.lower()
    if (
        lowered & _SECRET_PARTS
        or name.startswith(".env")
        or ".env" in name
        or name in {"id_rsa", "id_ed25519"}
        or name.endswith((".key", ".pem"))
    ):
        raise PathUnsafe(f"secret-like source rejected: {path}")


def _controlled_path(path: Path, *, create: bool) -> Path:
    """Return a runtime root without silently following a symlink escape."""

    candidate = path.expanduser().absolute()
    existing = candidate
    while True:
        if existing.is_symlink():
            raise PathUnsafe(f"symlink in controlled path: {existing}")
        if existing == existing.parent:
            break
        existing = existing.parent
    if candidate.exists() and not candidate.is_dir():
        raise PathUnsafe(f"controlled root is not a directory: {candidate}")
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    if candidate.exists() and candidate.is_symlink():
        raise PathUnsafe(f"controlled root became a symlink: {candidate}")
    if candidate.exists():
        candidate.chmod(0o700)
    return candidate


def _restrict_mode(path: Path, mode: int) -> None:
    path.chmod(mode)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class BackupLock:
    """Non-blocking single-flight lock shared by create, prune and restore."""

    def __init__(self, root: str | Path) -> None:
        self.root = _controlled_path(Path(root), create=False)
        self.handle: Any = None

    def __enter__(self) -> BackupLock:
        self.root.mkdir(parents=True, exist_ok=True)
        _reject_symlink_components(self.root)
        lock_path = self.root / LOCK_NAME
        if lock_path.is_symlink():
            raise PathUnsafe(f"unsafe backup lock: {lock_path}")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            self.handle = os.fdopen(descriptor, "a+")
        except OSError as error:
            if getattr(error, "errno", None) in {errno.ELOOP, errno.ENXIO}:
                raise PathUnsafe(f"unsafe backup lock: {lock_path}") from error
            raise
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            self.handle = None
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise BackupBusy(str(lock_path)) from error
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


DB_WRITER_SERVICES = frozenset(
    {
        "btcquant-carry.service",
        "btcquant-trend.service",
        "btcquant-watchdog.service",
        "btcquant-compact.service",
        "btcquant-backup.service",
        "btcquant-rebalance.service",
        "btcquant-rebalance-pending.service",
    }
)
DB_WRITER_TIMERS = frozenset(
    {
        "btcquant-watchdog.timer",
        "btcquant-compact.timer",
        "btcquant-backup.timer",
        "btcquant-rebalance.timer",
        "btcquant-rebalance-pending.timer",
    }
)
_INACTIVE_STATES = frozenset({"inactive", "dead", "failed", "absent"})


def require_writer_quiescence(
    service_states: Mapping[str, str],
    timer_states: Mapping[str, str],
    *,
    open_handles: Iterable[str] = (),
) -> None:
    """Refuse a migration unless every known writer and timer is inactive.

    The function is deliberately a gate, not a stopper. Production orchestration
    must stop units first, then collect fresh state and optionally inspect open
    file handles. Missing state is fail-closed; ``absent`` is an explicit result
    from the caller, not an assumption made by this library.
    """
    missing_services = sorted(DB_WRITER_SERVICES - set(service_states))
    missing_timers = sorted(DB_WRITER_TIMERS - set(timer_states))
    active_services = sorted(
        name for name in DB_WRITER_SERVICES if service_states.get(name) not in _INACTIVE_STATES
    )
    active_timers = sorted(
        name for name in DB_WRITER_TIMERS if timer_states.get(name) not in _INACTIVE_STATES
    )
    handles = sorted(str(handle) for handle in open_handles if str(handle))
    if missing_services or missing_timers or active_services or active_timers or handles:
        details = {
            "missing_services": missing_services,
            "missing_timers": missing_timers,
            "active_services": active_services,
            "active_timers": active_timers,
            "open_handles": handles,
        }
        raise WritersNotQuiesced(json.dumps(details, sort_keys=True))


@dataclass(frozen=True)
class BackupSource:
    logical_name: str
    source_path: Path
    classification: str
    source_type: str = "sqlite"
    restore_required: bool = True
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.logical_name or "/" in self.logical_name or "\\" in self.logical_name:
            raise ValueError("logical_name must be a single stable identifier")
        if self.classification not in CLASSIFICATIONS:
            raise ValueError(f"unknown classification: {self.classification}")
        if self.source_type not in {"sqlite", "file"}:
            raise ValueError(f"unknown source_type: {self.source_type}")
        allowed_names = _ALLOWED_LOGICAL_NAMES[self.classification]
        if self.logical_name not in allowed_names:
            raise ValueError(
                f"logical_name {self.logical_name!r} is not allowed for {self.classification}"
            )
        _reject_secret_path(self.source_path)


@dataclass(frozen=True)
class RetentionPolicy:
    """UTC generation policy; values are operational policy, not data facts."""

    recent_count: int = 24
    daily_count: int = 7
    weekly_count: int = 4
    monthly_count: int = 12
    safety_margin_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.recent_count,
                self.daily_count,
                self.weekly_count,
                self.monthly_count,
            )
        ):
            raise ValueError("retention counts cannot be negative")


@dataclass(frozen=True)
class BackupVerification:
    backup_id: str
    valid: bool
    trusted: bool
    state: str
    restore_verified: bool
    total_bytes: int
    app_schema_version: int | None
    manifest: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class RestoreResult:
    backup_id: str
    staging_path: Path
    recovery_marker: Path | None
    research_recovery_marker: Path | None
    migration_required: bool


def _reject_symlink_components(candidate: Path) -> None:
    current = candidate
    while True:
        if current.is_symlink():
            raise PathUnsafe(f"symlink in controlled path: {current}")
        if current == current.parent:
            break
        current = current.parent


def _source_uri(path: Path) -> str:
    return f"{path.absolute().as_uri()}?mode=ro"


def _source_path(path: Path, *, optional: bool) -> Path | None:
    candidate = path.expanduser().absolute()
    _reject_symlink_components(candidate)
    if not candidate.exists():
        if optional:
            return None
        raise SourceUnavailable(str(candidate))
    if not candidate.is_file():
        raise SourceUnavailable(str(candidate))
    _reject_secret_path(candidate)
    return candidate


def _sqlite_schema_version(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return int(row[0]) if row is not None else None


def _check_sqlite(path: Path) -> tuple[int | None, str, bool]:
    try:
        with sqlite3.connect(_source_uri(path), uri=True, timeout=15.0) as connection:
            connection.execute("PRAGMA query_only = ON")
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise BackupIntegrityError(f"{path}: integrity_check={integrity}")
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise BackupIntegrityError(f"{path}: foreign_key_check returned rows")
            return _sqlite_schema_version(connection), integrity, True
    except (sqlite3.DatabaseError, OSError) as error:
        if isinstance(error, BackupError):
            raise
        raise BackupIntegrityError(f"{path}: {error}") from error


def _copy_sqlite(source: Path, destination: Path) -> tuple[int | None, str, bool]:
    try:
        with sqlite3.connect(_source_uri(source), uri=True, timeout=15.0) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            integrity = str(source_connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise BackupIntegrityError(f"{source}: integrity_check={integrity}")
            if source_connection.execute("PRAGMA foreign_key_check").fetchall():
                raise BackupIntegrityError(f"{source}: foreign_key_check returned rows")
            with sqlite3.connect(destination) as destination_connection:
                source_connection.backup(destination_connection, pages=256, sleep=0.05)
                destination_connection.commit()
                # The destination is a self-contained backup artifact. Do not
                # leave a destination WAL beside it; SQLite's backup API has
                # already copied a consistent view of the source.
                destination_connection.execute("PRAGMA journal_mode = DELETE")
                destination_connection.commit()
            schema = _sqlite_schema_version(source_connection)
        _check_sqlite(destination)
        return schema, integrity, True
    except sqlite3.DatabaseError as error:
        raise BackupIntegrityError(f"SQLite backup failed for {source}: {error}") from error


def _estimated_bytes(sources: Iterable[BackupSource]) -> int:
    total = 0
    for source in sources:
        source_path = _source_path(source.source_path, optional=source.optional)
        if source_path is None:
            continue
        total += source_path.stat().st_size
        if source.source_type == "sqlite":
            for suffix in ("-wal", "-shm"):
                sidecar = source_path.with_name(source_path.name + suffix)
                if sidecar.exists():
                    if sidecar.is_symlink() or not sidecar.is_file():
                        raise PathUnsafe(str(sidecar))
                    total += sidecar.stat().st_size
    return total


def _check_space(root: Path, required: int, disk_usage_fn: Callable[[str], Any]) -> None:
    usage = disk_usage_fn(str(root))
    if int(usage.free) < required:
        raise InsufficientDisk(f"free={usage.free}, required={required}")


def _manifest_core(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key not in {"manifest_sha256"}}


def _write_manifest(directory: Path, manifest: dict[str, Any]) -> None:
    payload = _canonical_json(_manifest_core(manifest))
    manifest["manifest_sha256"] = _sha256_bytes(payload)
    manifest_path = directory / MANIFEST_NAME
    manifest_path.write_bytes(_canonical_json(manifest))
    _restrict_mode(manifest_path, 0o600)
    _fsync_file(manifest_path)
    digest_path = directory / MANIFEST_SHA_NAME
    digest_path.write_text(f"{sha256_file(manifest_path)}  {MANIFEST_NAME}\n", encoding="ascii")
    _restrict_mode(digest_path, 0o600)
    _fsync_file(digest_path)


def _read_manifest(directory: Path) -> dict[str, Any]:
    if not directory.is_dir() or directory.is_symlink():
        raise ManifestInvalid(str(directory))
    manifest_path = directory / MANIFEST_NAME
    digest_path = directory / MANIFEST_SHA_NAME
    if (
        manifest_path.is_symlink()
        or digest_path.is_symlink()
        or not manifest_path.is_file()
        or not digest_path.is_file()
    ):
        raise ManifestInvalid(f"missing or symlinked manifest files in {directory}")
    try:
        expected = digest_path.read_text(encoding="ascii").split()[0]
        actual = sha256_file(manifest_path)
        if expected != actual:
            raise HashMismatch(f"manifest expected={expected} actual={actual}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError) as error:
        raise ManifestInvalid(str(directory)) from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("backup_schema_version") != BACKUP_SCHEMA_VERSION
    ):
        raise ManifestInvalid("unknown backup schema version")
    core_hash = manifest.get("manifest_sha256")
    if core_hash != _sha256_bytes(_canonical_json(_manifest_core(manifest))):
        raise HashMismatch("manifest content hash mismatch")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManifestInvalid("manifest entries missing")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ManifestInvalid("invalid entry")
        logical = entry.get("logical_name")
        filename = entry.get("backup_filename")
        status = entry.get("source_status", "PRESENT")
        if not isinstance(logical, str) or logical in seen:
            raise ManifestInvalid("duplicate logical entry")
        seen.add(logical)
        if status == "OPTIONAL_NOT_PRESENT":
            if entry.get("optional") is not True or entry.get("size_bytes") != 0:
                raise ManifestInvalid("invalid optional source entry")
            if entry.get("sha256") is not None:
                raise ManifestInvalid("optional source cannot have a hash")
            continue
        if status != "PRESENT" or not isinstance(filename, str):
            raise ManifestInvalid("invalid source status or filename")
        _safe_relative(filename)
    return manifest


def _verify_directory_contents(directory: Path, manifest: Mapping[str, Any]) -> int:
    allowed = set(_ALLOWED_MANIFEST_FILES)
    total = 0
    for entry in manifest["entries"]:
        if entry.get("source_status", "PRESENT") == "OPTIONAL_NOT_PRESENT":
            continue
        relative = _safe_relative(str(entry["backup_filename"]))
        target = directory / relative
        if target.is_symlink() or not target.is_file():
            raise HashMismatch(f"missing or symlink entry: {relative}")
        allowed.add(relative)
        expected_size = int(entry["size_bytes"])
        if expected_size < 0 or not isinstance(entry.get("sha256"), str):
            raise ManifestInvalid(f"invalid entry metadata: {relative}")
        actual_size = target.stat().st_size
        if actual_size != expected_size:
            raise HashMismatch(f"size mismatch: {relative}")
        actual_hash = sha256_file(target)
        if actual_hash != entry["sha256"]:
            raise HashMismatch(f"hash mismatch: {relative}")
        if entry.get("source_type") == "sqlite":
            schema, integrity, _ = _check_sqlite(target)
            if schema != entry.get("sqlite_schema_version") or integrity != entry.get(
                "sqlite_integrity_check"
            ):
                raise ManifestInvalid(f"SQLite metadata mismatch: {relative}")
        total += actual_size
    for child in directory.rglob("*"):
        if child.is_symlink():
            raise PathUnsafe(f"symlink in backup set: {child}")
        if child.is_file() and child.relative_to(directory).as_posix() not in allowed:
            raise ManifestInvalid(f"unexpected backup file: {child}")
    return total


def _validate_manifest_metadata(manifest: Mapping[str, Any], total: int) -> None:
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManifestInvalid("manifest entries missing")
    if manifest.get("total_bytes") != total:
        raise ManifestInvalid("manifest total_bytes mismatch")
    for entry in entries:
        classification = entry.get("classification")
        logical_name = entry.get("logical_name")
        if classification not in CLASSIFICATIONS or logical_name not in _ALLOWED_LOGICAL_NAMES.get(
            classification, set()
        ):
            raise ManifestInvalid("manifest source identity is not allow-listed")
        source_type = entry.get("source_type")
        if source_type not in {"sqlite", "file"}:
            raise ManifestInvalid("manifest source type is invalid")
        if entry.get("source_status", "PRESENT") not in {"PRESENT", "OPTIONAL_NOT_PRESENT"}:
            raise ManifestInvalid("manifest source status is invalid")


def verify_backup_set(
    backup_directory: str | Path,
    *,
    expected_app_schema_version: int | None = None,
    now: datetime | None = None,
    clock_skew_tolerance_seconds: float = 5.0,
) -> BackupVerification:
    directory = _controlled_path(Path(backup_directory), create=False)
    manifest = _read_manifest(directory)
    backup_id = str(manifest.get("backup_id", ""))
    if not backup_id or directory.name != backup_id:
        raise ManifestInvalid("backup id does not match directory")
    state = str(manifest.get("state", ""))
    if state not in {"COMPLETED", "DEGRADED"}:
        raise ManifestInvalid(f"backup state is not published: {state}")
    created = _parse_iso(str(manifest["created_at_utc"]))
    if created > _utc(now) + timedelta(seconds=clock_skew_tolerance_seconds):
        raise ClockSkew(f"backup created in the future: {created}")
    skew = float(manifest.get("capture_skew_seconds", 0.0))
    threshold = float(manifest.get("max_capture_skew_seconds", 0.0))
    if skew < 0 or threshold < 0:
        raise ManifestInvalid("negative capture skew policy")
    expected_state = "DEGRADED" if skew > threshold else "COMPLETED"
    if state != expected_state:
        raise ManifestInvalid("backup state is inconsistent with captured skew")
    total = _verify_directory_contents(directory, manifest)
    _validate_manifest_metadata(manifest, total)
    schema_versions = {
        int(entry["sqlite_schema_version"])
        for entry in manifest["entries"]
        if entry.get("sqlite_schema_version") is not None
    }
    app_schema = max(schema_versions) if schema_versions else None
    if expected_app_schema_version is not None and app_schema is not None:
        if app_schema > expected_app_schema_version:
            raise SchemaIncompatible(
                f"backup={app_schema}, expected<={expected_app_schema_version}"
            )
    verification_path = directory / VERIFICATION_NAME
    restore_verified = False
    if verification_path.is_file() and not verification_path.is_symlink():
        try:
            record = json.loads(verification_path.read_text(encoding="utf-8"))
            restore_verified = (
                record.get("manifest_sha256") == manifest.get("manifest_sha256")
                and record.get("verification_status") == "VERIFIED"
            )
        except (OSError, ValueError):
            restore_verified = False
    return BackupVerification(
        backup_id=backup_id,
        valid=True,
        trusted=state == "COMPLETED",
        state=state,
        restore_verified=restore_verified,
        total_bytes=total,
        app_schema_version=app_schema,
        manifest=manifest,
    )


def export_verified_backup_set(
    backup_directory: str | Path,
    export_destination: str | Path,
    *,
    exporter: Callable[[Path, Path, BackupVerification], None],
    expected_app_schema_version: int | None = None,
    now: datetime | None = None,
) -> BackupVerification:
    """Admit only a completed, verified set to an off-host exporter.

    The exporter is deliberately injected: production integration must perform
    authenticated encryption and remote publication, while tests can remain
    entirely local. The callback is never invoked for a corrupt, degraded,
    unverified or otherwise untrusted BackupSet.
    """

    backup = _controlled_path(Path(backup_directory), create=False)
    destination = Path(export_destination).expanduser().absolute()
    with BackupLock(backup.parent):
        if destination.exists() or destination.is_symlink():
            raise ExportRefused(f"export destination already exists: {destination}")
        verification = verify_backup_set(
            backup,
            expected_app_schema_version=expected_app_schema_version,
            now=now,
        )
        if (
            not verification.trusted
            or verification.state != "COMPLETED"
            or not verification.restore_verified
        ):
            raise ExportRefused("only a completed verified BackupSet may be exported")
        exporter(backup, destination, verification)
    if not destination.exists() or destination.is_symlink():
        raise ExportRefused("exporter did not publish a destination")
    return verification


def _write_verification_record(
    directory: Path, manifest: Mapping[str, Any], *, restored: bool = False
) -> None:
    record = {
        "record_schema_version": 1,
        "verification_status": "VERIFIED",
        "manifest_sha256": manifest["manifest_sha256"],
        "verified_at_utc": _iso(datetime.now(UTC)),
        "restore_verified_at_utc": _iso(datetime.now(UTC)) if restored else None,
    }
    path = directory / VERIFICATION_NAME
    path.write_bytes(_canonical_json(record))
    _restrict_mode(path, 0o600)
    _fsync_file(path)
    _fsync_directory(directory)


def create_backup_set(
    sources: Iterable[BackupSource],
    destination_root: str | Path,
    *,
    app_git_sha: str,
    app_schema_version: int,
    source_identity: str,
    now: datetime | None = None,
    max_capture_skew_seconds: float = 30.0,
    disk_usage_fn: Callable[[str], Any] = shutil.disk_usage,
    failure_hook: Callable[[str], None] | None = None,
) -> Path:
    source_list = list(sources)
    if not source_list:
        raise ValueError("at least one explicit source is required")
    root = _controlled_path(Path(destination_root), create=True)
    with BackupLock(root):
        _check_space(root, 2 * _estimated_bytes(source_list) + 1024 * 1024, disk_usage_fn)
        created = _utc(now)
        stage = Path(tempfile.mkdtemp(prefix=".backup-staging-", dir=root))
        _restrict_mode(stage, 0o700)
        try:
            entries: list[dict[str, Any]] = []
            capture_times: list[datetime] = []
            for source in source_list:
                source_path = _source_path(source.source_path, optional=source.optional)
                filename = (
                    f"{source.logical_name}.sqlite3"
                    if source.source_type == "sqlite"
                    else f"{source.logical_name}.bin"
                )
                if source_path is None:
                    entries.append(
                        {
                            "logical_name": source.logical_name,
                            "source_path": str(source.source_path.expanduser().absolute()),
                            "source_type": source.source_type,
                            "classification": source.classification,
                            "restore_required": source.restore_required,
                            "optional": True,
                            "source_status": "OPTIONAL_NOT_PRESENT",
                            "backup_filename": filename,
                            "size_bytes": 0,
                            "sha256": None,
                            "sqlite_schema_version": None,
                            "sqlite_integrity_check": None,
                            "captured_at_utc": None,
                            "backup_method": "NOT_CAPTURED_OPTIONAL",
                        }
                    )
                    continue
                started = _utc(now) if now is not None else datetime.now(UTC)
                capture_times.append(started)
                target = stage / filename
                if source.source_type == "sqlite":
                    schema, integrity, _ = _copy_sqlite(source_path, target)
                else:
                    with (
                        source_path.open("rb") as source_stream,
                        target.open("wb") as target_stream,
                    ):
                        shutil.copyfileobj(source_stream, target_stream)
                        target_stream.flush()
                        os.fsync(target_stream.fileno())
                    schema, integrity = None, None
                _restrict_mode(target, 0o600)
                completed = _utc(now) if now is not None else datetime.now(UTC)
                capture_times.append(completed)
                entries.append(
                    {
                        "logical_name": source.logical_name,
                        "source_path": str(source_path),
                        "source_type": source.source_type,
                        "classification": source.classification,
                        "restore_required": source.restore_required,
                        "optional": source.optional,
                        "source_status": "PRESENT",
                        "backup_filename": filename,
                        "size_bytes": target.stat().st_size,
                        "sha256": sha256_file(target),
                        "sqlite_schema_version": schema,
                        "sqlite_integrity_check": integrity,
                        "captured_at_utc": _iso(completed),
                        "backup_method": "SQLITE_ONLINE_BACKUP_API"
                        if source.source_type == "sqlite"
                        else "ATOMIC_FILE_COPY",
                    }
                )
                _fsync_file(target)
                if failure_hook:
                    failure_hook(f"after-entry:{source.logical_name}")
            if not capture_times:
                raise SourceUnavailable("no source was available for capture")
            skew = (max(capture_times) - min(capture_times)).total_seconds()
            state = "DEGRADED" if skew > max_capture_skew_seconds else "COMPLETED"
            manifest = {
                "backup_schema_version": BACKUP_SCHEMA_VERSION,
                "tool_version": BACKUP_TOOL_VERSION,
                "state": state,
                "backup_id": "pending",
                "created_at_utc": _iso(created),
                "capture_started_at_utc": _iso(min(capture_times)),
                "capture_completed_at_utc": _iso(max(capture_times)),
                "capture_skew_seconds": skew,
                "max_capture_skew_seconds": max_capture_skew_seconds,
                "app_git_sha": app_git_sha,
                "app_schema_version": app_schema_version,
                "source_identity": source_identity,
                "external_exchange_state_included": False,
                "total_bytes": sum(int(entry["size_bytes"]) for entry in entries),
                "entries": entries,
            }
            provisional = _canonical_json(manifest)
            manifest["backup_id"] = _sha256_bytes(provisional)[:32]
            if failure_hook:
                failure_hook("before-manifest")
            _write_manifest(stage, manifest)
            if failure_hook:
                failure_hook("after-manifest")
            staged_total = _verify_directory_contents(stage, manifest)
            _validate_manifest_metadata(manifest, staged_total)
            _write_verification_record(stage, manifest)
            backup_id = str(manifest["backup_id"])
            final = root / backup_id
            if final.exists() or final.is_symlink():
                raise BackupError(f"backup id already exists: {backup_id}")
            _fsync_directory(stage)
            if os.stat(stage).st_dev != os.stat(root).st_dev:
                raise BackupError("staging and backup root are on different filesystems")
            if failure_hook:
                failure_hook("before-publish")
            os.replace(stage, final)
            if failure_hook:
                failure_hook("after-publish")
            _fsync_directory(root)
            verify_backup_set(final, expected_app_schema_version=app_schema_version, now=created)
            return final
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise


def list_backup_sets(root: str | Path, *, now: datetime | None = None) -> list[BackupVerification]:
    try:
        directory = _controlled_path(Path(root), create=False)
    except PathUnsafe:
        return []
    if not directory.is_dir():
        return []
    result: list[BackupVerification] = []
    for child in directory.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            result.append(verify_backup_set(child, now=now))
        except BackupError:
            continue
    return sorted(result, key=lambda item: str(item.manifest["created_at_utc"]), reverse=True)


def latest_valid_backup(root: str | Path, *, now: datetime | None = None) -> BackupVerification:
    valid = [item for item in list_backup_sets(root, now=now) if item.trusted]
    if not valid:
        raise SourceUnavailable("no verified valid backup set")
    return valid[0]


def _bucket(value: datetime, kind: str) -> tuple[int, ...]:
    if kind == "day":
        return (value.year, value.month, value.day)
    if kind == "week":
        iso = value.isocalendar()
        return (iso.year, iso.week)
    return (value.year, value.month)


def prune_backup_sets(
    root: str | Path,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> list[str]:
    directory = _controlled_path(Path(root), create=False)
    with BackupLock(directory):
        candidates = [item for item in list_backup_sets(directory, now=now) if item.trusted]
        if not candidates:
            return []
        keep: set[str] = {candidates[0].backup_id}
        keep.update(item.backup_id for item in candidates[: policy.recent_count])
        rules = (
            ("day", policy.daily_count),
            ("week", policy.weekly_count),
            ("month", policy.monthly_count),
        )
        for kind, limit in rules:
            buckets: dict[tuple[int, ...], list[BackupVerification]] = {}
            for item in candidates:
                created = _parse_iso(str(item.manifest["created_at_utc"]))
                buckets.setdefault(_bucket(created, kind), []).append(item)
            for bucket in sorted(buckets, reverse=True)[:limit]:
                keep.add(buckets[bucket][0].backup_id)
        removed: list[str] = []
        for item in candidates:
            if item.backup_id in keep:
                continue
            target = directory / item.backup_id
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
                removed.append(item.backup_id)
        _fsync_directory(directory)
        return removed


def _marker_path(root: str | Path, filename: str) -> Path:
    path = _controlled_path(Path(root), create=False)
    marker = path / filename
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise PathUnsafe(f"unsafe recovery marker: {marker}")
    return marker


def _write_marker(path: Path, payload: Mapping[str, Any]) -> None:
    path = _marker_path(path.parent, path.name)
    _controlled_path(path.parent, create=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise PathUnsafe(f"unsafe recovery marker temporary path: {temporary}")
    temporary.write_bytes(_canonical_json(dict(payload)))
    _restrict_mode(temporary, 0o600)
    _fsync_file(temporary)
    os.replace(temporary, path)
    _restrict_mode(path, 0o600)
    _fsync_directory(path.parent)


def _positive_evidence(value: Any, required: set[str] | frozenset[str]) -> bool:
    return isinstance(value, Mapping) and all(value.get(key) is True for key in required)


def _validate_recovery_marker(value: Any, path: Path) -> None:
    if not isinstance(value, dict) or value.get("marker_schema_version") != 1:
        raise RecoveryRequired(f"invalid recovery marker: {path}")
    status = value.get("status")
    if status not in _RECOVERY_STATUSES:
        raise RecoveryRequired(f"unknown recovery status: {path}")
    if value.get("scope") not in {"trading", "shadow", "research"}:
        raise RecoveryRequired(f"invalid recovery scope: {path}")
    if value.get("external_exchange_state_included") is not False:
        raise RecoveryRequired(f"invalid exchange-state claim: {path}")
    try:
        restored_at = _parse_iso(str(value.get("restored_at_utc")))
    except (TypeError, ValueError) as error:
        raise RecoveryRequired(f"invalid recovery timestamp: {path}") from error
    if restored_at > datetime.now(UTC) + timedelta(seconds=5):
        raise RecoveryRequired(f"future recovery marker: {path}")
    transitions = value.get("transitions")
    expected_transitions = {
        "RECOVERY_REQUIRED": [(None, "RECOVERY_REQUIRED")],
        "RECONCILIATION_VERIFIED": [
            (None, "RECOVERY_REQUIRED"),
            ("RECOVERY_REQUIRED", "RECONCILIATION_VERIFIED"),
        ],
        "RECOVERY_CLEARED": [
            (None, "RECOVERY_REQUIRED"),
            ("RECOVERY_REQUIRED", "RECONCILIATION_VERIFIED"),
            ("RECONCILIATION_VERIFIED", "RECOVERY_CLEARED"),
        ],
    }[status]
    if not isinstance(transitions, list):
        raise RecoveryRequired(f"invalid recovery lifecycle: {path}")
    if any(not isinstance(item, dict) for item in transitions):
        raise RecoveryRequired(f"invalid recovery lifecycle: {path}")
    actual_transitions = [(item.get("from"), item.get("to")) for item in transitions]
    if actual_transitions != expected_transitions:
        raise RecoveryRequired(f"invalid recovery lifecycle: {path}")
    if status == "RECOVERY_REQUIRED" and (
        value.get("writer_start_allowed") is not False
        or value.get("real_search_allowed") is not False
    ):
        raise RecoveryRequired(f"invalid recovery gate: {path}")
    scope = str(value["scope"])
    if status == "RECONCILIATION_VERIFIED":
        required = (
            _RESEARCH_RECONCILIATION_EVIDENCE if scope == "research" else _RECONCILIATION_EVIDENCE
        )
        if (
            value.get("writer_start_allowed") is not False
            or value.get("real_search_allowed") is not False
            or not _positive_evidence(value.get("evidence"), required)
        ):
            raise RecoveryRequired(f"incomplete reconciliation evidence: {path}")
    if status == "RECOVERY_CLEARED":
        if not _positive_evidence(
            value.get("evidence"),
            _RESEARCH_RECONCILIATION_EVIDENCE if scope == "research" else _RECONCILIATION_EVIDENCE,
        ) or not _positive_evidence(value.get("evidence"), {"reconciliation_marker_verified"}):
            raise RecoveryRequired(f"incomplete recovery-clear evidence: {path}")
        allowed = (value.get("writer_start_allowed") is True) ^ (
            value.get("real_search_allowed") is True
        )
        if not allowed:
            raise RecoveryRequired(f"invalid recovery-clear gate: {path}")


def load_recovery_marker(
    root: str | Path,
    *,
    research: bool = False,
    shadow: bool = False,
) -> dict[str, Any] | None:
    if research and shadow:
        raise ValueError("research and shadow markers are mutually exclusive")
    filename = (
        RESEARCH_RECOVERY_MARKER
        if research
        else SHADOW_RECOVERY_MARKER
        if shadow
        else TRADING_RECOVERY_MARKER
    )
    path = _marker_path(root, filename)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RecoveryRequired(f"invalid recovery marker: {path}") from error
    _validate_recovery_marker(value, path)
    return value


def assert_writer_recovery_clear(root: str | Path) -> None:
    marker = load_recovery_marker(root)
    if marker is not None and marker.get("status") != "RECOVERY_CLEARED":
        raise RecoveryRequired("writer startup blocked until trading reconciliation is proven")


def assert_research_recovery_clear(root: str | Path) -> None:
    marker = load_recovery_marker(root, research=True)
    if marker is not None and marker.get("status") != "RECOVERY_CLEARED":
        raise ResearchRecoveryRequired("real research blocked until governance lineage is proven")


def assert_shadow_recovery_clear(root: str | Path) -> None:
    marker = load_recovery_marker(root, shadow=True)
    if marker is not None and marker.get("status") != "RECOVERY_CLEARED":
        raise RecoveryRequired("shadow writer blocked until shadow reconciliation is proven")


def write_recovery_marker(
    root: str | Path,
    *,
    backup_id: str,
    restored_app_schema_version: int | None,
    source_identity: str,
    restored_at: datetime | None = None,
    research: bool = False,
    shadow: bool = False,
) -> Path:
    if research and shadow:
        raise ValueError("research and shadow markers are mutually exclusive")
    filename = (
        RESEARCH_RECOVERY_MARKER
        if research
        else SHADOW_RECOVERY_MARKER
        if shadow
        else TRADING_RECOVERY_MARKER
    )
    path = _marker_path(root, filename)
    if path.exists() or path.is_symlink():
        raise RecoveryRequired(f"recovery marker already exists: {path}")
    scope = "research" if research else "shadow" if shadow else "trading"
    marker = {
        "marker_schema_version": 1,
        "scope": scope,
        "status": "RECOVERY_REQUIRED",
        "backup_id": backup_id,
        "restored_app_schema_version": restored_app_schema_version,
        "source_identity": source_identity,
        "restored_at_utc": _iso(_utc(restored_at)),
        "external_exchange_state_included": False,
        "writer_start_allowed": False,
        "real_search_allowed": False,
        "evidence": {},
        "transitions": [{"from": None, "to": "RECOVERY_REQUIRED"}],
    }
    _write_marker(path, marker)
    return path


def advance_recovery_marker(
    root: str | Path,
    new_status: str,
    *,
    evidence: Mapping[str, Any],
    research: bool = False,
    shadow: bool = False,
) -> Path:
    if research and shadow:
        raise ValueError("research and shadow markers are mutually exclusive")
    filename = (
        RESEARCH_RECOVERY_MARKER
        if research
        else SHADOW_RECOVERY_MARKER
        if shadow
        else TRADING_RECOVERY_MARKER
    )
    path = _marker_path(root, filename)
    marker = load_recovery_marker(root, research=research, shadow=shadow)
    if marker is None:
        raise RecoveryRequired("no recovery marker exists")
    current = str(marker.get("status"))
    if new_status not in _RECOVERY_TRANSITIONS.get(current, set()):
        raise RecoveryRequired(f"invalid recovery transition {current}->{new_status}")
    if marker.get("scope") != ("research" if research else "shadow" if shadow else "trading"):
        raise RecoveryRequired("recovery marker scope mismatch")
    required = (
        _RESEARCH_RECONCILIATION_EVIDENCE
        if research and new_status == "RECONCILIATION_VERIFIED"
        else _RECONCILIATION_EVIDENCE
        if new_status == "RECONCILIATION_VERIFIED"
        else {"reconciliation_marker_verified"}
    )
    if not _positive_evidence(evidence, required):
        raise RecoveryRequired(f"insufficient positive evidence for {new_status}")
    merged_evidence = {**dict(marker.get("evidence", {})), **dict(evidence)}
    updated = dict(marker)
    updated.update(
        {
            "status": new_status,
            "evidence": merged_evidence,
            "writer_start_allowed": new_status == "RECOVERY_CLEARED" and not research,
            "real_search_allowed": new_status == "RECOVERY_CLEARED" and research,
            "transitions": [
                *list(marker["transitions"]),
                {"from": current, "to": new_status},
            ],
        }
    )
    _validate_recovery_marker(updated, path)
    _write_marker(path, updated)
    return path


def restore_to_staging(
    backup_directory: str | Path,
    staging_destination: str | Path,
    *,
    runtime_root: str | Path,
    expected_app_schema_version: int,
    now: datetime | None = None,
) -> RestoreResult:
    backup = _controlled_path(Path(backup_directory), create=False)
    root = backup.parent
    with BackupLock(root):
        verification = verify_backup_set(
            backup, expected_app_schema_version=expected_app_schema_version, now=now
        )
        if not verification.trusted:
            raise RestoreRefused("backup is not trusted")
        destination = Path(staging_destination).expanduser().absolute()
        if destination.exists() or destination.is_symlink():
            raise RestoreRefused(f"restore destination already exists: {destination}")
        _controlled_path(destination.parent, create=True)
        stage = destination.with_name(f".{destination.name}.{os.getpid()}.staging")
        if stage.exists() or stage.is_symlink():
            raise RestoreRefused(f"restore staging already exists: {stage}")
        stage.mkdir(parents=False)
        _restrict_mode(stage, 0o700)
        marker_root = Path(runtime_root).expanduser().absolute()
        if marker_root == destination:
            marker_write_root = stage
        else:
            marker_write_root = _controlled_path(marker_root, create=True)
        marker = None
        shadow_marker = None
        research_marker = None
        try:
            classifications = {
                str(entry["classification"]) for entry in verification.manifest["entries"]
            }
            for entry in verification.manifest["entries"]:
                if entry.get("source_status", "PRESENT") == "OPTIONAL_NOT_PRESENT":
                    continue
                logical_name = str(entry["logical_name"])
                target_name = _RESTORE_FILENAMES.get(logical_name)
                if target_name is None:
                    target_name = (
                        _safe_relative(logical_name) + ".sqlite3"
                        if entry["source_type"] == "sqlite"
                        else _safe_relative(logical_name) + ".bin"
                    )
                source = backup / _safe_relative(str(entry["backup_filename"]))
                target = stage / _safe_relative(target_name)
                if source.is_symlink() or not source.is_file():
                    raise PathUnsafe(str(source))
                shutil.copyfile(source, target)
                _restrict_mode(target, 0o600)
                _fsync_file(target)
                if sha256_file(target) != entry["sha256"]:
                    raise HashMismatch(target.name)
                if entry["source_type"] == "sqlite":
                    _check_sqlite(target)
            _fsync_directory(stage)
            if "AUTHORITATIVE_TRADING_STATE" in classifications:
                marker = write_recovery_marker(
                    marker_write_root,
                    backup_id=verification.backup_id,
                    restored_app_schema_version=verification.app_schema_version,
                    source_identity=str(verification.manifest["source_identity"]),
                    restored_at=now,
                )
            if "AUTHORITATIVE_SHADOW_STATE" in classifications:
                shadow_marker = write_recovery_marker(
                    marker_write_root,
                    backup_id=verification.backup_id,
                    restored_app_schema_version=verification.app_schema_version,
                    source_identity=str(verification.manifest["source_identity"]),
                    restored_at=now,
                    shadow=True,
                )
            if "AUTHORITATIVE_RESEARCH_GOVERNANCE" in classifications:
                research_marker = write_recovery_marker(
                    marker_write_root,
                    backup_id=verification.backup_id,
                    restored_app_schema_version=verification.app_schema_version,
                    source_identity=str(verification.manifest["source_identity"]),
                    restored_at=now,
                    research=True,
                )
            _fsync_directory(marker_write_root)
            if os.stat(stage).st_dev != os.stat(destination.parent).st_dev:
                raise RestoreRefused("restore staging and destination must share a filesystem")
            os.replace(stage, destination)
            _fsync_directory(destination.parent)
        except Exception:
            if stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage)
            raise
        if marker_root == destination:
            if marker is not None:
                marker = destination / TRADING_RECOVERY_MARKER
            if shadow_marker is not None:
                shadow_marker = destination / SHADOW_RECOVERY_MARKER
            if research_marker is not None:
                research_marker = destination / RESEARCH_RECOVERY_MARKER
        return RestoreResult(
            backup_id=verification.backup_id,
            staging_path=destination,
            recovery_marker=marker,
            research_recovery_marker=research_marker,
            migration_required=verification.app_schema_version is not None
            and verification.app_schema_version < expected_app_schema_version,
        )


def record_restore_drill(
    record_root: str | Path,
    *,
    drill_id: str,
    backup_id: str,
    started_at: datetime,
    completed_at: datetime,
    result: str,
    integrity: str,
    application_open_test: str,
    recovery_gate_present: bool,
) -> Path:
    """Write a non-secret restore-drill record outside the immutable set."""

    safe_id = _safe_relative(drill_id)
    if not safe_id.endswith(".json"):
        safe_id += ".json"
    root = Path(record_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"restore-drill-{safe_id}"
    payload = {
        "drill_schema_version": 1,
        "drill_id": drill_id,
        "backup_id": backup_id,
        "started_at_utc": _iso(_utc(started_at)),
        "completed_at_utc": _iso(_utc(completed_at)),
        "result": result,
        "integrity": integrity,
        "application_open_test": application_open_test,
        "recovery_gate_present": bool(recovery_gate_present),
    }
    _write_marker(path, payload)
    return path


def backup_freshness(
    created_at: datetime,
    *,
    last_verified_at: datetime | None,
    now: datetime,
    fresh_after_seconds: float,
    stale_after_seconds: float,
) -> str:
    current = _utc(now)
    reference = _utc(last_verified_at or created_at)
    age = (current - reference).total_seconds()
    if age < 0:
        return "UNKNOWN"
    if age <= fresh_after_seconds:
        return "FRESH_BACKUP"
    if age <= stale_after_seconds:
        return "STALE_BACKUP"
    return "UNKNOWN"


__all__ = [
    "BackupBusy",
    "BackupError",
    "BackupIntegrityError",
    "BackupSource",
    "BackupVerification",
    "ClockSkew",
    "InsufficientDisk",
    "ManifestInvalid",
    "PathUnsafe",
    "RecoveryRequired",
    "ResearchRecoveryRequired",
    "RestoreRefused",
    "ExportRefused",
    "RestoreResult",
    "RetentionPolicy",
    "SchemaIncompatible",
    "SourceUnavailable",
    "WritersNotQuiesced",
    "DB_WRITER_SERVICES",
    "DB_WRITER_TIMERS",
    "advance_recovery_marker",
    "assert_research_recovery_clear",
    "assert_shadow_recovery_clear",
    "assert_writer_recovery_clear",
    "backup_freshness",
    "create_backup_set",
    "latest_valid_backup",
    "list_backup_sets",
    "prune_backup_sets",
    "record_restore_drill",
    "require_writer_quiescence",
    "restore_to_staging",
    "sha256_file",
    "verify_backup_set",
    "export_verified_backup_set",
    "write_recovery_marker",
]
