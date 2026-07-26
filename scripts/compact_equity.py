"""Compacte l'historique SQLite en gardant 7 jours à pleine résolution."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from btcquant.execution.state_store import StateStore

STATE = ROOT / "state"
KEEP_FULL_DAYS = 7


def main() -> None:
    database = STATE / "btcquant.db"
    if not database.exists():
        print("btcquant.db absente, aucune compaction")
        return
    cutoff = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=KEEP_FULL_DAYS)).isoformat()
    store = StateStore(database)
    for engine in ("trend", "carry"):
        before, after = store.compact_equity(engine, cutoff)
        print(f"{engine} : {before} → {after} échantillons")


if __name__ == "__main__":
    main()
