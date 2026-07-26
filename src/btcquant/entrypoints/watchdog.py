"""Watchdog : détecte un moteur figé (vivant mais silencieux) et alerte.

systemd relance les crashs, mais pas un processus bloqué (réseau gelé,
exception avalée dans une boucle). Ce script — appelé par un timer systemd
toutes les 10 min — vérifie la fraîcheur des fichiers d'état et notifie si
un moteur n'a pas écrit depuis trop longtemps. Il tente aussi un restart
systemd du service concerné (si les droits le permettent).
"""

import argparse
import os
from pathlib import Path

from btcquant.execution.health import execution_health, sync_execution_incidents
from btcquant.execution.state_store import StateStore
from btcquant.notify import notify

ROOT = Path(os.environ.get("BTCQUANT_ROOT", Path.cwd())).resolve()
STATE = ROOT / "state"

# (moteur, âge max en secondes, nom du service systemd)
CHECKS = [
    ("trend", 600, "btcquant-trend"),
    ("carry", 1200, "btcquant-carry"),  # tick carry = 5 min
]


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    database = STATE / "btcquant.db"
    if not database.exists():
        notify("⚠ Watchdog : base btcquant.db introuvable — moteurs jamais démarrés ?")
        return
    # Le watchdog écrit/actualise les incidents ; il peut donc aussi appliquer
    # la migration de schéma avant les premiers runners après un déploiement.
    store = StateStore(database)
    for engine, max_age, service in CHECKS:
        age = store.engine_age_seconds(engine)
        fingerprint = f"engine:{engine}:stale"
        if age is None:
            incident = store.record_incident(
                fingerprint,
                engine=engine,
                severity="CRITICAL",
                kind="engine_state_missing",
                message=f"État {engine} absent — {service} n'a jamais démarré",
            )
            if incident["is_new_or_reopened"]:
                notify(f"⚠ Watchdog : état {engine} absent — {service} n'a jamais démarré ?")
        elif age > max_age:
            incident = store.record_incident(
                fingerprint,
                engine=engine,
                severity="CRITICAL",
                kind="engine_stale",
                message=f"{service} silencieux depuis {age / 60:.0f} min",
                context={"age_seconds": age, "threshold_seconds": max_age},
            )
            if incident["is_new_or_reopened"]:
                notify(
                    f"⚠ Watchdog : {service} silencieux depuis {age / 60:.0f} min "
                    f"(seuil {max_age / 60:.0f}). Intervention requise."
                )
        else:
            store.resolve_incident(fingerprint)

        health = execution_health(store, engine)
        for incident in sync_execution_incidents(store, health):
            icon = "⛔" if incident["severity"] == "CRITICAL" else "⚠"
            notify(f"{icon} Exécution {engine} : {incident['message']}")


if __name__ == "__main__":
    main()
