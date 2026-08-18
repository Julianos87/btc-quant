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

VALIDATION_ROOT="$(mktemp -d /tmp/btcquant-release-validation.XXXXXX)"
VALIDATION_BIN="${VALIDATION_ROOT}/bin"
VALIDATION_ENV="${VALIDATION_ROOT}/venv"
PIP_AUDIT_CACHE="${VALIDATION_ROOT}/pip-audit"
MYPY_CACHE="${VALIDATION_ROOT}/mypy-cache"
MYPY_ENV="${VALIDATION_ROOT}/mypy-venv"
COVERAGE_FILE="${VALIDATION_ROOT}/.coverage"
TEMP_EXPORT=""
cleanup() {
  rm -f -- "${TEMP_EXPORT}"
  rm -rf -- "${VALIDATION_ROOT}"
}
trap cleanup EXIT

TEMP_EXPORT="$(mktemp "${VALIDATION_ROOT}/export.XXXXXX")"

# Keep all validation-only artifacts outside the immutable release staging.
UV_PROJECT_ENVIRONMENT="${VALIDATION_ENV}" "${UV_BIN}" sync --frozen --dev \
  --python 3.12 --directory "${RELEASE}"
mkdir -p "${VALIDATION_BIN}"
cp -- "${UV_BIN}" "${VALIDATION_BIN}/uv"
chmod +x "${VALIDATION_BIN}/uv"
export PATH="${VALIDATION_BIN}:${PATH}"
cd "${RELEASE}"

# Orchestration exports BTCQUANT_ROOT to the runtime root (releases/, state/,
# backups/, data/). That root has no repository source. Several entrypoints
# resolve environments/paper/config.yaml from BTCQUANT_ROOT at import time,
# so pytest collection would look under the runtime root and could also point
# STATE at the live database through the staging symlink. Validation must
# stay hermetic to this staging tree.
run_isolated() {
  env \
    -u BTCQUANT_ROOT \
    -u BTCQUANT_CURRENT \
    -u BTCQUANT_DATABASE \
    -u BTCQUANT_CLONE \
    "$@"
}

run_isolated env COVERAGE_FILE="${COVERAGE_FILE}" \
  "${VALIDATION_ENV}/bin/pytest" -q \
  -p no:cacheprovider --cov=btcquant --cov-branch --cov-fail-under=80
run_isolated "${VALIDATION_ENV}/bin/ruff" check --no-cache .
run_isolated "${VALIDATION_ENV}/bin/ruff" format --check --no-cache .
run_isolated env UV_PROJECT_ENVIRONMENT="${MYPY_ENV}" \
  "${UV_BIN}" run --locked --python 3.11 mypy --cache-dir "${MYPY_CACHE}" src
command -v node >/dev/null
node --check dashboard/static/dashboard.js
node --check dashboard/static/effects.js
bash -n deploy/install.sh deploy/update.sh deploy/create-release.sh \
  deploy/preflight.sh deploy/migrate.sh deploy/rebalance-root.sh \
  deploy/resolve-uv.sh \
  deploy/start-hyperliquid-testnet.sh deploy/stop-hyperliquid-testnet.sh \
  scripts/backup_state.sh
run_isolated "${UV_BIN}" export --locked --no-default-groups --group exchange --group dashboard \
  --no-header --no-emit-project --output-file "${TEMP_EXPORT}"
diff -u requirements.txt "${TEMP_EXPORT}"
run_isolated "${VALIDATION_ENV}/bin/python" scripts/check_sbom.py
run_isolated "${VALIDATION_ENV}/bin/python" scripts/check_baseline_provenance.py
run_isolated "${VALIDATION_ENV}/bin/pip-audit" -r requirements.txt --disable-pip \
  --progress-spinner off --cache-dir "${PIP_AUDIT_CACHE}" --timeout 15 -s osv

# No validation artifact is allowed to be published with the release.
for transient in .validation-venv .pytest_cache .mypy_cache .ruff_cache .coverage .uv-cache; do
  if [ -e "${RELEASE}/${transient}" ]; then
    echo "Artefact de validation dans la release : ${transient}" >&2
    exit 1
  fi
done
