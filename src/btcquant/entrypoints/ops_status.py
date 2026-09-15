"""Command-line entry point for the read-only operational status report."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from btcquant.operations.status import collect_status, render_human


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only BTCQuant operational status")
    parser.add_argument("--root", default=os.environ.get("BTCQUANT_ROOT", "/opt/btcquant"))
    parser.add_argument("--source-repository", default=None)
    parser.add_argument("--json", action="store_true", help="emit canonical JSON")
    args = parser.parse_args()
    report = collect_status(
        Path(args.root).resolve(),
        source_repository=Path(args.source_repository).resolve()
        if args.source_repository
        else None,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2))
    else:
        print(render_human(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
