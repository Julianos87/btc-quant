"""Cutover explicite d'un Carry paper synthétique legacy vers v6 FLAT.

Usage diagnostique (aucune écriture) :

    python -m btcquant.entrypoints.carry_cutover \\
        --database /path/to/copy.db \\
        --config environments/paper/config.yaml \\
        --print-expected-state-sha256

Usage mutateur (copie hors production, Carry arrêté) :

    python -m btcquant.entrypoints.carry_cutover \\
        --database /path/to/copy.db \\
        --config environments/paper/config.yaml \\
        --expected-state-sha256 <hash> \\
        --git-sha <40hex> \\
        --confirm-legacy-synthetic-cutover
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from btcquant.execution.carry_cutover import (
    CUTOVER_APPLIED,
    NO_OP_ALREADY_CUT_OVER,
    CutoverRefused,
    apply_legacy_synthetic_carry_cutover,
    diagnose_legacy_synthetic_pattern,
    read_carry_state_sha256,
    require_paper_carry_config,
    require_schema_6,
)
from btcquant.execution.state_store import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expected-state-sha256", default=None)
    parser.add_argument("--git-sha", default=None)
    parser.add_argument(
        "--confirm-legacy-synthetic-cutover",
        action="store_true",
        help="autorise explicitement la réécriture du checkpoint Carry",
    )
    parser.add_argument(
        "--print-expected-state-sha256",
        action="store_true",
        help="affiche le hash canonique et le diagnostic, sans écrire",
    )
    return parser


def _diagnose(database: Path) -> int:
    require_schema_6(database)
    store = StateStore(database, allow_migration=False, read_only=True)
    payload = store.load_engine_state("carry")
    if payload is None:
        print("CUTOVER_BLOCKED: engine_state carry absent", file=sys.stderr)
        return 2
    digest = read_carry_state_sha256(database)
    failures = diagnose_legacy_synthetic_pattern(payload)
    print(f"carry_state_sha256={digest}")
    if failures:
        print("pattern=REFUSE " + "; ".join(failures))
        return 2
    print("pattern=LEGACY_SYNTHETIC_OPEN_QTY0")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_paper_carry_config(args.config)
        if args.print_expected_state_sha256:
            return _diagnose(args.database)
        if not args.confirm_legacy_synthetic_cutover:
            print(
                "CUTOVER_BLOCKED: confirmation absente ; "
                "utiliser --confirm-legacy-synthetic-cutover après arrêt Carry",
                file=sys.stderr,
            )
            return 3
        if not args.expected_state_sha256:
            print("CUTOVER_BLOCKED: --expected-state-sha256 requis", file=sys.stderr)
            return 3
        if not args.git_sha:
            print("CUTOVER_BLOCKED: --git-sha requis", file=sys.stderr)
            return 3
        result = apply_legacy_synthetic_carry_cutover(
            args.database,
            expected_state_sha256=args.expected_state_sha256,
            git_sha=args.git_sha,
        )
    except CutoverRefused as error:
        print(str(error), file=sys.stderr)
        return 2
    print(result.status)
    print(f"old_state_sha256={result.old_state_sha256}")
    print(f"new_state_sha256={result.new_state_sha256}")
    print(f"equity={result.equity}")
    if result.cutover_timestamp_utc:
        print(f"cutover_timestamp_utc={result.cutover_timestamp_utc}")
    if result.status not in {CUTOVER_APPLIED, NO_OP_ALREADY_CUT_OVER}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
