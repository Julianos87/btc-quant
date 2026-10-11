#!/usr/bin/env python3
"""Validate and durably register the frozen Carry reserve/duration protocol."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from btcquant.research.governance import ExperimentSpec
from btcquant.research.search_gates import validate_search_ready
from btcquant.research.governance_store import GovernanceStore


def register(spec_path: Path, database_path: Path) -> dict[str, object]:
    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("protocol JSON must contain one object")
    spec = ExperimentSpec.from_dict(payload)
    validate_search_ready(spec)
    with GovernanceStore(database_path) as store:
        if not store.path_is_explicit:
            raise ValueError("governance database path must be explicit")
        fingerprint = store.register_experiment(spec)
        stored = store.get_experiment_spec(spec.experiment_id)
        if stored.fingerprint != fingerprint:
            raise RuntimeError("durable experiment fingerprint verification failed")
    return {
        "status": "REGISTERED_NO_TRIALS",
        "selection_started": False,
        "experiment_id": spec.experiment_id,
        "fingerprint": fingerprint,
        "database": str(database_path.resolve()),
        "protocol": str(spec_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=ROOT / "audit/carry_rework_protocol.json")
    parser.add_argument(
        "--database", type=Path, default=ROOT / "audit/carry_rework_governance.sqlite3"
    )
    args = parser.parse_args()
    print(json.dumps(register(args.spec, args.database), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
