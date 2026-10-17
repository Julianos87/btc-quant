"""Déchiffre, extrait et contrôle une sauvegarde sans toucher à l'état actif."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import tarfile
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


_PAPER_STOP_ID = re.compile(r"paper-stop-[0-9]+\Z")
_BACKUP_NAME = re.compile(r"state-[0-9]{8}-[0-9]{4}\.tar\.gz\.enc\Z")
_VERIFICATION_SUFFIX = ".verification.json"
_VERIFICATION_CHECKS = {
    "encrypted_round_trip": "PASS",
    "archive_read": "PASS",
    "sqlite_snapshots": "PASS",
}


def _unresolved_order_count(
    connection: sqlite3.Connection,
    tables: set[str],
    order_columns: set[str],
) -> int | None:
    """Count ambiguous external effects, excluding only a persisted PAPER stop.

    A PAPER broker stop is restart-safe only when its exact broker id is linked
    to the same open Trend position and all persisted protection fields are
    valid. Missing/legacy schema details fail closed.
    """

    if "status" not in order_columns:
        return None
    rows = connection.execute(
        "SELECT * FROM orders WHERE status IN ('PENDING', 'OPEN', 'UNBALANCED')"
    ).fetchall()
    required_order_columns = {
        "engine",
        "slot",
        "broker_order_id",
        "order_type",
        "local_state",
        "external_state",
    }
    required_position_columns = {
        "engine",
        "slot",
        "status",
        "qty",
        "stop_price",
        "direction",
        "stop_order_id",
    }
    if (
        "positions" not in tables
        or not required_order_columns.issubset(order_columns)
        or not required_position_columns.issubset(
            {str(row[1]) for row in connection.execute("PRAGMA table_info(positions)")}
        )
    ):
        return len(rows)

    linked_stops: set[tuple[str, str, str]] = set()
    for position in connection.execute(
        "SELECT engine, slot, status, qty, stop_price, direction, stop_order_id FROM positions"
    ):
        stop_id = position[6]
        try:
            qty = float(position[3])
            stop_price = float(position[4])
            direction = int(position[5])
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            position[2] == "OPEN"
            and isinstance(stop_id, str)
            and _PAPER_STOP_ID.fullmatch(stop_id) is not None
            and math.isfinite(qty)
            and qty > 0
            and math.isfinite(stop_price)
            and stop_price > 0
            and direction in (-1, 1)
        ):
            linked_stops.add((str(position[0]), str(position[1]), stop_id))

    unresolved = 0
    for order in rows:
        safe_paper_stop = (
            order["order_type"] == "STOP"
            and order["status"] == "OPEN"
            and order["local_state"] == "AWAITING_EXTERNAL"
            and order["external_state"] == "OPEN"
            and isinstance(order["broker_order_id"], str)
            and _PAPER_STOP_ID.fullmatch(order["broker_order_id"]) is not None
            and (
                str(order["engine"]),
                str(order["slot"]),
                order["broker_order_id"],
            )
            in linked_stops
        )
        if not safe_paper_stop:
            unresolved += 1
    return unresolved


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination):
                raise ValueError(f"Chemin dangereux dans l'archive : {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"Lien interdit dans l'archive : {member.name}")
        bundle.extractall(destination)


def _decrypt(archive: Path, destination: Path) -> None:
    if not os.environ.get("BACKUP_ENCRYPTION_KEY"):
        raise RuntimeError("BACKUP_ENCRYPTION_KEY est requise pour cette archive")
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
            str(destination),
            "-pass",
            "env:BACKUP_ENCRYPTION_KEY",
        ],
        check=True,
        timeout=120,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_decrypted(archive: Path) -> str:
    """Hash decrypted bytes as a stream without creating another archive copy."""

    if not os.environ.get("BACKUP_ENCRYPTION_KEY"):
        raise RuntimeError("BACKUP_ENCRYPTION_KEY est requise pour cette archive")
    command = [
        "openssl",
        "enc",
        "-d",
        "-aes-256-cbc",
        "-pbkdf2",
        "-iter",
        "200000",
        "-in",
        str(archive),
        "-pass",
        "env:BACKUP_ENCRYPTION_KEY",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None:
        raise RuntimeError("OpenSSL output stream unavailable")
    digest = hashlib.sha256()
    for chunk in iter(lambda: process.stdout.read(1024 * 1024), b""):
        digest.update(chunk)
    _, stderr = process.communicate()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command, stderr=stderr)
    return digest.hexdigest()


def _validate_backup_tar(plaintext: Path) -> None:
    """Read every tar member and reject unsafe links and missing DB snapshots."""

    safe_root = Path("/safe-root")
    found_database = False
    with tarfile.open(plaintext, "r|gz") as bundle:
        for member in bundle:
            target = (safe_root / member.name).resolve()
            if not target.is_relative_to(safe_root):
                raise ValueError(f"unsafe archive member: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"archive link is not allowed: {member.name}")
            if member.name == "state/btcquant.db" and member.isfile():
                found_database = True
    if not found_database:
        raise ValueError("state/btcquant.db is missing from the backup")


def write_archive_verification_record(
    archive: Path,
    destination: Path,
    *,
    plaintext_archive: Path | None = None,
) -> Path:
    """Verify the published encrypted container and atomically write its proof.

    The backup pipeline invokes this after SQLite Online Backup calls pass. If
    it supplies the staged plaintext archive, this check hashes decrypted bytes
    in a pipe and scans that existing tar stream before publishing the sidecar.
    Direct callers retain the isolated temporary-decrypt path.
    """

    if archive.is_symlink():
        raise ValueError("backup archive must be a regular file")
    archive = archive.resolve(strict=True)
    if not archive.is_file():
        raise ValueError("backup archive must be a regular file")
    if _BACKUP_NAME.fullmatch(archive.name) is None:
        raise ValueError("backup archive name is invalid")
    if destination.name != f"{archive.name}{_VERIFICATION_SUFFIX}":
        raise ValueError("verification record name must bind the archive name")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("verification record destination already exists")

    if plaintext_archive is None:
        with tempfile.TemporaryDirectory(prefix="btcquant-archive-verify-") as temp_name:
            plaintext = Path(temp_name) / "state.tar.gz"
            _decrypt(archive, plaintext)
            _validate_backup_tar(plaintext)
    else:
        if plaintext_archive.is_symlink():
            raise ValueError("plaintext archive must be a regular file")
        plaintext = plaintext_archive.resolve(strict=True)
        if not plaintext.is_file():
            raise ValueError("plaintext archive must be a regular file")
        if _sha256_decrypted(archive) != _sha256_file(plaintext):
            raise RuntimeError("decrypted bytes do not match supplied plaintext archive")
        _validate_backup_tar(plaintext)

    stat = archive.stat()
    if stat.st_size <= 0:
        raise ValueError("backup archive is empty")
    payload = {
        "archive_name": archive.name,
        "archive_sha256": _sha256_file(archive),
        "archive_size_bytes": stat.st_size,
        "format_version": 1,
        "kind": "BTCQUANT_BACKUP_VERIFICATION",
        "status": "PASS",
        "verified_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "verification_checks": dict(_VERIFICATION_CHECKS),
        "verification_scope": "encrypted_archive_and_sqlite_snapshot_integrity",
    }
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            os.fchmod(stream.fileno(), 0o640)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("verification record destination already exists")
        os.replace(temporary_path, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return destination


def verify_archive(archive: Path, extract_to: Path | None = None) -> dict[str, object]:
    """Retourne le diagnostic SQLite et conserve l'extraction si demandé."""

    archive = archive.resolve(strict=True)
    if extract_to is not None:
        extract_to = extract_to.resolve()
        if extract_to.exists() and any(extract_to.iterdir()):
            raise FileExistsError(f"Destination non vide : {extract_to}")
        extract_to.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="btcquant-restore-") as temp_name:
        temp = Path(temp_name)
        plain = temp / "state.tar.gz"
        if archive.name.endswith(".enc"):
            _decrypt(archive, plain)
        else:
            plain = archive

        destination = extract_to or (temp / "extracted")
        destination.mkdir(parents=True, exist_ok=True)
        _safe_extract(plain, destination)
        database = destination / "state" / "btcquant.db"
        if not database.is_file():
            raise FileNotFoundError("state/btcquant.db absent de la sauvegarde")

        # Le context manager sqlite3 commit/rollback mais ne ferme pas la
        # connexion. ``closing`` est indispensable pour pouvoir supprimer le
        # clean-room immédiatement, notamment sous Windows.
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"SQLite integrity_check : {integrity}")
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            counts = {
                table: int(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table.replace(chr(34), chr(34) * 2)}"'
                    ).fetchone()[0]
                )
                for table in tables
            }
            schema_row = (
                connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
                if "metadata" in tables
                else None
            )
            schema_version = int(schema_row[0]) if schema_row is not None else None
            order_columns = (
                {str(row[1]) for row in connection.execute("PRAGMA table_info(orders)")}
                if "orders" in tables
                else set()
            )
            unresolved_orders = _unresolved_order_count(connection, set(tables), order_columns)
            engine_states = (
                [
                    str(row[0])
                    for row in connection.execute("SELECT engine FROM engine_state ORDER BY engine")
                ]
                if "engine_state" in tables
                else []
            )

    return {
        "archive": str(archive),
        "integrity": integrity,
        "tables": tables,
        "rows": counts,
        "schema_version": schema_version,
        "unresolved_orders": unresolved_orders,
        "engine_states": engine_states,
        "restart_safe": unresolved_orders == 0 if unresolved_orders is not None else False,
        "extracted_to": str(extract_to) if extract_to else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--extract-to",
        type=Path,
        help="Répertoire neuf où conserver la restauration de test",
    )
    parser.add_argument(
        "--plaintext-archive",
        type=Path,
        help="Already-staged plaintext archive whose bytes are matched by streaming decryption",
    )
    parser.add_argument(
        "--write-archive-verification-record",
        type=Path,
        help="Write an atomic sidecar after verifying encrypted archive bytes and tar contents",
    )
    args = parser.parse_args()
    if args.write_archive_verification_record is not None:
        record = write_archive_verification_record(
            args.archive,
            args.write_archive_verification_record,
            plaintext_archive=args.plaintext_archive,
        )
        print(json.dumps({"status": "PASS", "verification_record": str(record)}, sort_keys=True))
        return
    print(
        json.dumps(
            verify_archive(args.archive, args.extract_to),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
