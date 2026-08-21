#!/usr/bin/env bash
# Construit une release immuable sans modifier current/previous.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Ce script doit être exécuté par root." >&2
  exit 1
fi
if [ "$#" -ne 2 ]; then
  echo "Usage : create-release.sh <source> <commit-sha>" >&2
  exit 2
fi

SOURCE="$(readlink -f "$1")"
RELEASE_ID="$2"
ROOT="${BTCQUANT_ROOT:-/opt/btcquant}"
RELEASES="${ROOT}/releases"
DEPLOY_REMOTE="${DEPLOY_REMOTE:-}"

# The deployment orchestrator is root, while the canonical checkout is owned
# by btcquant. Keep Git trust process-local and scoped to this exact source path.
source_git() {
  git -c "safe.directory=${SOURCE}" -C "${SOURCE}" "$@"
}

case "${RELEASE_ID}" in
  *[!0-9a-f]*|"")
    echo "Identifiant de release invalide : ${RELEASE_ID}" >&2
    exit 2
    ;;
esac
if [ "${#RELEASE_ID}" -ne 40 ] || [ ! -d "${SOURCE}" ]; then
  echo "Source ou identifiant de release invalide : SHA complet requis." >&2
  exit 2
fi

SOURCE_HEAD="$(source_git rev-parse HEAD)"
if [ "${SOURCE_HEAD}" != "${RELEASE_ID}" ]; then
  echo "Le clone source ne correspond pas au SHA demandé." >&2
  exit 1
fi
source_git diff-tree --check -r "${RELEASE_ID}"
if [ -z "${DEPLOY_REMOTE}" ]; then
  echo "Refus: DEPLOY_REMOTE doit identifier la remote canonique." >&2
  exit 1
fi
GIT_TREE="$(source_git rev-parse "${RELEASE_ID}^{tree}")"
ORIGIN="$(source_git remote get-url "${DEPLOY_REMOTE}")"
TARGET="${RELEASES}/${RELEASE_ID}"
if [ -e "${TARGET}" ]; then
  if [ ! -f "${TARGET}/release-manifest.json" ]; then
    echo "Release existante sans manifeste : refus de la réutiliser." >&2
    exit 1
  fi
  # A pre-existing tree is reused only when its launcher runtime is already
  # valid. Never mutate an existing release to make the contract pass.
  if [ ! -x "${TARGET}/venv/bin/python" ]; then
    echo "Release existante : interpréteur python absent, refus de réutilisation." >&2
    exit 1
  fi
  if ! "${TARGET}/venv/bin/python" "${TARGET}/scripts/relocate_venv_launchers.py" \
    --validate-existing --release "${TARGET}"; then
    echo "Release existante : launchers runtime invalides, refus de réutilisation." >&2
    exit 1
  fi
  echo "${TARGET}"
  exit 0
fi

UV_BIN="$(
  UV_BIN="${UV_BIN:-}" bash "${SOURCE}/deploy/resolve-uv.sh"
)"
echo "Construction staging pour ${RELEASE_ID}" >&2
mkdir -p "${RELEASES}" "${ROOT}/state" "${ROOT}/backups" "${ROOT}/data"
STAGING="${RELEASES}/.${RELEASE_ID}.$$"
NEW_RELEASE_CREATED=0
cleanup() {
  if [ "${NEW_RELEASE_CREATED}" = 1 ] && [ -e "${TARGET}" ]; then
    echo "RELEASE BUILD REFUSED : launchers post-move invalides." >&2
    if /usr/bin/python3 "${TARGET}/scripts/relocate_venv_launchers.py" \
      --quarantine-new --release "${TARGET}" --root "${ROOT}"; then
      echo "Release nouvellement créée mise en quarantaine (jamais current/previous)." >&2
    else
      echo "Quarantaine de la release nouvellement créée refusée." >&2
    fi
    return
  fi
  case "${STAGING}" in
    "${RELEASES}"/.*) rm -rf -- "${STAGING}" ;;
    *) echo "Refus de nettoyer un chemin inattendu : ${STAGING}" >&2 ;;
  esac
}
trap cleanup EXIT
mkdir -p "${STAGING}"

rsync -a \
  --exclude .git --exclude .venv --exclude venv --exclude __pycache__ \
  --exclude state --exclude backups --exclude backups-repo \
  --exclude .env --exclude /data --exclude reports \
  --exclude .pytest_cache --exclude .mypy_cache --exclude .ruff_cache \
  --exclude .coverage --exclude .uv-cache --exclude .hypothesis \
  --exclude .validation-venv \
  "${SOURCE}/" "${STAGING}/"

# Le lockfile est la source de vérité; aucune résolution ni mise à niveau
# implicite n'est autorisée pendant un build de release.
UV_PROJECT_ENVIRONMENT="${STAGING}/venv" "${UV_BIN}" sync \
  --frozen --no-dev --no-editable --python 3.12 --directory "${STAGING}"

# Les scripts console d'un virtualenv contiennent un shebang absolu. Comme la
# release est construite sous STAGING puis renommée atomiquement, ces shebangs
# doivent viser TARGET avant le mv final.
"${STAGING}/venv/bin/python" "${STAGING}/scripts/relocate_venv_launchers.py" \
  --venv "${STAGING}/venv" \
  --old-prefix "${STAGING}" \
  --new-prefix "${TARGET}"

"${STAGING}/venv/bin/python" -m compileall -q "${STAGING}/src" "${STAGING}/dashboard"
# Validation and import smoke run before the runtime state/data/backups
# symlinks exist, so a leaked BTCQUANT_ROOT or an import-time StateStore
# cannot write through to the live runtime root.
env \
  -u BTCQUANT_ROOT \
  -u BTCQUANT_CURRENT \
  -u BTCQUANT_DATABASE \
  -u BTCQUANT_CLONE \
  bash "${STAGING}/deploy/validate-release.sh" "${STAGING}" "${UV_BIN}"
(
  cd "${STAGING}"
  env \
    -u BTCQUANT_ROOT \
    -u BTCQUANT_CURRENT \
    -u BTCQUANT_DATABASE \
    -u BTCQUANT_CLONE \
    "${STAGING}/venv/bin/python" -c \
    "import dashboard.app; from btcquant.config import load_config; load_config('environments/paper/config.yaml')"
)
# Tests may have created a real state/ directory under staging. Remove any
# leftover before installing the runtime symlinks required after activation.
for runtime_link in state backups data backups-repo; do
  rm -rf -- "${STAGING}/${runtime_link}"
done
ln -s ../../state "${STAGING}/state"
ln -s ../../backups "${STAGING}/backups"
ln -s ../../data "${STAGING}/data"
ln -s ../../backups-repo "${STAGING}/backups-repo"
"${STAGING}/venv/bin/python" "${STAGING}/scripts/create_release_manifest.py" \
  --release "${STAGING}" \
  --git-sha "${RELEASE_ID}" \
  --git-tree "${GIT_TREE}" \
  --origin "${ORIGIN}" \
  --python-version "$(${STAGING}/venv/bin/python --version)" \
  --uv-version "$("${UV_BIN}" --version)"

if [ ! -f "${STAGING}/release-manifest.json" ]; then
  echo "Manifeste de release absent après génération." >&2
  exit 1
fi

# Le code reste détenu par root (release immuable), mais les services systemd
# s'exécutent sous btcquant et doivent pouvoir traverser/lire la release.
chown -R root:btcquant "${STAGING}"
chmod -R o-rwx "${STAGING}"
chmod +x "${STAGING}/scripts/backup_state.sh" "${STAGING}/deploy/"*.sh
mv "${STAGING}" "${TARGET}"
NEW_RELEASE_CREATED=1
# Static shebang rewrite is not enough: the OS must be able to exec the
# launchers from the FINAL target path before the release is advertised.
if ! /usr/bin/python3 "${TARGET}/scripts/relocate_venv_launchers.py" \
  --smoke --release "${TARGET}"; then
  echo "RELEASE BUILD REFUSED : smoke post-move des launchers." >&2
  exit 1
fi
trap - EXIT
echo "${TARGET}"
