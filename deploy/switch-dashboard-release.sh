#!/usr/bin/env bash
# Pointe atomiquement le dashboard vers une release indépendante.
# Ne modifie jamais current/previous ni aucun service de trading.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Ce script doit être exécuté par root." >&2
  exit 1
fi

ROOT="${BTCQUANT_ROOT:-/opt/btcquant}"
DASHBOARD_CURRENT="${BTCQUANT_DASHBOARD_CURRENT:-${ROOT}/dashboard-current}"
DASHBOARD_PREVIOUS="${BTCQUANT_DASHBOARD_PREVIOUS:-${ROOT}/dashboard-previous}"
LOCK_FILE="${BTCQUANT_DASHBOARD_LOCK:-${ROOT}/.dashboard-release.lock}"

mkdir -p "$(dirname "${LOCK_FILE}")"
exec 9>"${LOCK_FILE}"
flock -n 9 || {
  echo "Bascule dashboard déjà en cours." >&2
  exit 75
}

usage() {
  echo "Usage: $0 <release-path>|--rollback" >&2
  exit 2
}

validate_release() {
  local requested="$1"
  local resolved manifest_sha release_id

  resolved="$(readlink -f -- "${requested}" 2>/dev/null || true)"
  case "${resolved}" in
    "${ROOT}"/releases/*) ;;
    *)
      echo "Release dashboard refusée : chemin canonique inattendu (${resolved})." >&2
      return 1
      ;;
  esac
  release_id="$(basename "${resolved}")"
  [[ "${release_id}" =~ ^[0-9a-f]{40}$ ]] || {
    echo "Release dashboard refusée : identifiant non hexadécimal." >&2
    return 1
  }
  [ -d "${resolved}" ] || { echo "Release dashboard absente." >&2; return 1; }
  [ -f "${resolved}/release-manifest.json" ] || {
    echo "Manifeste dashboard absent." >&2
    return 1
  }
  manifest_sha="$(/usr/bin/python3 - "${resolved}/release-manifest.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("git_sha", ""))
PY
  )"
  [ "${manifest_sha}" = "${release_id}" ] || {
    echo "Manifeste dashboard incohérent avec le chemin de release." >&2
    return 1
  }
  for executable in \
    "${resolved}/venv/bin/python" \
    "${resolved}/venv/bin/gunicorn"; do
    [ -x "${executable}" ] || {
      echo "Exécutable dashboard requis absent : ${executable}." >&2
      return 1
    }
  done
  for required in \
    "${resolved}/dashboard/wsgi.py" \
    "${resolved}/dashboard/index.html" \
    "${resolved}/dashboard/static/dashboard.css" \
    "${resolved}/dashboard/static/dashboard.js" \
    "${resolved}/environments/paper/config.yaml"; do
    [ -e "${required}" ] || {
      echo "Fichier dashboard requis absent : ${required}." >&2
      return 1
    }
  done
  [ "$(readlink -f "${resolved}/state" 2>/dev/null || true)" = "${ROOT}/state" ] || {
    echo "La release dashboard ne pointe pas vers le state runtime attendu." >&2
    return 1
  }
  printf '%s\n' "${resolved}"
}

link_atomic() {
  local link="$1"
  local target="$2"
  local temporary="${link}.next.$$"
  rm -f -- "${temporary}"
  ln -s -- "${target}" "${temporary}"
  mv -Tf -- "${temporary}" "${link}"
}

[ "$#" -eq 1 ] || usage
if [ "$1" = "--rollback" ]; then
  [ -L "${DASHBOARD_PREVIOUS}" ] || {
    echo "Rollback dashboard refusé : dashboard-previous absent." >&2
    exit 1
  }
  REQUESTED="$(readlink -f -- "${DASHBOARD_PREVIOUS}")"
else
  REQUESTED="$1"
fi

TARGET="$(validate_release "${REQUESTED}")"
if [ -e "${DASHBOARD_CURRENT}" ] || [ -L "${DASHBOARD_CURRENT}" ]; then
  CURRENT_TARGET="$(readlink -f -- "${DASHBOARD_CURRENT}" 2>/dev/null || true)"
else
  CURRENT_TARGET=""
fi
if [ "${CURRENT_TARGET}" = "${TARGET}" ]; then
  echo "Release dashboard déjà active : ${TARGET}"
  exit 0
fi

if [ -n "${CURRENT_TARGET}" ]; then
  validate_release "${CURRENT_TARGET}" >/dev/null
  link_atomic "${DASHBOARD_PREVIOUS}" "${CURRENT_TARGET}"
fi
link_atomic "${DASHBOARD_CURRENT}" "${TARGET}"
echo "Release dashboard active : ${TARGET}"
