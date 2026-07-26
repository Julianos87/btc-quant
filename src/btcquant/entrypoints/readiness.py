"""Pilote la campagne formelle de qualification paper → testnet."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from btcquant.execution.readiness import evaluate_readiness, finalize_campaign, start_campaign
from btcquant.execution.state_store import StateStore

ROOT = Path(os.environ.get("BTCQUANT_ROOT", Path.cwd())).resolve()
_CONSOLE_TRANSLATION: dict[int, str | int | None] = {
    ord("≥"): ">=",
    ord("≤"): "<=",
    ord("→"): "->",
}


def _console_text(value: object) -> str:
    """Garde la CLI lisible sur les consoles Windows CP1252."""

    return str(value).translate(_CONSOLE_TRANSLATION)


def _print_report(report: dict) -> None:
    print(
        f"Qualification #{report['campaign_id'] or '—'} "
        f"v{report['protocol_version']} : {report['status']} "
        f"({report['n_ok']}/{report['n_total']})"
    )
    for check in report["checks"]:
        marker = "PASS" if check["passed"] else "FAIL"
        print(
            f"  [{marker}] {_console_text(check['label'])}: "
            f"{_console_text(check['value'])} ({_console_text(check['target'])})"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "status", "finalize", "cancel"))
    parser.add_argument("--database", default="state/btcquant.db")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    store = StateStore(ROOT / args.database)
    if args.command == "start":
        started_campaign = start_campaign(store)
        print(f"Campagne #{started_campaign['id']} démarrée le {started_campaign['started_at']}.")
        return
    if args.command == "cancel":
        campaign = store.active_qualification_campaign()
        if campaign is None:
            sys.exit("Aucune campagne active.")
        store.finish_qualification_campaign(int(campaign["id"]), status="CANCELED")
        print(f"Campagne #{campaign['id']} annulée.")
        return
    try:
        report = (
            finalize_campaign(store)
            if args.command == "finalize"
            else evaluate_readiness(store, persist=True)
        )
    except RuntimeError as error:
        report = evaluate_readiness(store, persist=True)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_report(report)
            print(f"\nREFUS : {error}")
        raise SystemExit(2) from error
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    raise SystemExit(0 if report["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
