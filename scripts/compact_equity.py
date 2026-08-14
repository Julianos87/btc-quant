"""Compacte l'historique SQLite en gardant 7 jours à pleine résolution.

Deux tables croissent linéairement avec le temps d'exécution : les échantillons
d'équity (un par tick) et le journal d'événements (un checkpoint par tick,
portant l'état complet du moteur). Sans compaction, une campagne de 90 jours les
rend coûteuses à lire — et `read_events` chargeait tout en mémoire.

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
KEEP_FULL_DAYS = 7


def main() -> None:
    database = STATE / "btcquant.db"
    if not database.exists():
        print("btcquant.db absente, aucune compaction")
        return
    assert_writer_recovery_clear(STATE)
    cutoff = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=KEEP_FULL_DAYS)).isoformat()
    store = StateStore(database)
    for engine in ("trend", "carry"):
        before, after = store.compact_equity(engine, cutoff)
        print(f"{engine} : {before} → {after} échantillons d'équity")
    before, after = store.compact_events(cutoff)
    print(f"événements : {before} → {after} (checkpoints de routine seuls purgés)")


if __name__ == "__main__":
    main()
