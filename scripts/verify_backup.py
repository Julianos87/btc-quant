"""Déchiffre, extrait et contrôle une sauvegarde sans toucher à l'état actif."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path


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
            unresolved_orders = (
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM orders "
                        "WHERE status IN ('PENDING', 'OPEN', 'UNBALANCED')"
                    ).fetchone()[0]
                )
                if "status" in order_columns
                else None
            )
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
    args = parser.parse_args()
    print(
        json.dumps(
            verify_archive(args.archive, args.extract_to),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
