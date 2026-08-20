#!/usr/bin/env bash
# Migration explicite: elle refuse toute écriture tant que la quiescence des
# writers et des timers n'est pas démontrée. Elle ne les arrête pas implicitement.
#
# Le runtime de migration est la release qui contient CE script (cible),
# jamais /opt/btcquant/current. BTCQUANT_CURRENT n'est pas consulté.
set -euo pipefail

ROOT="${BTCQUANT_ROOT:-/opt/btcquant}"
DATABASE="${BTCQUANT_DATABASE:-${ROOT}/state/btcquant.db}"
BACKUP=""
TARGET_SHA=""
CONFIRM=false
LOCK_FILE="${BTCQUANT_DEPLOY_LOCK:-/run/lock/btcquant-deploy.lock}"

SCRIPT_DIR="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1
  pwd -P
)"
MIGRATION_RELEASE="$(
  cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1
  pwd -P
)"
MIGRATION_PYTHON="${MIGRATION_RELEASE}/venv/bin/python"
MANIFEST="${MIGRATION_RELEASE}/release-manifest.json"

if [ "$(id -u)" -ne 0 ]; then
  echo "La migration doit être préparée par root." >&2
  exit 1
fi
while [ "$#" -gt 0 ]; do
  case "$1" in
    --sha)
      [ "$#" -ge 2 ] || { echo "--sha requiert un SHA complet." >&2; exit 2; }
      TARGET_SHA="$2"
      shift 2
      ;;
    --backup)
      [ "$#" -ge 2 ] || { echo "--backup requiert un chemin." >&2; exit 2; }
      BACKUP="$2"
      shift 2
      ;;
    --confirm-migration)
      CONFIRM=true
      shift
      ;;
    *)
      echo "Usage: migrate.sh --sha <40-hex> --confirm-migration [--backup path]" >&2
      exit 2
      ;;
  esac
done
if ! [[ "${TARGET_SHA}" =~ ^[0-9a-f]{40}$ ]] || ! ${CONFIRM}; then
  echo "MIGRATION_REFUSED: SHA complet et confirmation explicite requis." >&2
  exit 2
fi
if [ -z "${BACKUP}" ]; then
  BACKUP="${ROOT}/backups/pre-migration-${TARGET_SHA}.db"
fi

# Les états inconnus sont refusés. Les timers sont contrôlés séparément pour
# empêcher le démarrage d'un writer entre la preuve et le backup.
WRITER_UNITS=(
  btcquant-carry.service
  btcquant-trend.service
  btcquant-dashboard.service
  btcquant-watchdog.service
  btcquant-compact.service
  btcquant-backup.service
  btcquant-rebalance.service
  btcquant-rebalance-pending.service
  btcquant-shadow.service
  btcquant-hyperliquid-testnet.service
  btcquant-hyperliquid-watchdog.service
)
WRITER_TIMERS=(
  btcquant-watchdog.timer
  btcquant-hyperliquid-watchdog.timer
  btcquant-compact.timer
  btcquant-backup.timer
  btcquant-rebalance.timer
  btcquant-rebalance-pending.timer
)
check_quiescence() {
  local unit state load_state
  for unit in "${WRITER_UNITS[@]}" "${WRITER_TIMERS[@]}"; do
    load_state="$(systemctl show "${unit}" --property=LoadState --value 2>/dev/null || true)"
    state="$(systemctl show "${unit}" --property=ActiveState --value 2>/dev/null || true)"
    if [ "${load_state}" != loaded ] || [ -z "${state}" ]; then
      echo "MIGRATION_REFUSED: unité systemd absente ou inconnue (${unit})." >&2
      return 1
    fi
    case "${state}" in
      inactive|dead) ;;
      *)
        echo "MIGRATION_REFUSED: writer/timer actif (${unit}: ${state})." >&2
        return 1
        ;;
    esac
  done
}

# Le parent update.sh peut déjà détenir le verrou; le script autonome le prend.
if [ "${BTCQUANT_DEPLOY_LOCK_HELD:-false}" != true ]; then
  mkdir -p "$(dirname "${LOCK_FILE}")"
  exec 9>"${LOCK_FILE}"
  if ! flock -n 9; then
    echo "MIGRATION_REFUSED: déploiement déjà en cours." >&2
    exit 75
  fi
fi
if [ ! -x "${MIGRATION_PYTHON}" ]; then
  echo "MIGRATION_REFUSED: Python de migration absent dans la release cible (${MIGRATION_PYTHON})." >&2
  exit 1
fi
if [ ! -f "${MANIFEST}" ]; then
  echo "MIGRATION_REFUSED: manifeste de release absent (${MANIFEST})." >&2
  exit 1
fi
MANIFEST_SHA="$(
  "${MIGRATION_PYTHON}" -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("git_sha",""))' \
    "${MANIFEST}"
)" || {
  echo "MIGRATION_REFUSED: manifeste illisible (${MANIFEST})." >&2
  exit 1
}
if [ "${MANIFEST_SHA}" != "${TARGET_SHA}" ]; then
  echo "MIGRATION_REFUSED: migration release SHA != requested target SHA (${MANIFEST_SHA} != ${TARGET_SHA})." >&2
  exit 1
fi

check_quiescence
# The Python migration entrypoint performs the second, independent /proc gate
# for open DB/WAL/SHM descriptors immediately before checkpoint and backup.
exec "${MIGRATION_PYTHON}" -m btcquant.entrypoints.migrate \
  --database "${DATABASE}" \
  --backup "${BACKUP}" \
  --target-git-sha "${TARGET_SHA}" \
  --confirm-migration \
  --require-quiescence
