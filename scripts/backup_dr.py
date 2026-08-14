#!/usr/bin/env python3
"""Explicit local backup/restore protocol entry point.

This command has no production defaults and never starts a service.  Sources
and destinations must be named explicitly.  The restore command only creates
a new staging directory and emits a recovery marker; it cannot replace a
runtime directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.backup import (
    BackupSource,
    RetentionPolicy,
    create_backup_set,
    list_backup_sets,
    prune_backup_sets,
    restore_to_staging,
    verify_backup_set,
)


def _source(value: str) -> BackupSource:
    try:
        logical, path, classification = (
            value.split("=", 1)[0],
            *value.split("=", 1)[1].split(":", 1),
        )
    except ValueError as error:
        raise argparse.ArgumentTypeError("source must be logical=path:CLASSIFICATION") from error
    return BackupSource(logical, Path(path), classification)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--destination-root", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create")
    _common(create)
    create.add_argument("--git-sha", required=True)
    create.add_argument("--schema-version", type=int, required=True)
    create.add_argument("--source", action="append", type=_source, required=True)
    create.add_argument("--source-identity", required=True)
    create.add_argument("--safety-margin-bytes", type=int, default=64 * 1024 * 1024)

    verify = commands.add_parser("verify")
    verify.add_argument("--backup-directory", type=Path, required=True)
    verify.add_argument("--schema-version", type=int)

    listing = commands.add_parser("list")
    _common(listing)

    prune = commands.add_parser("prune")
    _common(prune)
    prune.add_argument("--recent", type=int, default=24)
    prune.add_argument("--daily", type=int, default=7)
    prune.add_argument("--weekly", type=int, default=4)
    prune.add_argument("--monthly", type=int, default=12)

    restore = commands.add_parser("restore-to-staging")
    restore.add_argument("--backup-directory", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--runtime-root", type=Path, required=True)
    restore.add_argument("--schema-version", type=int, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "create":
        created_path = create_backup_set(
            args.source,
            args.destination_root,
            app_git_sha=args.git_sha,
            app_schema_version=args.schema_version,
            source_identity=args.source_identity,
            safety_margin_bytes=args.safety_margin_bytes,
        )
        print(created_path)
    elif args.command == "verify":
        verification = verify_backup_set(
            args.backup_directory,
            expected_app_schema_version=args.schema_version,
        )
        print(json.dumps(verification.manifest, indent=2, sort_keys=True))
    elif args.command == "list":
        print(
            json.dumps(
                [item.manifest for item in list_backup_sets(args.destination_root)], indent=2
            )
        )
    elif args.command == "prune":
        removed = prune_backup_sets(
            args.destination_root,
            RetentionPolicy(args.recent, args.daily, args.weekly, args.monthly),
        )
        print(json.dumps({"removed": removed}))
    elif args.command == "restore-to-staging":
        restored = restore_to_staging(
            args.backup_directory,
            args.destination,
            runtime_root=args.runtime_root,
            expected_app_schema_version=args.schema_version,
        )
        print(restored.staging_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
