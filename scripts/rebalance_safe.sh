#!/usr/bin/env bash
# Rééquilibrage 60/40 SÛR : arrête les moteurs, applique, redémarre.
#
# Pourquoi : les runners gardent leur état EN MÉMOIRE et le réécrivent sur
# disque à chaque tick (60 s trend / 300 s carry). Modifier les fichiers
# d'état pendant qu'ils tournent est silencieusement écrasé dans la minute.
# Ce wrapper (lancé root par le timer systemd) garantit l'arrêt propre,
# l'application par l'utilisateur de service, puis le redémarrage — même si
# le rééquilibrage échoue (trap EXIT).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

trap 'systemctl start btcquant-trend btcquant-carry' EXIT
systemctl stop btcquant-trend btcquant-carry
# petit délai : laisser les runners finir leur dernier _save_state
sleep 3
runuser -u btcquant -- "${ROOT}/venv/bin/python" "${ROOT}/scripts/rebalance.py" --apply
