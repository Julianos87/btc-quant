#!/usr/bin/env bash
# Construit une release immuable sans modifier le lien /opt/btcquant/current.
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
ROOT=/opt/btcquant
RELEASES="${ROOT}/releases"

case "${RELEASE_ID}" in
  *[!0-9a-f]*|"")
    echo "Identifiant de release invalide : ${RELEASE_ID}" >&2
    exit 2
    ;;
esac
if [ "${#RELEASE_ID}" -lt 7 ] || [ ! -d "${SOURCE}" ]; then
  echo "Source ou identifiant de release invalide." >&2
  exit 2
fi

TARGET="${RELEASES}/${RELEASE_ID}"
if [ -d "${TARGET}" ]; then
  echo "${TARGET}"
  exit 0
fi

mkdir -p "${RELEASES}" "${ROOT}/state" "${ROOT}/backups" "${ROOT}/data"
STAGING="${RELEASES}/.${RELEASE_ID}.$$"
cleanup() {
  case "${STAGING}" in
    /opt/btcquant/releases/.*) rm -rf -- "${STAGING}" ;;
    *) echo "Refus de nettoyer un chemin inattendu : ${STAGING}" >&2 ;;
  esac
}
trap cleanup EXIT
mkdir -p "${STAGING}"

rsync -a \
  --exclude .git --exclude .venv --exclude venv --exclude __pycache__ \
  --exclude state --exclude backups --exclude backups-repo \
  --exclude .env --exclude data --exclude reports \
  "${SOURCE}/" "${STAGING}/"

ln -s ../../state "${STAGING}/state"
ln -s ../../backups "${STAGING}/backups"
ln -s ../../data "${STAGING}/data"
ln -s ../../backups-repo "${STAGING}/backups-repo"

python3 -m venv "${STAGING}/venv"
"${STAGING}/venv/bin/pip" install --quiet --upgrade pip
"${STAGING}/venv/bin/pip" install --quiet -r "${STAGING}/requirements.txt"
"${STAGING}/venv/bin/pip" install --quiet --no-deps "${STAGING}"

# Les scripts console d'un virtualenv contiennent un shebang absolu. Comme la
# release est construite sous STAGING puis renommée atomiquement, ces shebangs
# doivent viser TARGET avant le mv final.
while IFS= read -r launcher; do
  sed -i \
    "1s|^#!${STAGING}/venv/bin/python|#!${TARGET}/venv/bin/python|" \
    "${launcher}"
done < <(
  find "${STAGING}/venv/bin" -maxdepth 1 -type f \
    -exec grep -Il "^#!${STAGING}/venv/bin/python" {} +
)
if grep -RIl "^#!${STAGING}/" "${STAGING}/venv/bin" | grep -q .; then
  echo "Un lanceur de virtualenv référence encore le staging." >&2
  exit 1
fi

"${STAGING}/venv/bin/python" -m compileall -q "${STAGING}/src" "${STAGING}/dashboard"
(
  cd "${STAGING}"
  "${STAGING}/venv/bin/python" -c \
    "import dashboard.app; from btcquant.config import load_config; load_config('environments/paper/config.yaml')"
)

# Le code reste détenu par root (release immuable), mais les services systemd
# s'exécutent sous btcquant et doivent pouvoir traverser/lire la release.
chown -R root:btcquant "${STAGING}"
chmod -R o-rwx "${STAGING}"
chmod +x "${STAGING}/scripts/backup_state.sh" "${STAGING}/deploy/"*.sh
mv "${STAGING}" "${TARGET}"
trap - EXIT
echo "${TARGET}"
