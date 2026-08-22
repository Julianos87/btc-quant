#!/usr/bin/env bash
# Déploiement atomique depuis un clone canonique, avec chemins explicites
# code-only et migration. Ce script ne doit jamais être lancé implicitement.
set -euo pipefail

ROOT="${BTCQUANT_ROOT:-/opt/btcquant}"
CLONE="${BTCQUANT_CLONE:-/home/btcquant/btc-quant}"
CURRENT="${ROOT}/current"
PREVIOUS="${ROOT}/previous"
RESTART_ENGINES=false
MIGRATION_MODE=false
TARGET_SHA=""
LOCK_FILE="${BTCQUANT_DEPLOY_LOCK:-/run/lock/btcquant-deploy.lock}"
DEPLOY_REMOTE="${DEPLOY_REMOTE:-}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
CANONICAL_REPOSITORY="${BTCQUANT_CANONICAL_REPOSITORY:-github.com/Julianos87/btc-quant.git}"
TESTNET_APPROVAL="${ROOT}/state/HYPERLIQUID_TESTNET_APPROVED"
MIGRATION_ATTEMPTED=false
MIGRATION_COMPLETED=false
TARGET_WRITES_STARTED=false
MIGRATION_BACKUP=""

WRITER_UNITS=(
  btcquant-carry.service btcquant-trend.service btcquant-dashboard.service
  btcquant-watchdog.service btcquant-compact.service btcquant-backup.service
  btcquant-rebalance.service btcquant-rebalance-pending.service btcquant-shadow.service
  btcquant-hyperliquid-testnet.service btcquant-hyperliquid-watchdog.service
)
WRITER_TIMERS=(
  btcquant-watchdog.timer btcquant-hyperliquid-watchdog.timer btcquant-compact.timer
  btcquant-backup.timer btcquant-rebalance.timer btcquant-rebalance-pending.timer
)

stop_all_writer_processes() {
  systemctl stop "${WRITER_UNITS[@]}" "${WRITER_TIMERS[@]}" 2>/dev/null || true
}

restart_target_dashboard() {
  # Dashboard is in the writer set: starting it crosses the irreversible
  # migration rollback frontier even if no business write is later observed.
  TARGET_WRITES_STARTED=true
  systemctl restart btcquant-dashboard
}

restart_selected_engines() {
  TARGET_WRITES_STARTED=true
  if [ -f "${TESTNET_APPROVAL}" ]; then
    systemctl restart btcquant-hyperliquid-testnet
  else
    systemctl restart btcquant-trend btcquant-carry
  fi
}

configure_shadow_service() {
  TARGET_WRITES_STARTED=true
  if [ -x "${CURRENT}/venv/bin/btcquant-shadow" ]; then
    systemctl enable btcquant-shadow.service
    systemctl restart btcquant-shadow.service
  else
    systemctl disable --now btcquant-shadow.service 2>/dev/null || true
  fi
}

configure_pending_rebalance_timer() {
  TARGET_WRITES_STARTED=true
  if [ -f "${CURRENT}/deploy/btcquant-rebalance-pending.timer" ]; then
    systemctl enable --now btcquant-rebalance-pending.timer
  else
    systemctl disable --now btcquant-rebalance-pending.timer 2>/dev/null || true
  fi
}

wait_for_dashboard() {
  local attempt
  for attempt in {1..15}; do
    if systemctl is-active --quiet btcquant-dashboard &&
      curl --fail --silent --show-error --max-time 10 \
        http://127.0.0.1:8666/healthz >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Le dashboard n'est pas sain après 15 secondes." >&2
  return 1
}

wait_for_readiness() {
  local attempt
  for attempt in {1..30}; do
    if curl --fail --silent --show-error --max-time 10 \
      http://127.0.0.1:8666/readyz >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "La readiness applicative n'est pas acquise après 30 secondes." >&2
  return 1
}

install_units() {
  cp "${CURRENT}/deploy/"btcquant-*.service "${CURRENT}/deploy/"btcquant-*.timer \
    /etc/systemd/system/
  install -o root -g root -m 0755 "${CURRENT}/deploy/rebalance-root.sh" \
    /usr/local/libexec/btcquant-rebalance
  systemctl daemon-reload
  systemd-analyze verify "${CURRENT}/deploy/"*.service "${CURRENT}/deploy/"*.timer
}

validate_release_target() {
  local target="$1"
  local expected_target="$2"
  local resolved_target

  if [ -L "${target}" ]; then
    echo "Refus: la release cible ne doit pas être un lien symbolique." >&2
    return 1
  fi
  if [ ! -d "${target}" ]; then
    echo "Refus: la release cible attendue est absente ou n'est pas un répertoire." >&2
    return 1
  fi
  resolved_target="$(readlink -f -- "${target}" 2>/dev/null || true)"
  if [ "${resolved_target}" != "${expected_target}" ]; then
    echo "Refus: le chemin canonique de la release cible est inattendu." >&2
    return 1
  fi
  if [ ! -f "${target}/release-manifest.json" ]; then
    echo "Refus: manifeste de release absent." >&2
    return 1
  fi
  if [ ! -x "${target}/venv/bin/python" ]; then
    echo "Refus: interpréteur Python de release absent ou non exécutable." >&2
    return 1
  fi
}

restore_pre_migration_and_old_code() {
  local old_target="$1"
  if [ ! -f "${MIGRATION_BACKUP}" ]; then
    echo "MANUAL RECOVERY REQUIRED: backup pré-migration absent." >&2
    return 1
  fi
  stop_all_writer_processes
  "${TARGET}/venv/bin/python" -c '
import sys
from btcquant.deployment import restore_sqlite_database

result = restore_sqlite_database(sys.argv[1], sys.argv[2])
assert result["integrity_check"] == "ok" and int(result["schema_version"]) < 6, result
' "${MIGRATION_BACKUP}" "${ROOT}/state/btcquant.db"
  "${TARGET}/venv/bin/python" -c '
import sys
from btcquant.deployment import atomic_switch_release

atomic_switch_release(sys.argv[1], sys.argv[2])
' "${ROOT}" "${old_target}"
  install_units
  systemctl restart btcquant-dashboard
  wait_for_dashboard
}

if [ "$(id -u)" -ne 0 ]; then
  echo "Ce script doit être exécuté par root." >&2
  exit 1
fi
mkdir -p "$(dirname "${LOCK_FILE}")"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "Déploiement déjà en cours." >&2
  exit 75
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --sha)
      [ "$#" -ge 2 ] || { echo "Usage: update.sh --sha <40-hex> [--migration] [--engines]" >&2; exit 2; }
      TARGET_SHA="$2"
      shift 2
      ;;
    --engines)
      RESTART_ENGINES=true
      shift
      ;;
    --migration)
      MIGRATION_MODE=true
      shift
      ;;
    --rollback)
      [ -z "${TARGET_SHA}" ] || { echo "Arguments incompatibles." >&2; exit 2; }
      TARGET_SHA=rollback
      shift
      ;;
    *)
      echo "Usage: update.sh --sha <40-hex> [--migration] [--engines|--rollback]" >&2
      exit 2
      ;;
  esac
done
if [ -z "${TARGET_SHA}" ]; then
  echo "Refus: un SHA exact est obligatoire." >&2
  exit 2
fi
if [ "${TARGET_SHA}" != rollback ] && ! [[ "${TARGET_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Refus: SHA Git complet de 40 caractères requis." >&2
  exit 2
fi
if [ "${TARGET_SHA}" = rollback ] && ${MIGRATION_MODE}; then
  echo "Refus: rollback et migration sont incompatibles." >&2
  exit 2
fi

if [ "${TARGET_SHA}" = rollback ]; then
  [ -L "${CURRENT}" ] && [ -L "${PREVIOUS}" ] || { echo "Rollback refusé: liens absents." >&2; exit 1; }
  TARGET="$(readlink -f "${PREVIOUS}")"
  OLD_TARGET="$(readlink -f "${CURRENT}")"
  RELEASE_ID="$(basename "${TARGET}")"
  [[ "${RELEASE_ID}" =~ ^[0-9a-f]{40}$ ]] || { echo "Rollback refusé: previous invalide." >&2; exit 1; }
  [ -f "${TARGET}/release-manifest.json" ] || { echo "Rollback refusé: manifeste absent." >&2; exit 1; }
  DB_SCHEMA="$(${OLD_TARGET}/venv/bin/python -c '
import sqlite3
import sys

connection = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
row = connection.execute("SELECT value FROM metadata WHERE key = ?", ("schema_version",)).fetchone()
print(row[0] if row else "UNKNOWN")
' "${ROOT}/state/btcquant.db")"
  REQUIRED_SCHEMA="$(${TARGET}/venv/bin/python -c '
import json
import sys

print(json.loads(open(sys.argv[1], encoding="utf-8").read())["schema_version_required"])
' "${TARGET}/release-manifest.json")"
  if [ "${DB_SCHEMA}" = UNKNOWN ] || [ "${DB_SCHEMA}" -gt "${REQUIRED_SCHEMA}" ]; then
    echo "Rollback code refusé contre une DB plus récente; récupération manuelle requise." >&2
    exit 1
  fi
  "${OLD_TARGET}/venv/bin/python" -c '
import sys
from btcquant.deployment import atomic_switch_release

atomic_switch_release(sys.argv[1], sys.argv[2])
' "${ROOT}" "${TARGET}"
  install_units
  configure_pending_rebalance_timer
  configure_shadow_service
  if ! systemctl restart btcquant-dashboard || ! wait_for_dashboard; then
    echo "Rollback invalide; restauration manuelle requise." >&2
    exit 1
  fi
  echo "Rollback activé: ${RELEASE_ID}"
  exit 0
fi

if [ -z "${DEPLOY_REMOTE}" ]; then
  echo "Refus: DEPLOY_REMOTE doit identifier explicitement la remote canonique." >&2
  exit 1
fi
if [ -z "${DEPLOY_BRANCH}" ]; then
  echo "Refus: DEPLOY_BRANCH doit identifier explicitement la branche canonique." >&2
  exit 1
fi
if [ ! -f "${ROOT}/.env" ]; then
  echo "Refus de déployer : ${ROOT}/.env est absent." >&2
  exit 1
fi
if [ ! -L "${CURRENT}" ]; then
  echo "Refus de déployer : ${CURRENT} n'est pas un lien de release." >&2
  exit 1
fi
if ! grep -q '^BACKUP_ENCRYPTION_KEY=.\+' "${ROOT}/.env"; then
  echo "Refus de déployer : BACKUP_ENCRYPTION_KEY est absente." >&2
  exit 1
fi
[ -d "${CLONE}/.git" ] || { echo "Refus: clone source absent." >&2; exit 1; }
REMOTE_URL="$(sudo -u btcquant git -C "${CLONE}" remote get-url "${DEPLOY_REMOTE}" 2>/dev/null || true)"
[ -n "${REMOTE_URL}" ] || { echo "Refus: remote ${DEPLOY_REMOTE} absente." >&2; exit 1; }
PYTHONPATH="${CLONE}/src" /usr/bin/python3 -c '
import sys
from btcquant.deployment import validate_canonical_repository

validate_canonical_repository(sys.argv[1], sys.argv[2])
' "${REMOTE_URL}" "${CANONICAL_REPOSITORY}"
sudo -u btcquant git -C "${CLONE}" fetch "${DEPLOY_REMOTE}" --prune
REMOTE_REF="${DEPLOY_REMOTE}/${DEPLOY_BRANCH}"
REMOTE_SHA="$(sudo -u btcquant git -C "${CLONE}" rev-parse "${REMOTE_REF}")"
if [ "${REMOTE_SHA}" != "${TARGET_SHA}" ] || ! sudo -u btcquant git -C "${CLONE}" merge-base --is-ancestor "${TARGET_SHA}" "${REMOTE_REF}"; then
  echo "Refus: le SHA cible n'est pas exactement la branche canonique ${REMOTE_REF}." >&2
  exit 1
fi
if [ -n "$(sudo -u btcquant git -C "${CLONE}" status --porcelain --untracked-files=all)" ]; then
  echo "Refus de déployer un clone dirty ou non suivi." >&2
  exit 1
fi
sudo -u btcquant git -C "${CLONE}" checkout --detach --quiet "${TARGET_SHA}"
[ "$(sudo -u btcquant git -C "${CLONE}" rev-parse HEAD)" = "${TARGET_SHA}" ] || { echo "Refus: HEAD source inattendu." >&2; exit 1; }
RELEASE_ID="${TARGET_SHA}"
OLD_TARGET="$(readlink -f "${CURRENT}")"
OLD_PREVIOUS="$(readlink -f "${PREVIOUS}" 2>/dev/null || true)"

# create-release.sh emits build logs; the release identity is the deterministic
# ROOT/releases/SHA path, never a value parsed from its stdout.
BTCQUANT_ROOT="${ROOT}" \
  bash "${CLONE}/deploy/create-release.sh" "${CLONE}" "${RELEASE_ID}"
TARGET="${ROOT}/releases/${RELEASE_ID}"
EXPECTED_TARGET="$(readlink -m -- "${ROOT}/releases/${RELEASE_ID}")"
validate_release_target "${TARGET}" "${EXPECTED_TARGET}"
"${TARGET}/venv/bin/python" -c '
import sys
from btcquant.deployment import validate_release_manifest

validate_release_manifest(sys.argv[1], sys.argv[2])
' "${TARGET}" "${RELEASE_ID}"
APP_SCHEMA="$(${TARGET}/venv/bin/python -c '
import sys
from btcquant.deployment import inspect_sqlite

print(inspect_sqlite(sys.argv[1]).metadata_schema_version or "UNKNOWN")
' "${ROOT}/state/btcquant.db")"
if [ "${APP_SCHEMA}" = UNKNOWN ]; then
  echo "Refus: app_schema_version inconnue." >&2
  exit 1
fi
if [ "${APP_SCHEMA}" -gt 6 ]; then
  echo "Refus: app_schema_version plus récente que le code cible." >&2
  exit 1
fi
if ! ${MIGRATION_MODE} && [ "${APP_SCHEMA}" -lt 6 ]; then
  echo "CODE_DEPLOY_REFUSED: migration explicite requise (app_schema_version=${APP_SCHEMA})." >&2
  exit 3
fi
if ${MIGRATION_MODE} && [ "${APP_SCHEMA}" -eq 6 ]; then
  echo "MIGRATION_DEPLOY_REFUSED: la base est déjà au schéma cible; utiliser le chemin code-only." >&2
  exit 3
fi
if ${MIGRATION_MODE}; then
  BTCQUANT_ROOT="${ROOT}" BTCQUANT_CURRENT="${TARGET}" BTCQUANT_MIGRATION_PENDING=true \
    bash "${TARGET}/deploy/preflight.sh"
else
  BTCQUANT_ROOT="${ROOT}" BTCQUANT_CURRENT="${TARGET}" bash "${TARGET}/deploy/preflight.sh"
fi

migration_abort_on_error() {
  code=$?
  trap - ERR
  if [ -f "${MIGRATION_BACKUP}" ]; then
    echo "FAIL CLOSED: migration interrompue après création du backup; MANUAL RECOVERY REQUIRED." >&2
  else
    echo "MIGRATION_REFUSED: migration arrêtée avant backup; DB/current/previous inchangés." >&2
  fi
  exit "${code}"
}

if ${MIGRATION_MODE}; then
  MIGRATION_ATTEMPTED=true
  MIGRATION_BACKUP="${ROOT}/backups/pre-migration-${TARGET_SHA}.db"
  trap migration_abort_on_error ERR
  # migrate.sh resolves its Python from its own release directory (TARGET),
  # not from /opt/btcquant/current. Do not pass BTCQUANT_CURRENT here.
  BTCQUANT_DEPLOY_LOCK_HELD=true BTCQUANT_ROOT="${ROOT}" \
    BTCQUANT_DATABASE="${ROOT}/state/btcquant.db" \
    bash "${TARGET}/deploy/migrate.sh" --sha "${TARGET_SHA}" \
      --backup "${MIGRATION_BACKUP}" --confirm-migration
  MIGRATION_COMPLETED=true
  trap - ERR
fi

rollback_on_error() {
  code=$?
  trap - ERR
  if ${MIGRATION_COMPLETED}; then
    if ${TARGET_WRITES_STARTED}; then
      stop_all_writer_processes
      echo "FAIL CLOSED: migration effectuée et écriture cible détectée; MANUAL RECOVERY REQUIRED." >&2
      exit "${code}"
    fi
    echo "Health/pre-switch failure après migration sans écriture cible; restauration vérifiée." >&2
    if restore_pre_migration_and_old_code "${OLD_TARGET}"; then
      echo "Rollback migration validé après restauration du backup pré-migration." >&2
    else
      echo "MANUAL RECOVERY REQUIRED: restauration ou retour code impossible." >&2
    fi
    exit "${code}"
  fi
  if ${MIGRATION_ATTEMPTED}; then
    echo "FAIL CLOSED: migration interrompue; schéma potentiellement ambigu, MANUAL RECOVERY REQUIRED." >&2
    exit "${code}"
  fi
  echo "Échec code-only; retour à ${OLD_TARGET}." >&2
  stop_all_writer_processes
  "${TARGET}/venv/bin/python" -c '
import sys
from btcquant.deployment import atomic_switch_release

atomic_switch_release(sys.argv[1], sys.argv[2])
' "${ROOT}" "${OLD_TARGET}" || true
  install_units || true
  systemctl daemon-reload || true
  systemctl restart btcquant-dashboard || true
  exit "${code}"
}
trap rollback_on_error ERR

"${TARGET}/venv/bin/python" -c '
import sys
from btcquant.deployment import atomic_switch_release

atomic_switch_release(sys.argv[1], sys.argv[2])
' "${ROOT}" "${TARGET}"
install_units

if ${MIGRATION_MODE}; then
  # Le dashboard appartient au writer set: son démarrage franchit la frontière
  # irréversible, même si aucun write métier n'est ensuite observé.
  restart_target_dashboard
  wait_for_dashboard
  # À partir d'ici timers/shadow/engines peuvent écrire: c'est le point de
  # non-retour documenté pour un rollback automatique de migration.
  configure_pending_rebalance_timer
  configure_shadow_service
  if ${RESTART_ENGINES}; then
    restart_selected_engines
  fi
else
  systemctl enable --now btcquant-compact.timer
  configure_pending_rebalance_timer
  configure_shadow_service
  systemctl restart btcquant-dashboard
  if ${RESTART_ENGINES}; then
    restart_selected_engines
  fi
  wait_for_dashboard
fi

if ${RESTART_ENGINES}; then
  if [ -f "${TESTNET_APPROVAL}" ]; then
    systemctl is-active --quiet btcquant-hyperliquid-testnet
  else
    systemctl is-active --quiet btcquant-trend
    systemctl is-active --quiet btcquant-carry
  fi
  wait_for_readiness
fi

trap - ERR
if ${MIGRATION_MODE}; then
  DEPLOY_KIND=migration
else
  DEPLOY_KIND=code-only
fi
echo "Release active : ${RELEASE_ID} (${DEPLOY_KIND})"
echo "Rollback manuel : sudo bash ${CURRENT}/deploy/update.sh --rollback"
