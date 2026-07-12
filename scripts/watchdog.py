"""Watchdog : détecte un moteur figé (vivant mais silencieux) et alerte.

systemd relance les crashs, mais pas un processus bloqué (réseau gelé,
exception avalée dans une boucle). Ce script — appelé par un timer systemd
toutes les 10 min — vérifie la fraîcheur des fichiers d'état et notifie si
un moteur n'a pas écrit depuis trop longtemps. Il tente aussi un restart
systemd du service concerné (si les droits le permettent).
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcquant.notify import notify

STATE = ROOT / "state"

# (fichier d'état, âge max en secondes, nom du service systemd)
CHECKS = [
    ("live_state_4x.json", 600, "btcquant-trend"),
    ("carry_state.json", 1200, "btcquant-carry"),  # tick carry = 5 min
]


def main() -> None:
    now = time.time()
    for fname, max_age, service in CHECKS:
        path = STATE / fname
        if not path.exists():
            notify(f"⚠ Watchdog : état {fname} introuvable — {service} n'a jamais démarré ?")
            continue
        age = now - path.stat().st_mtime
        if age > max_age:
            notify(f"⚠ Watchdog : {service} silencieux depuis {age/60:.0f} min "
                   f"(seuil {max_age/60:.0f}). Tentative de redémarrage.")
            try:
                subprocess.run(["systemctl", "restart", service], timeout=30, check=False)
            except Exception as e:
                notify(f"⚠ Watchdog : échec du restart de {service} : {e}")


if __name__ == "__main__":
    main()
