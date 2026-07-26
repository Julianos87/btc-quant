#!/usr/bin/env bash
# Portail explicite paper PASS -> smoke test -> campagne P1 Hyperliquid testnet.
set -euo pipefail

ROOT=/opt/btcquant
CURRENT="${ROOT}/current"
ENV_FILE="${ROOT}/.env"
APPROVAL="${ROOT}/state/HYPERLIQUID_TESTNET_APPROVED"

if [ "$(id -u)" -ne 0 ]; then
  echo "Ce portail doit être exécuté par root." >&2
  exit 1
fi
if [ "${1:-}" != "--i-accept-hyperliquid-testnet-orders" ]; then
  echo "Usage : $0 --i-accept-hyperliquid-testnet-orders" >&2
  exit 2
fi
if [ ! -x "${CURRENT}/venv/bin/btcquant-readiness" ] || [ ! -f "${ENV_FILE}" ]; then
  echo "Release ou fichier de secrets absent." >&2
  exit 1
fi
if ! grep -Eq '^HYPERLIQUID_WALLET_ADDRESS=0x[0-9a-fA-F]{40}$' "${ENV_FILE}"; then
  echo "HYPERLIQUID_WALLET_ADDRESS absente ou invalide." >&2
  exit 1
fi
if ! grep -Eq '^HYPERLIQUID_PRIVATE_KEY=0x[0-9a-fA-F]{64}$' "${ENV_FILE}"; then
  echo "HYPERLIQUID_PRIVATE_KEY (API wallet dédié) absente ou invalide." >&2
  exit 1
fi
if ! grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' "${ENV_FILE}" ||
  ! grep -Eq '^TELEGRAM_CHAT_ID=.+$' "${ENV_FILE}"; then
  echo "Alertes Telegram obligatoires pour la campagne P1." >&2
  exit 1
fi

"${CURRENT}/venv/bin/btcquant-readiness" status \
  --database "${ROOT}/state/btcquant.db" >/dev/null

# Le fichier est root:btcquant 640 : le sourcer ne publie aucune valeur.
set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a
export BTCQUANT_ENABLE_TESTNET=I_ACCEPT_TESTNET_ORDERS

if ! sudo -u btcquant "${CURRENT}/venv/bin/python" -c \
  "from btcquant.execution.state_store import StateStore; s=StateStore('${ROOT}/state/btcquant-testnet.db'); raise SystemExit(0 if s.active_qualification_campaign() else 1)"; then
  sudo -u btcquant "${CURRENT}/venv/bin/btcquant-readiness" start \
    --profile testnet-p1 --database "${ROOT}/state/btcquant-testnet.db"
fi

sudo -u btcquant --preserve-env=HYPERLIQUID_WALLET_ADDRESS,HYPERLIQUID_PRIVATE_KEY,\
BTCQUANT_ENABLE_TESTNET,TELEGRAM_BOT_TOKEN,TELEGRAM_CHAT_ID \
  "${CURRENT}/venv/bin/python" "${CURRENT}/scripts/test_testnet.py"

install -o root -g btcquant -m 0640 /dev/null "${APPROVAL}"
systemctl stop btcquant-trend.service
systemctl enable --now btcquant-hyperliquid-testnet.service
systemctl enable --now btcquant-hyperliquid-watchdog.timer
systemctl is-active --quiet btcquant-hyperliquid-testnet.service

echo "Hyperliquid TESTNET actif. Campagne P1 : ${ROOT}/state/btcquant-testnet.db"
echo "Arrêt d'urgence : sudo ${CURRENT}/deploy/stop-hyperliquid-testnet.sh"
