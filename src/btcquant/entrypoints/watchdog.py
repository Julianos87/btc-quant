"""Watchdog : détecte un moteur figé (vivant mais silencieux) et alerte.

systemd relance les crashs, mais pas un processus bloqué (réseau gelé,
exception avalée dans une boucle). Ce script — appelé par un timer systemd
toutes les 10 min — lit l'horodatage du dernier checkpoint SQLite de chaque
moteur et ouvre un incident si l'un d'eux n'a pas écrit depuis trop longtemps.

Il **ne redémarre rien** : il tourne sous l'utilisateur `btcquant`, sans droit
systemd, et un redémarrage automatique masquerait la cause plutôt que de la
traiter. La reprise reste une décision humaine, guidée par l'incident ouvert.
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
# Les seuils reprennent ceux des SLO (docs/RELIABILITY_SLO.md) et de la
# politique de qualification : 10 min pour le trend (tick 60 s), 20 min pour le
# carry (tick 300 s). Le carry était absent de cette liste : un moteur carry
# figé n'était donc jamais détecté, alors qu'il porte 40 % du portefeuille.
CHECKS = [
    ("trend", 600, "btcquant-trend"),
    ("carry", 1200, "btcquant-carry"),
]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(STATE / "btcquant.db"))
    parser.add_argument("--service", default="btcquant-trend")
    parser.add_argument("--max-age", type=int, default=600)
    args = parser.parse_args(argv)
    database = Path(args.database)
    if not database.exists():
        notify("⚠ Watchdog : base btcquant.db introuvable — moteurs jamais démarrés ?")
        return
    # Le watchdog écrit/actualise les incidents ; il peut donc aussi appliquer
    # la migration de schéma avant les premiers runners après un déploiement.
    store = StateStore(database)
    checks = (
        [("trend", args.max_age, args.service)]
        if args.database != str(STATE / "btcquant.db") or args.service != "btcquant-trend"
        else CHECKS
    )
    for engine, max_age, service in checks:
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
