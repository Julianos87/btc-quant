"""Watchdog : détecte un moteur figé (vivant mais silencieux) et alerte.

systemd relance les crashs, mais pas un processus bloqué (réseau gelé,
exception avalée dans une boucle). Ce script — appelé par un timer systemd
toutes les 2 min — lit l'horodatage du dernier checkpoint SQLite de chaque
moteur et ouvre un incident si l'un d'eux n'a pas écrit depuis trop longtemps.

Il **ne redémarre rien** : il tourne sous l'utilisateur `btcquant`, sans droit
systemd, et un redémarrage automatique masquerait la cause plutôt que de la
traiter. La reprise reste une décision humaine, guidée par l'incident ouvert.
"""

import argparse
import logging
import os
from pathlib import Path

from btcquant.execution.health import execution_health, sync_execution_incidents
from btcquant.execution.readiness import service_component_profile
from btcquant.execution.shadow import ShadowStore
from btcquant.execution.state_store import StateStore
from btcquant.notify import notify

log = logging.getLogger(__name__)

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
SHADOW_MAX_AGE_SECONDS = 300


def _sync_shadow_incident(
    store: StateStore,
    database: Path,
    *,
    max_age: int = SHADOW_MAX_AGE_SECONDS,
) -> None:
    fingerprint = "shadow:market_data_stale"
    if not database.exists():
        incident = store.record_incident(
            fingerprint,
            engine="shadow",
            severity="WARNING",
            kind="shadow_data_missing",
            message="Base shadow absente ; observation mainnet indisponible",
            context={"database": str(database)},
        )
    else:
        try:
            health = ShadowStore(database, read_only=True).runtime_health()
            age = health["last_success_age_seconds"]
        except Exception as error:
            incident = store.record_incident(
                "watchdog:shadow:check_failed",
                engine="shadow",
                severity="CRITICAL",
                kind="watchdog_check_failed",
                message="Lecture de santé shadow impossible ; état UNKNOWN",
                context={"error_type": type(error).__name__},
            )
            if incident["is_new_or_reopened"]:
                notify(f"Watchdog shadow : {incident['message']}")
            return
        if age is not None and age <= max_age:
            store.resolve_incident(fingerprint)
            return
        message = (
            "Aucune lecture de carnet shadow réussie"
            if age is None
            else f"Carnet shadow silencieux depuis {age / 60:.0f} min"
        )
        incident = store.record_incident(
            fingerprint,
            engine="shadow",
            severity="WARNING",
            kind="shadow_data_stale",
            message=message,
            context={
                "age_seconds": age,
                "threshold_seconds": max_age,
                "consecutive_failures": health["consecutive_failures"],
                "last_error_type": health["last_error_type"],
            },
        )
    if incident["is_new_or_reopened"]:
        notify(f"Watchdog shadow : {incident['message']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=str(STATE / "btcquant.db"))
    parser.add_argument("--service", default="btcquant-trend")
    parser.add_argument("--max-age", type=int, default=600)
    parser.add_argument("--shadow-database", type=Path)
    parser.add_argument("--shadow-max-age", type=int, default=SHADOW_MAX_AGE_SECONDS)
    args = parser.parse_args(argv)
    database = Path(args.database)
    if not database.exists():
        message = "Watchdog: database unavailable; source state is UNKNOWN"
        log.critical(message)
        notify(message)
        raise SystemExit(2)
    # Le watchdog écrit/actualise les incidents. Il est donc un writer à
    # arrêter avant toute sauvegarde ou migration de la base partagée; le code
    # refuse désormais une migration implicite.
    try:
        store = StateStore(database)
    except Exception as error:
        message = (
            "CRITICAL Watchdog: database read failed; source state is UNKNOWN "
            f"({type(error).__name__})"
        )
        log.critical(message)
        notify(message)
        raise SystemExit(2) from error
    default_database = args.database == str(STATE / "btcquant.db")
    default_service = args.service == "btcquant-trend"
    if default_database and default_service:
        profile = service_component_profile()
        if profile.reason_codes:
            message = "CRITICAL Watchdog: invalid required-engine profile; source state is UNKNOWN"
            log.critical(message)
            notify(message)
            raise SystemExit(2)
        checks = [check for check in CHECKS if check[0] in profile.required]
    else:
        checks = [("trend", args.max_age, args.service)]
    for engine, max_age, service in checks:
        fingerprint = f"engine:{engine}:stale"
        try:
            age = store.engine_age_seconds(engine)
            health = execution_health(store, engine)
        except Exception as error:
            incident = store.record_incident(
                f"watchdog:{engine}:check_failed",
                engine=engine,
                severity="CRITICAL",
                kind="watchdog_check_failed",
                message=f"Lecture de santé {engine} impossible ; état UNKNOWN",
                context={"error_type": type(error).__name__},
            )
            if incident["is_new_or_reopened"]:
                notify(f"CRITICAL Watchdog : {incident['message']}")
            continue

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

        for incident in sync_execution_incidents(store, health):
            icon = "⛔" if incident["severity"] == "CRITICAL" else "⚠"
            notify(f"{icon} Exécution {engine} : {incident['message']}")

    if args.shadow_database is not None:
        _sync_shadow_incident(
            store,
            args.shadow_database,
            max_age=args.shadow_max_age,
        )


if __name__ == "__main__":
    main()
