"""Inspect/apply an auditable PAPER correction for funding after observed stops.

Original stop, trade and funding rows are preserved. Adjustments use distinct
funding-ledger identities and atomic checkpoints; rerunning cannot repay twice.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import sqlite3
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btcquant.config import load_config  # noqa: E402
from btcquant.execution.financial_application_plan import sha256_json  # noqa: E402
from btcquant.execution.instance_lock import EngineInstanceLock  # noqa: E402
from btcquant.execution.state_store import StateStore  # noqa: E402

PREFIX = "trend-stop-repair-v1|"


def exposure_at(timeline, timestamp):
    point = pd.Timestamp(timestamp)
    selected = None
    for item in timeline:
        if pd.Timestamp(item["effective_at"]) <= point:
            selected = item
    return selected


import pandas as pd  # noqa: E402


def build_plan(database: Path):
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN")
        state = json.loads(
            c.execute("SELECT payload FROM engine_state WHERE engine='trend'").fetchone()[0]
        )
        if state.get("reconciliation_required") or any(
            v["position"] is not None for v in state["slots"].values()
        ):
            raise ValueError("PAPER reconciliation requires flat slots and a clear engine")
        repaired = copy.deepcopy(state)
        stops = c.execute("""SELECT e.id, e.payload, o.slot, o.intent_id, t.payload AS trade_payload
            FROM events e JOIN orders o ON o.intent_id=e.correlation_id AND o.engine='trend'
            JOIN events t ON t.correlation_id=e.correlation_id AND t.event_type='trade_closed' AND t.engine='trend'
            WHERE e.engine='trend' AND e.event_type='external_stop_fill_observed' ORDER BY e.id""").fetchall()
        evidence = []
        for row in stops:
            event = json.loads(row["payload"])
            trade = json.loads(row["trade_payload"])
            after_state = event["state"]
            if event.get("state_sha256") != sha256_json(after_state):
                raise ValueError("Observed stop checkpoint hash mismatch")
            name = row["slot"]
            if name not in repaired["slots"]:
                raise ValueError("Unknown observed stop slot")
            timeline = repaired["slots"][name]["position_timeline"]
            if any(item["intent_id"] == row["intent_id"] for item in timeline):
                continue
            effective = trade["exit_ts"]
            previous = exposure_at(timeline, effective)
            if previous is None or float(previous["qty"]) == 0:
                raise ValueError("Stop has no proven preceding exposure")
            after_position = after_state["slots"][name]["position"]
            remaining = 0.0 if after_position is None else float(after_position["qty"])
            if not math.isclose(
                float(previous["qty"]) - float(event["filled_qty"]), remaining, abs_tol=1e-9
            ):
                raise ValueError("Stop quantity does not reconcile with its durable checkpoint")
            if not str(previous["generation"]).startswith("entry=" + trade["entry_ts"] + "|"):
                raise ValueError("Stop position generation mismatch")
            transition = {
                "effective_at": effective,
                "intent_id": row["intent_id"],
                "qty": remaining,
                "direction": int(previous["direction"]),
                "generation": previous["generation"],
            }
            timeline.append(transition)
            timeline.sort(key=lambda item: (item["effective_at"], item["intent_id"]))
            evidence.append(
                {
                    "source_event_id": row["id"],
                    "slot": name,
                    "transition": transition,
                    "source_state_sha256": event["state_sha256"],
                }
            )
        for name, slot in repaired["slots"].items():
            if slot["position_timeline"] and slot["position_timeline"][-1]["qty"] != 0:
                raise ValueError("Repaired timeline still disagrees with flat position: " + name)
        ledgers = {r["event_key"]: dict(r) for r in c.execute("SELECT * FROM funding_ledger")}
        payments = c.execute(
            "SELECT id,payload FROM events WHERE engine='trend' AND event_type='funding_payment' ORDER BY id"
        ).fetchall()
        adjustments = []
        for row in payments:
            event = json.loads(row["payload"])
            original_key = event.get("event_key", "")
            if not original_key.startswith("trend|"):
                continue
            original = ledgers.get(original_key)
            if original is None:
                raise ValueError("Funding event lacks its durable ledger")
            if not math.isclose(
                float(original["funding_pnl"]), float(event["funding_pnl"]), abs_tol=1e-12
            ):
                raise ValueError("Original funding event and ledger mismatch")
            amounts = {}
            for name, actual in event["amounts"].items():
                if name not in repaired["slots"]:
                    raise ValueError("Funding references unknown slot")
                qty = float(actual["qty"])
                amount = float(actual["amount"])
                reference = event["reference_price"]
                rate = float(event["native_funding_rate"])
                if qty and (
                    reference is None
                    or not math.isclose(
                        amount,
                        qty * int(actual["direction"]) * float(reference) * rate,
                        abs_tol=1e-12,
                    )
                ):
                    raise ValueError("Original per-slot funding amount is not reproducible")
                exposure = exposure_at(
                    repaired["slots"][name]["position_timeline"], event["payment_ts"]
                )
                expected_qty = 0.0 if exposure is None else float(exposure["qty"])
                expected = (
                    0.0
                    if expected_qty == 0
                    else expected_qty * int(exposure["direction"]) * float(reference) * rate
                )
                delta = amount - expected
                if abs(delta) > 1e-12:
                    amounts[name] = delta
            if not amounts:
                continue
            correction_key = PREFIX + original_key
            total = math.fsum(amounts.values())
            if correction_key in ledgers:
                if not math.isclose(
                    float(ledgers[correction_key]["funding_pnl"]), total, abs_tol=1e-12
                ):
                    raise ValueError("Existing correction amount mismatch")
                continue
            adjustments.append(
                {
                    "original_event_id": row["id"],
                    "original_event_key": original_key,
                    "event_key": correction_key,
                    "funding_timestamp": original["funding_timestamp"],
                    "venue": original["venue"],
                    "instrument": original["instrument"],
                    "amounts": amounts,
                    "funding_pnl": total,
                }
            )
        c.rollback()
    return {
        "state_before": state,
        "state_after_timelines": repaired,
        "state_before_sha256": sha256_json(state),
        "stop_evidence": evidence,
        "adjustments": adjustments,
        "cash_adjustments": {
            name: math.fsum(a["amounts"].get(name, 0) for a in adjustments)
            for name in state["slots"]
        },
    }


def apply_plan(database: Path, plan):
    store = StateStore(database, initialize=False)
    if sha256_json(store.load_engine_state("trend")) != plan["state_before_sha256"]:
        raise ValueError("Engine checkpoint changed since inspection")
    state = plan["state_after_timelines"]
    if plan["stop_evidence"]:
        store.save_engine_state(
            "trend",
            state,
            event_type="position_timeline_reconstructed",
            event_payload={
                "source": "observed_stop_fill_checkpoints",
                "evidence": plan["stop_evidence"],
                "state_before_sha256": plan["state_before_sha256"],
            },
        )
    for adjustment in plan["adjustments"]:
        for name, amount in adjustment["amounts"].items():
            state["slots"][name]["cash"] += amount
        ledger = {
            "event_key": adjustment["event_key"],
            "venue": adjustment["venue"],
            "instrument": adjustment["instrument"],
            "funding_timestamp": adjustment["funding_timestamp"],
            "native_funding_rate": 0.0,
            "position_generation": "CORRECTION:" + adjustment["original_event_key"],
            "funding_notional": 0.0,
            "funding_notional_price": None,
            "funding_notional_price_source": "PAPER_STOP_FUNDING_RECONCILIATION_V1",
            "funding_notional_price_timestamp": adjustment["funding_timestamp"],
            "funding_pnl": adjustment["funding_pnl"],
            "borrow_principal": 0.0,
            "borrow_rate_ann": 0.0,
            "borrow_dt_seconds": 0.0,
            "borrow_cost": 0.0,
            "applied_at": datetime.now(UTC).isoformat(),
        }
        result = store.apply_carry_accounting_event_and_checkpoint(
            ledger,
            state,
            engine="trend",
            event_payload={
                "reason": "historical_stop_funding_reconciliation",
                "event_key": adjustment["event_key"],
                "correction": adjustment,
            },
        )
        if result != "applied":
            raise ValueError("Correction changed since inspection")
    return build_plan(database)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if cfg["environment"] != "paper" or cfg["execution"]["mode"] != "paper":
        raise ValueError("This reconciliation is PAPER only")
    database = args.database.resolve(strict=True)
    if args.apply:
        with EngineInstanceLock(database, "trend"):
            plan = build_plan(database)
            after = apply_plan(database, plan)
            assert not after["adjustments"] and not after["stop_evidence"]
    else:
        plan = build_plan(database)
    summary = {k: v for k, v in plan.items() if not k.startswith("state_")}
    summary["state_before_sha256"] = plan["state_before_sha256"]
    summary["applied"] = args.apply
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
