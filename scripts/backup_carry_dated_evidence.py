"""Encrypt and restore immutable public Carry-dated archive segments once."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path

from btcquant.research.carry_dated_collector import verify_archive


def _reject_symlink_components(path: Path) -> None:
    current = path.expanduser().absolute()
    while True:
        if current.is_symlink():
            raise RuntimeError(f"Refusing evidence path through symlink: {current}")
        if current == current.parent:
            return
        current = current.parent


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decrypted_sha256(encrypted: Path, *, key_env: str) -> str:
    process = subprocess.Popen(
        [
            "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
            "-in", str(encrypted), "-pass", f"env:{key_env}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdout = process.stdout
    if stdout is None:
        raise RuntimeError("could not read decrypted evidence stream")
    digest = hashlib.sha256()
    for block in iter(lambda: stdout.read(1024 * 1024), b""):
        digest.update(block)
    stdout.close()
    if process.wait() != 0:
        raise RuntimeError(f"encrypted evidence failed decryption: {encrypted.name}")
    return digest.hexdigest()


def _encrypt_verified(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    key_env: str,
    min_free_bytes: int,
) -> bool:
    _reject_symlink_components(source)
    _reject_symlink_components(destination.parent)
    if destination.is_symlink():
        raise RuntimeError(f"Refusing evidence destination symlink: {destination.name}")
    if not source.is_file():
        raise FileNotFoundError(f"evidence source is missing: {source.name}")
    if destination.exists():
        if _decrypted_sha256(destination, key_env=key_env) != expected_sha256:
            raise RuntimeError(f"existing encrypted evidence mismatch: {destination.name}")
        return False
    needed = source.stat().st_size + 1024 * 1024 + min_free_bytes
    if shutil.disk_usage(destination.parent).free < needed:
        raise OSError(
            f"evidence backup would breach free-space reserve before {source.name}"
        )
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    if source.is_symlink() or destination.is_symlink() or temporary.exists():
        raise RuntimeError("evidence paths must be regular files")
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as output:
            subprocess.run(
                [
                    "openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                    "-salt", "-pass", f"env:{key_env}",
                ],
                stdin=source_handle,
                stdout=output,
                stderr=subprocess.PIPE,
                check=True,
            )
            output.flush()
            os.fsync(output.fileno())
        if _decrypted_sha256(temporary, key_env=key_env) != expected_sha256:
            raise RuntimeError(f"encrypted evidence verification failed: {source.name}")
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return True


def backup_evidence(
    source_dir: Path,
    destination_dir: Path,
    *,
    key_env: str = "BACKUP_ENCRYPTION_KEY",
    min_free_bytes: int = 2_684_354_560,
) -> dict[str, int]:
    """Encrypt each immutable segment and manifest only once; never purge."""

    _reject_symlink_components(source_dir)
    _reject_symlink_components(destination_dir)
    if not source_dir.exists():
        return {"segments_added": 0, "bytes_added": 0}
    destination_dir.mkdir(parents=True, exist_ok=True)
    added = 0
    bytes_added = 0
    segments_added = 0
    for manifest_path in sorted(source_dir.glob("*.manifest.json")):
        _reject_symlink_components(manifest_path)
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        archive_name = manifest.get("archive_file")
        if not isinstance(archive_name, str) or Path(archive_name).name != archive_name:
            raise ValueError(f"invalid archive filename in {manifest_path.name}")
        archive_path = source_dir / archive_name
        _reject_symlink_components(archive_path)
        segment_destination = destination_dir / f"{archive_name}.enc"
        manifest_destination = destination_dir / f"{manifest_path.name}.enc"
        segment_exists = segment_destination.exists()
        manifest_exists = manifest_destination.exists()
        segment_added = False
        manifest_added = False
        if not (segment_exists and manifest_exists):
            verify_archive(manifest_path)
        expected_archive_hash = manifest.get("archive_sha256")
        if not isinstance(expected_archive_hash, str) or len(expected_archive_hash) != 64:
            raise ValueError(f"invalid archive hash in {manifest_path.name}")
        if not segment_exists and _sha256_file(archive_path) != expected_archive_hash:
            raise ValueError(f"source archive hash mismatch: {archive_path.name}")
        if _encrypt_verified(
            archive_path,
            segment_destination,
            expected_sha256=expected_archive_hash,
            key_env=key_env,
            min_free_bytes=min_free_bytes,
        ):
            segment_added = True
            added += 1
            bytes_added += segment_destination.stat().st_size
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if _encrypt_verified(
            manifest_path,
            manifest_destination,
            expected_sha256=manifest_hash,
            key_env=key_env,
            min_free_bytes=min_free_bytes,
        ):
            manifest_added = True
            added += 1
            bytes_added += manifest_destination.stat().st_size
        segments_added += int(segment_added or manifest_added)
    return {"segments_added": segments_added, "files_added": added, "bytes_added": bytes_added}


def restore_evidence(
    source_dir: Path,
    destination_dir: Path,
    *,
    key_env: str = "BACKUP_ENCRYPTION_KEY",
    restore_database: Path | None = None,
) -> dict[str, int]:
    """Decrypt into a new clean-room directory and verify every segment."""

    _reject_symlink_components(source_dir)
    _reject_symlink_components(destination_dir.parent)
    if destination_dir.exists() or destination_dir.is_symlink():
        raise FileExistsError(f"restore destination already exists: {destination_dir}")
    restore_db_temporary: Path | None = None
    if restore_database is not None:
        _reject_symlink_components(restore_database.parent)
        if restore_database.exists() or restore_database.is_symlink():
            raise FileExistsError(f"restore database already exists: {restore_database}")
        restore_database.parent.mkdir(parents=True, exist_ok=True)
        restore_db_temporary = restore_database.with_name(
            f".{restore_database.name}.{uuid.uuid4().hex}.tmp"
        )
    temporary_dir = destination_dir.with_name(f".{destination_dir.name}.{uuid.uuid4().hex}.tmp")
    temporary_dir.mkdir(parents=False)
    restored_files = 0
    try:
        encrypted_files = sorted(source_dir.glob("*.enc"))
        if not encrypted_files:
            raise ValueError("no encrypted evidence files were found")
        for encrypted in encrypted_files:
            _reject_symlink_components(encrypted)
            if encrypted.name.endswith(".manifest.json.enc"):
                target_name = encrypted.name[: -len(".enc")]
            elif encrypted.name.endswith(".jsonl.gz.enc"):
                target_name = encrypted.name[: -len(".enc")]
            else:
                raise ValueError(f"unexpected encrypted evidence file: {encrypted.name}")
            target = temporary_dir / target_name
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            process = subprocess.run(
                [
                    "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                    "-in", str(encrypted), "-out", str(temporary), "-pass", f"env:{key_env}",
                ],
                stderr=subprocess.PIPE,
                check=True,
            )
            del process
            if target.name.endswith(".manifest.json"):
                manifest = json.loads(temporary.read_text(encoding="utf-8"))
                archive_name = manifest.get("archive_file")
                if not isinstance(archive_name, str) or Path(archive_name).name != archive_name:
                    raise ValueError("restored archive manifest has an unsafe filename")
            os.replace(temporary, target)
            restored_files += 1
        archives = {path.name for path in temporary_dir.glob("*.jsonl.gz")}
        manifests = sorted(temporary_dir.glob("*.manifest.json"))
        if not manifests:
            raise ValueError("no archived evidence manifests were restored")
        expected_archives: set[str] = set()
        for manifest_path in manifests:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            archive_name = manifest.get("archive_file")
            if not isinstance(archive_name, str) or Path(archive_name).name != archive_name:
                raise ValueError("restored archive manifest has an unsafe filename")
            if archive_name in expected_archives:
                raise ValueError("duplicate archive filename in evidence manifests")
            expected_archives.add(archive_name)
        if archives != expected_archives:
            raise ValueError("encrypted evidence archive/manifest set is incomplete")
        records = 0
        for manifest in manifests:
            report = verify_archive(manifest)
            records += int(report["record_count"])
            if restore_db_temporary is not None:
                report = verify_archive(manifest, restore_database=restore_db_temporary)
                if report["status"] != "PASS":
                    raise RuntimeError("clean-room database evidence restore failed")
        os.replace(temporary_dir, destination_dir)
        if restore_db_temporary is not None and restore_database is not None:
            with sqlite3.connect(restore_db_temporary) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise RuntimeError("clean-room evidence database failed integrity_check")
            os.replace(restore_db_temporary, restore_database)
        return {
            "segments_restored": len(manifests),
            "records_restored": records,
            "files_restored": restored_files,
        }
    except BaseException:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        if restore_db_temporary is not None:
            restore_db_temporary.unlink(missing_ok=True)
            Path(str(restore_db_temporary) + "-wal").unlink(missing_ok=True)
            Path(str(restore_db_temporary) + "-shm").unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--restore-database", type=Path)
    parser.add_argument("--min-free-bytes", type=int, default=2_684_354_560)
    arguments = parser.parse_args()
    if arguments.restore:
        result = restore_evidence(
            arguments.source,
            arguments.destination,
            restore_database=arguments.restore_database,
        )
    else:
        result = backup_evidence(
            arguments.source,
            arguments.destination,
            min_free_bytes=arguments.min_free_bytes,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
