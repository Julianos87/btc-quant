#!/usr/bin/env bash
# Contrôles bloquants de l'hôte avant activation ou mise à jour.
set -euo pipefail

ROOT=/opt/btcquant
CURRENT="${ROOT}/current"

if [ "$(id -u)" -ne 0 ]; then
  echo "Le preflight doit être exécuté par root." >&2
  exit 1
fi
if [ ! -L "${CURRENT}" ] || [ ! -x "${CURRENT}/venv/bin/python" ]; then
  echo "Release current ou virtualenv absent." >&2
  exit 1
fi
if [ ! -f "${ROOT}/.env" ] ||
  ! grep -q '^BACKUP_ENCRYPTION_KEY=.\+' "${ROOT}/.env"; then
  echo ".env ou clé de sauvegarde absente." >&2
  exit 1
fi
if [ "$(stat -c '%U:%G:%a' "${ROOT}/.env")" != "root:btcquant:640" ]; then
  echo "Permissions .env attendues : root:btcquant:640." >&2
  exit 1
fi

NTP_SYNC="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
if [ "${NTP_SYNC}" != "yes" ]; then
  echo "Horloge non synchronisée par NTP : déploiement refusé." >&2
  exit 1
fi

AVAILABLE_KB="$(df -Pk "${ROOT}" | awk 'NR==2 {print $4}')"
if [ "${AVAILABLE_KB:-0}" -lt 1048576 ]; then
  echo "Moins de 1 Gio disponible sous ${ROOT}." >&2
  exit 1
fi

if [ -f "${ROOT}/state/btcquant.db" ]; then
  RESULT="$(
    "${CURRENT}/venv/bin/python" -c \
      "import sqlite3; c=sqlite3.connect('file:/opt/btcquant/state/btcquant.db?mode=ro', uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0])"
  )"
  if [ "${RESULT}" != "ok" ]; then
    echo "SQLite integrity_check a échoué : ${RESULT}" >&2
    exit 1
  fi
fi

echo "Preflight hôte : OK"
