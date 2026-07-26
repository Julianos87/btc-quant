#!/usr/bin/env bash
# Wrapper privilégié installé hors de l'arbre applicatif modifiable.
set -uo pipefail

ROOT=/opt/btcquant
CURRENT="${ROOT}/current"

trap 'systemctl start btcquant-trend btcquant-carry' EXIT
systemctl stop btcquant-trend btcquant-carry
sleep 3
DEPOSIT="$(grep -s '^MONTHLY_DEPOSIT=' "${ROOT}/.env" | cut -d= -f2)"
runuser -u btcquant -- env BTCQUANT_ROOT="${CURRENT}" \
  "${CURRENT}/venv/bin/btcquant-rebalance" \
  --apply --deposit "${DEPOSIT:-0}"
