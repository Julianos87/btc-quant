#!/usr/bin/env bash
set -euo pipefail

ROOT="${BTCQUANT_ROOT:-/opt/btcquant}"
PYBIN="${ROOT}/current/venv/bin/python"
STATE_DIR="${ROOT}/state"
MARKER="${STATE_DIR}/.backup-health-alert"

notify() {
  "${PYBIN}" - "$1" <<'PY'
import sys
from btcquant.notify import notify

if not notify(sys.argv[1]):
    sys.exit(1)
PY
}

set +e
problem="$("${PYBIN}" -m btcquant.entrypoints.backup_health --root "${ROOT}" 2>&1)"
status=$?
set -e

if [ "${status}" -ne 0 ]; then
  if [ ! -e "${MARKER}" ]; then
    if notify "⛔ BTCQuant sauvegarde : ${problem}. Intervention requise."; then
      printf '%s\n' "${problem}" >"${MARKER}"
    fi
  fi
  echo "Backup health failed: ${problem}" >&2
  exit 1
fi

if [ -e "${MARKER}" ]; then
  previous="$(head -1 "${MARKER}" 2>/dev/null || true)"
  if notify "✅ BTCQuant sauvegarde rétablie (${previous})."; then
    rm -f -- "${MARKER}"
  fi
fi

printf '%s\n' "${problem}"
