#!/usr/bin/env bash
# Première installation versionnée de btcquant sur Ubuntu/Debian.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Ce script doit être exécuté par root." >&2
  exit 1
fi

SOURCE="$(pwd -P)"
ROOT=/opt/btcquant

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
if git -C "${SOURCE}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  RELEASE_ID="$(git -C "${SOURCE}" rev-parse HEAD)"
else
  RELEASE_ID="$(date -u +%Y%m%d%H%M%S)"
fi
TARGET="$(bash "${SOURCE}/deploy/create-release.sh" "${SOURCE}" "${RELEASE_ID}")"
ln -sfn "${TARGET}" "${ROOT}/.current-next"
mv -Tf "${ROOT}/.current-next" "${ROOT}/current"
bash "${ROOT}/current/deploy/preflight.sh"

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
