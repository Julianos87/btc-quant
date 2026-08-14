"""Crée une sauvegarde SQLite cohérente et validée via l'API backup."""

from __future__ import annotations

import argparse
from contextlib import closing
import os
import sqlite3
from pathlib import Path


def _reject_symlink_components(path: Path) -> None:
    current = path.expanduser().absolute()
    while True:
        if current.is_symlink():
            raise RuntimeError(f"Refus de suivre un symlink SQLite: {current}")
        if current == current.parent:
            break
        current = current.parent


def readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _check_integrity(connection: sqlite3.Connection, label: str) -> None:
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError(f"{label} SQLite integrity_check : {integrity}")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(
            f"{label} SQLite foreign_key_check : {len(foreign_key_errors)} violation(s)"
        )


def create_backup(source_path: Path, destination_path: Path) -> None:
    _reject_symlink_components(source_path)
    _reject_symlink_components(destination_path.parent)
    if destination_path.is_symlink():
        raise RuntimeError(f"Destination symlink refusee: {destination_path}")
    if not source_path.is_file():
        raise FileNotFoundError(f"Source SQLite absente: {source_path}")
    if destination_path.exists() or destination_path.is_symlink():
        raise FileExistsError(f"Destination déjà présente: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(f".{destination_path.name}.{os.getpid()}.tmp")
    try:
        with closing(sqlite3.connect(readonly_uri(source_path), uri=True)) as source:
            source.execute("PRAGMA query_only=ON")
            if source.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise RuntimeError("Source SQLite non read-only")
            _check_integrity(source, "Source")
            with closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination, pages=256, sleep=0.1)
                _check_integrity(destination, "Backup")
        destination_descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(destination_descriptor)
        finally:
            os.close(destination_descriptor)
        os.replace(temporary, destination_path)
        directory_descriptor = os.open(destination_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    create_backup(args.source, args.destination)


if __name__ == "__main__":
    main()
