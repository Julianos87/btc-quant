#!/usr/bin/env bash
# Première installation versionnée de btcquant sur Ubuntu/Debian.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Ce script doit être exécuté par root." >&2
  exit 1
fi

SOURCE="$(pwd -P)"
ROOT=/opt/btcquant
DEPLOY_REMOTE="${DEPLOY_REMOTE:-}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
CANONICAL_REPOSITORY="${BTCQUANT_CANONICAL_REPOSITORY:-github.com/Julianos87/btc-quant.git}"
# Optional SSH aliases must be explicitly mapped, e.g. github-backup=github.com.
CANONICAL_REMOTE_ALIASES="${BTCQUANT_CANONICAL_REMOTE_ALIASES:-}"
export BTCQUANT_CANONICAL_REMOTE_ALIASES="${CANONICAL_REMOTE_ALIASES}"

if [ -z "${DEPLOY_REMOTE}" ] || [ -z "${DEPLOY_BRANCH}" ]; then
  echo "Refus: DEPLOY_REMOTE et DEPLOY_BRANCH doivent être configurés." >&2
  exit 1
fi

echo "── Dépendances système ──"
apt-get update -y
apt-get install -y python3 python3-venv rsync openssl curl

echo "── Utilisateur et répertoires partagés ──"
id -u btcquant &>/dev/null || useradd -r -m -s /usr/sbin/nologin btcquant
mkdir -p "${ROOT}/releases" "${ROOT}/state" "${ROOT}/backups" "${ROOT}/data"

echo "── Secrets locaux ──"
if [ ! -f "${ROOT}/.env" ]; then
  TOKEN="$(openssl rand -hex 24)"
  cat >"${ROOT}/.env" <<EOF
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8666
DASHBOARD_TOKEN=${TOKEN}
BACKUP_ENCRYPTION_KEY=$(openssl rand -hex 32)
EOF
elif ! grep -q '^BACKUP_ENCRYPTION_KEY=' "${ROOT}/.env"; then
  echo "BACKUP_ENCRYPTION_KEY=$(openssl rand -hex 32)" >>"${ROOT}/.env"
fi

chown -R btcquant:btcquant "${ROOT}/state" "${ROOT}/backups" "${ROOT}/data"
chown root:btcquant "${ROOT}/.env"
chmod 640 "${ROOT}/.env"

echo "── Construction de la release ──"
git -C "${SOURCE}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "Refus: une source Git vérifiable est obligatoire." >&2
  exit 1
}
[ -z "$(git -C "${SOURCE}" status --porcelain --untracked-files=all)" ] || {
  echo "Refus: source Git dirty ou contenant un fichier non suivi." >&2
  exit 1
}
RELEASE_ID="${BTCQUANT_TARGET_SHA:-$(git -C "${SOURCE}" rev-parse HEAD)}"
[[ "${RELEASE_ID}" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Refus: SHA Git complet requis." >&2
  exit 1
}
REMOTE_URL="$(git -C "${SOURCE}" remote get-url "${DEPLOY_REMOTE}" 2>/dev/null || true)"
[ -n "${REMOTE_URL}" ] || {
  echo "Refus: remote ${DEPLOY_REMOTE} absente." >&2
  exit 1
}
PYTHONPATH="${SOURCE}/src" /usr/bin/python3 -c '
import sys
from btcquant.deployment import validate_canonical_repository

validate_canonical_repository(sys.argv[1], sys.argv[2])
' "${REMOTE_URL}" "${CANONICAL_REPOSITORY}"
git -C "${SOURCE}" fetch "${DEPLOY_REMOTE}" --prune
REMOTE_REF="${DEPLOY_REMOTE}/${DEPLOY_BRANCH}"
[ "$(git -C "${SOURCE}" rev-parse "${REMOTE_REF}")" = "${RELEASE_ID}" ] || {
  echo "Refus: la cible doit être exactement ${REMOTE_REF} canonique." >&2
  exit 1
}
git -C "${SOURCE}" merge-base --is-ancestor "${RELEASE_ID}" "${REMOTE_REF}" || {
  echo "Refus: SHA cible non atteignable depuis ${REMOTE_REF}." >&2
  exit 1
}
command -v uv >/dev/null || {
  echo "Refus: uv est requis pour un build --frozen." >&2
  exit 1
}
TARGET="$(bash "${SOURCE}/deploy/create-release.sh" "${SOURCE}" "${RELEASE_ID}")"
BTCQUANT_ROOT="${ROOT}" BTCQUANT_CURRENT="${TARGET}" bash "${TARGET}/deploy/preflight.sh"
ln -sfn "${TARGET}" "${ROOT}/.current-next"
mv -Tf "${ROOT}/.current-next" "${ROOT}/current"

echo "── Vérification et activation systemd ──"
systemd-analyze verify "${ROOT}/current/deploy/"*.service \
  "${ROOT}/current/deploy/"*.timer
cp "${ROOT}/current/deploy/"btcquant-*.service \
  "${ROOT}/current/deploy/"btcquant-*.timer /etc/systemd/system/
install -o root -g root -m 0755 "${ROOT}/current/deploy/rebalance-root.sh" \
  /usr/local/libexec/btcquant-rebalance
systemctl daemon-reload
systemctl enable --now btcquant-trend btcquant-carry btcquant-dashboard btcquant-shadow
systemctl enable --now btcquant-digest.timer btcquant-weekly.timer \
  btcquant-watchdog.timer btcquant-backup.timer btcquant-rebalance.timer \
  btcquant-rebalance-pending.timer btcquant-compact.timer

systemctl is-active --quiet btcquant-dashboard
curl --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:8666/healthz >/dev/null

echo "Installation terminée : ${RELEASE_ID}"
echo "Dashboard local : http://127.0.0.1:8666/login"
echo "Un reverse proxy TLS reste obligatoire pour tout accès externe."
