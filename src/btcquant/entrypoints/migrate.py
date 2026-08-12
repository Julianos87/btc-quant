"""Migration SQLite explicitement autorisée, avec backup vérifié."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from btcquant.deployment import (
    backup_sqlite_database,
    checkpoint_sqlite_wal,
    inspect_sqlite,
    open_database_handle_failures,
    systemd_writer_quiescence_failures,
)
from btcquant.execution.state_store import SCHEMA_VERSION, StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--target-git-sha", required=True)
    parser.add_argument(
        "--confirm-migration",
        action="store_true",
        help="autorise explicitement l'ouverture migrante de la base",
    )
    parser.add_argument(
        "--require-quiescence",
        action="store_true",
        help="exige la preuve systemd que tous les writers/timers sont arrêtés",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    before = inspect_sqlite(args.database)
    if before.metadata_schema_version is None:
        print("Refus: la version metadata de la base est inconnue.", file=sys.stderr)
        return 2
    if before.metadata_schema_version > SCHEMA_VERSION:
        print("Refus: la base est plus récente que le code.", file=sys.stderr)
        return 2
    if before.metadata_schema_version == SCHEMA_VERSION:
        print(f"Schéma déjà à jour: {SCHEMA_VERSION}")
        return 0
    if not args.confirm_migration:
        print(
            "Refus: migration requise mais non autorisée; "
            "utiliser --confirm-migration après arrêt des writers.",
            file=sys.stderr,
        )
        return 3
    production_database = args.database.resolve().as_posix().startswith("/opt/btcquant/state/")
    if args.require_quiescence or production_database:
        failures = systemd_writer_quiescence_failures()
        if failures:
            print(
                "MIGRATION_REFUSED: quiescence non prouvée: " + "; ".join(failures),
                file=sys.stderr,
            )
            return 4
        handle_failures = open_database_handle_failures(
            (args.database, Path(f"{args.database}-wal"), Path(f"{args.database}-shm"))
        )
        if handle_failures:
            print(
                "MIGRATION_REFUSED: descripteur DB/WAL/SHM encore ouvert: "
                + "; ".join(handle_failures),
                file=sys.stderr,
            )
            return 4
    try:
        checkpoint_sqlite_wal(args.database)
        backup = backup_sqlite_database(
            args.database, args.backup, target_git_sha=args.target_git_sha
        )
    except Exception as error:
        print(f"Migration refusée avant backup; base inchangée: {error}", file=sys.stderr)
        return 1
    try:
        StateStore(args.database, allow_migration=True)
        after = inspect_sqlite(args.database)
        if after.metadata_schema_version != SCHEMA_VERSION or after.integrity_check != "ok":
            raise RuntimeError("Validation post-migration invalide")
    except Exception as error:
        print(
            f"Migration échouée; backup conservé pour récupération: {args.backup}: {error}",
            file=sys.stderr,
        )
        return 1
    print(
        f"Migration validée: app_schema_version {before.metadata_schema_version} -> "
        f"{after.metadata_schema_version}; sqlite_schema_cookie={after.pragma_schema_version}; "
        f"backup_sha256={backup['backup_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
