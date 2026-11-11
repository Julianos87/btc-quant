"""Dry-run or apply an audited reversal of the latest false PAPER stop trade."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from btcquant.config import load_config  # noqa: E402
from btcquant.execution.instance_lock import EngineInstanceLock  # noqa: E402
from btcquant.execution.paper_trade_reversal import apply_plan, build_plan  # noqa: E402


def _summary(plan: dict, *, applied: bool, result: dict | None = None) -> dict:
    if plan.get("already_applied"):
        return {
            "already_applied": True,
            "reversal_event_id": plan.get("reversal_event_id"),
            "applied": applied,
        }
    position = plan["state_after"]["slots"][plan["trade"]["strategy"]]["position"]
    return {
        "applied": applied,
        "result": result,
        "trade_id": plan["trade_id"],
        "strategy": plan["trade"]["strategy"],
        "entry_ts": plan["trade"]["entry_ts"],
        "exit_ts": plan["trade"]["exit_ts"],
        "realized_pnl_excluded": plan["trade"]["pnl"],
        "restored_cash": plan["cash_restored"],
        "cash_delta": plan["cash_delta"],
        "funding_pnl_restored": plan["funding_pnl_restored"],
        "funding_corrections_reversed": len(plan["funding_corrections"]),
        "funding_payments_after_stop_restored": len(plan["post_close_funding"]),
        "restored_position": {
            "direction": plan["trade"]["direction"],
            "qty": position["qty"],
            "entry_price": position["entry_price"],
            "stop_price": position["stop_price"],
            "last_bar_ts": plan["state_after"]["slots"][plan["trade"]["strategy"]].get(
                "last_bar_ts"
            ),
        },
        "equity_samples_removed_for_rebuild": plan["equity_rows_to_remove"],
        "equity_rebuild_range": plan["equity_range_to_remove"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--trade-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    if config["environment"] != "paper" or config["execution"]["mode"] != "paper":
        raise ValueError("Cette procédure ne s'exécute qu'en environnement PAPER")
    database = args.database.resolve(strict=True)
    with EngineInstanceLock(database, "trend"):
        plan = build_plan(database, args.trade_id)
        result = apply_plan(database, plan) if args.apply else None
    print(json.dumps(_summary(plan, applied=args.apply, result=result), indent=2))


if __name__ == "__main__":
    main()
