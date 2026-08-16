"""Inspecte la base opérationnelle sans la modifier.

Usage : python scripts/inspect_state.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.console import enable_utf8_output

enable_utf8_output()

from btcquant.execution.state_store import StateStore
from btcquant.execution.health import execution_health


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "state" / "btcquant.db")
    args = parser.parse_args()
    if not args.database.exists():
        raise SystemExit(f"Base introuvable : {args.database}")

    store = StateStore(args.database, initialize=False)
    print(f"Base       : {args.database}")
    print(f"Intégrité  : {'OK' if store.integrity_check() else 'ERREUR'}")
    for engine in ("trend", "carry"):
        state = store.load_engine_state(engine)
        age = store.engine_age_seconds(engine)
        print(
            f"{engine:10}: {'présent' if state else 'absent'}, âge {age:.0f} s"
            if age is not None
            else f"{engine:10}: absent"
        )
        health = execution_health(store, engine)
        if (
            health.fill_ratio is not None
            and health.rejection_rate is not None
            and health.p95_slippage_bps is not None
        ):
            print(
                f"{'':10}  exécution: {health.orders_analyzed} ordres, "
                f"fill {health.fill_ratio:.1%}, rejet {health.rejection_rate:.1%}, "
                f"slippage p95 {health.p95_slippage_bps:.1f} bps"
            )
        elif health.orders_analyzed == 0:
            print(f"{'':10}  exécution: aucun ordre journalisé")
        else:
            print(f"{'':10}  exécution: données insuffisantes")

    unresolved = store.unresolved_orders("trend") + store.unresolved_orders("carry")
    print(f"Ordres non résolus : {len(unresolved)}")
    for order in unresolved:
        print(
            f"  #{order['id']} [{order['status']}] {order['engine']}/{order['slot']} "
            f"{order['side']} {order['requested_qty']} — {order['intent_id']}"
        )

    with sqlite3.connect(args.database) as connection:
        for table in (
            "positions",
            "orders",
            "events",
            "incidents",
            "trades",
            "equity_samples",
            "flows",
        ):
            count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"{table:15}: {count}")

    if unresolved:
        print("\nDétail JSON des ordres à réconcilier :")
        print(json.dumps(unresolved, indent=2, ensure_ascii=False))

    incidents = store.read_incidents(open_only=True)
    if incidents:
        print("\nIncidents ouverts :")
        print(json.dumps(incidents, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
