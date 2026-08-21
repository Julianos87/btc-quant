#!/usr/bin/env bash
# Wrapper privilégié installé hors de l'arbre applicatif modifiable.
set -euo pipefail

ROOT=/opt/btcquant
CURRENT="${ROOT}/current"
MODE="${1:-monthly}"

if [ "${MODE}" = "--pending-only" ]; then
  set +e
  runuser -u btcquant -- env BTCQUANT_ROOT="${ROOT}" \
    "${CURRENT}/venv/bin/btcquant-rebalance" --check-pending
  pending_status=$?
  set -e
  case "${pending_status}" in
    0) ;;
    3) exit 0 ;;
    *) exit "${pending_status}" ;;
  esac
elif [ "${MODE}" != "monthly" ]; then
  echo "Usage : btcquant-rebalance [--pending-only]" >&2
  exit 2
fi

trap 'systemctl start btcquant-trend btcquant-carry' EXIT
systemctl stop btcquant-trend btcquant-carry
sleep 3

if [ "${MODE}" = "--pending-only" ]; then
  args=(--apply --deposit 0)
else
  DEPOSIT="$(grep -s '^MONTHLY_DEPOSIT=' "${ROOT}/.env" | cut -d= -f2 || true)"
  args=(
    --apply
    --deposit "${DEPOSIT:-0}"
    --deposit-id "monthly:$(date -u +%Y-%m)"
  )
fi

runuser -u btcquant -- env BTCQUANT_ROOT="${ROOT}" \
  "${CURRENT}/venv/bin/btcquant-rebalance" "${args[@]}"
