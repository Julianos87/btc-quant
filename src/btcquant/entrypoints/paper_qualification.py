"""Inspect or record an evidence-driven PAPER technical qualification."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

from btcquant.console import enable_utf8_output
from btcquant.execution.paper_technical_qualification import (
    QualificationFailed,
    collect_paper_technical_evidence,
    record_paper_technical_evidence,
)


def main() -> None:
    enable_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "record"))
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("BTCQUANT_ROOT", "/opt/btcquant")),
    )
    args = parser.parse_args()
    try:
        evidence = collect_paper_technical_evidence(args.root)
        row_id = (
            record_paper_technical_evidence(args.root, evidence)
            if args.command == "record"
            else None
        )
    except (QualificationFailed, OSError, sqlite3.Error, ValueError) as error:
        reason = (
            error.reason if isinstance(error, QualificationFailed) else "EVIDENCE_COLLECTION_FAILED"
        )
        print(
            json.dumps(
                {"status": "FAIL", "reason": reason, "detail": str(error)},
                sort_keys=True,
            )
        )
        raise SystemExit(2) from error
    output = dict(evidence)
    output["qualification_record_id"] = row_id
    output["recorded"] = row_id is not None
    print(json.dumps(output, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
