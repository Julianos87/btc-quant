"""Crée une sauvegarde SQLite cohérente via l'API de backup en ligne."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.source) as source:
        with sqlite3.connect(args.destination) as destination:
            source.backup(destination)


if __name__ == "__main__":
    main()
