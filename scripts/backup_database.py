"""Crée une sauvegarde SQLite cohérente et validée via l'API backup."""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit(f"Source SQLite absente: {args.source}")
    if args.destination.exists():
        raise SystemExit(f"Destination déjà présente: {args.destination}")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.destination.with_name(f".{args.destination.name}.{os.getpid()}.tmp")
    try:
        with sqlite3.connect(readonly_uri(args.source), uri=True) as source:
            integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Source SQLite invalide: {integrity}")
            with sqlite3.connect(temporary) as destination:
                source.backup(destination, pages=256, sleep=0.1)
                result = destination.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError(f"Backup SQLite invalide: {result}")
        os.replace(temporary, args.destination)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


if __name__ == "__main__":
    main()
