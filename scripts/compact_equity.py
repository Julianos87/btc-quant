"""Compacte l'historique SQLite en gardant 90 jours à pleine résolution.

Les checkpoints de routine (events) restent purgés après 7 jours. L'equity
reste dense sur toute la fenêtre de qualification (90 j) ; au-delà, un point
toutes les 5 minutes suffit à la mesure d'uptime (fraîcheur 10 / 20 min).

La trace d'audit — ordres, fills, stops, funding, flux — n'est jamais purgée.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.console import enable_utf8_output
from btcquant.backup import assert_writer_recovery_clear

enable_utf8_output()

import pandas as pd

from btcquant.execution.state_store import StateStore

STATE = ROOT / "state"
KEEP_FULL_DAYS = 90
KEEP_EVENT_DAYS = 7


def main() -> None:
    database = STATE / "btcquant.db"
    if not database.exists():
        print("btcquant.db absente, aucune compaction")
        return
    assert_writer_recovery_clear(STATE)
    now = pd.Timestamp.now(tz="UTC")
    equity_cutoff = (now - pd.Timedelta(days=KEEP_FULL_DAYS)).isoformat()
    event_cutoff = (now - pd.Timedelta(days=KEEP_EVENT_DAYS)).isoformat()
    store = StateStore(database)
    for engine in ("trend", "carry"):
        before, after = store.compact_equity(engine, equity_cutoff)
        print(f"{engine} : {before} → {after} échantillons d'équity")
    before, after = store.compact_events(event_cutoff)
    print(f"événements : {before} → {after} (checkpoints de routine seuls purgés)")


if __name__ == "__main__":
    main()
