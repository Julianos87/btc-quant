#!/usr/bin/env bash
# Validation complète d'une release staging, avant son renommage immuable.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: validate-release.sh <release-staging> <uv-bin>" >&2
  exit 2
fi
RELEASE="$(readlink -f "$1")"
UV_BIN="$2"
[ -d "${RELEASE}" ] || { echo "Release staging absente." >&2; exit 1; }
[ "${UV_BIN#/}" != "${UV_BIN}" ] || { echo "uv doit être un chemin absolu résolu." >&2; exit 1; }
[ -x "${UV_BIN}" ] || { echo "uv absent ou non exécutable." >&2; exit 1; }

VALIDATION_ENV="${RELEASE}/.validation-venv"
TEMP_EXPORT="$(mktemp /tmp/btcquant-export.XXXXXX)"
cleanup() {
  rm -f -- "${TEMP_EXPORT}"
  rm -rf -- "${VALIDATION_ENV}"
}
trap cleanup EXIT

# L'environnement de validation est séparé du venv runtime --no-dev et est
# supprimé avant l'activation de la release.
UV_PROJECT_ENVIRONMENT="${VALIDATION_ENV}" "${UV_BIN}" sync --frozen --dev \
  --python 3.12 --directory "${RELEASE}"
export PATH="$(dirname "${UV_BIN}"):${PATH}"
cd "${RELEASE}"

"${VALIDATION_ENV}/bin/pytest" -q \
  --cov=btcquant --cov-branch --cov-fail-under=80
"${VALIDATION_ENV}/bin/ruff" check .
"${VALIDATION_ENV}/bin/ruff" format --check .
"${VALIDATION_ENV}/bin/mypy" src
command -v node >/dev/null
node --check dashboard/static/dashboard.js
node --check dashboard/static/effects.js
bash -n deploy/install.sh deploy/update.sh deploy/create-release.sh \
  deploy/preflight.sh deploy/migrate.sh deploy/rebalance-root.sh \
  deploy/resolve-uv.sh \
  deploy/start-hyperliquid-testnet.sh deploy/stop-hyperliquid-testnet.sh \
  scripts/backup_state.sh
"${UV_BIN}" export --locked --no-default-groups --group exchange --group dashboard \
  --no-header --no-emit-project --output-file "${TEMP_EXPORT}"
diff -u requirements.txt "${TEMP_EXPORT}"
"${VALIDATION_ENV}/bin/python" scripts/check_sbom.py
"${VALIDATION_ENV}/bin/python" scripts/check_baseline_provenance.py
"${VALIDATION_ENV}/bin/pip-audit" -r requirements.txt --disable-pip \
  --progress-spinner off --cache-dir .uv-cache/pip-audit --timeout 15 -s osv
