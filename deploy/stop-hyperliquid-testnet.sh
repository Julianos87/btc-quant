#!/usr/bin/env bash
# Coupe le runner testnet et retire son autorisation persistante.
set -euo pipefail

ROOT=/opt/btcquant
APPROVAL="${ROOT}/state/HYPERLIQUID_TESTNET_APPROVED"

if [ "$(id -u)" -ne 0 ]; then
  echo "Ce script doit être exécuté par root." >&2
  exit 1
fi

systemctl disable --now btcquant-hyperliquid-watchdog.timer || true
systemctl disable --now btcquant-hyperliquid-testnet.service || true
if [ -f "${APPROVAL}" ]; then
  rm -- "${APPROVAL}"
fi
echo "Runner Hyperliquid TESTNET arrêté et autorisation retirée."
