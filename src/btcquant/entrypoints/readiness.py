"""Pilote la campagne formelle de qualification paper → testnet."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from btcquant.console import enable_utf8_output
from btcquant.execution.readiness import (
    evaluate_readiness,
    finalize_campaign,
    start_campaign,
    testnet_p1_policy,
)
from btcquant.execution.state_store import StateStore
from btcquant.execution.testnet_preflight import evaluate_testnet_preflight

ROOT = Path(os.environ.get("BTCQUANT_ROOT", Path.cwd())).resolve()


def _print_report(report: dict) -> None:
    print(
        f"Qualification #{report['campaign_id'] or '—'} "
        f"v{report['protocol_version']} : {report['status']} "
        f"({report['n_ok']}/{report['n_total']})"
    )
    for check in report["checks"]:
        marker = "PASS" if check["passed"] else "FAIL"
        print(f"  [{marker}] {check['label']}: {check['value']} ({check['target']})")


def main() -> None:
    enable_utf8_output()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("start", "status", "finalize", "cancel", "preflight-testnet")
    )
    parser.add_argument("--database", default="state/btcquant.db")
    parser.add_argument(
        "--profile",
        choices=("paper", "testnet-p1"),
        default="paper",
        help="Politique immuable appliquée uniquement lors de start.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "preflight-testnet":
        report = evaluate_testnet_preflight(ROOT)
        if args.json:
            print(json.dumps(report, ensure_ascii=True, indent=2))
        else:
            print(f"Testnet preflight : {report['status']}")
            for check in report["checks"]:
                marker = "PASS" if check["passed"] else "FAIL"
                print(f"  [{marker}] {check['key']}: {check['value']} ({check['required']})")
            if report["reason_codes"]:
                print("  Reasons: " + ", ".join(report["reason_codes"]))
        raise SystemExit(0 if report["status"] == "PASS" else 2)

    store = StateStore(ROOT / args.database)
    if args.command == "start":
        policy = testnet_p1_policy() if args.profile == "testnet-p1" else None
        started_campaign = start_campaign(store, policy)
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
            print(json.dumps(report, ensure_ascii=True, indent=2))
        else:
            _print_report(report)
            print(f"\nREFUS : {error}")
        raise SystemExit(2) from error
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        _print_report(report)
    raise SystemExit(0 if report["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
