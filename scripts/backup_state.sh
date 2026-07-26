#!/usr/bin/env bash
# Archive quotidienne de state/ (track record paper), conservée 30 jours.
# Appelé par un timer systemd. Chemin projet auto-détecté.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${ROOT}/backups"
mkdir -p "${BACKUP_DIR}"
# compaction des historiques d'équity avant archivage
PYBIN="${ROOT}/venv/bin/python"; [ -x "${PYBIN}" ] || PYBIN="python3"
"${PYBIN}" "${ROOT}/scripts/compact_equity.py" || true
STAMP="$(date -u +%Y%m%d-%H%M)"
TMP_DIR="$(mktemp -d /tmp/btcquant-backup.XXXXXX)"
cleanup() {
  case "${TMP_DIR}" in
    /tmp/btcquant-backup.*) rm -rf -- "${TMP_DIR}" ;;
    *) echo "Refus de supprimer un chemin temporaire inattendu : ${TMP_DIR}" >&2 ;;
  esac
}
trap cleanup EXIT
mkdir -p "${TMP_DIR}/state"
rsync -a \
  --exclude 'btcquant.db' --exclude 'btcquant.db-wal' --exclude 'btcquant.db-shm' \
  "${ROOT}/state/" "${TMP_DIR}/state/"
"${PYBIN}" "${ROOT}/scripts/backup_database.py" \
  "${ROOT}/state/btcquant.db" "${TMP_DIR}/state/btcquant.db"
PLAIN_ARCHIVE="${TMP_DIR}/state-${STAMP}.tar.gz"
tar -czf "${PLAIN_ARCHIVE}" -C "${TMP_DIR}" state
if [ -n "${BACKUP_ENCRYPTION_KEY:-}" ]; then
  ARCHIVE="${BACKUP_DIR}/state-${STAMP}.tar.gz.enc"
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
    -in "${PLAIN_ARCHIVE}" -out "${ARCHIVE}" \
    -pass env:BACKUP_ENCRYPTION_KEY
  # Vérifie immédiatement qu'une restauration avec la clé produit exactement
  # l'archive créée. Une sauvegarde non déchiffrable ne doit pas être annoncée.
  ROUNDTRIP_ARCHIVE="${TMP_DIR}/roundtrip.tar.gz"
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
    -in "${ARCHIVE}" -out "${ROUNDTRIP_ARCHIVE}" \
    -pass env:BACKUP_ENCRYPTION_KEY
  cmp --silent "${PLAIN_ARCHIVE}" "${ROUNDTRIP_ARCHIVE}"
else
  # Disponibilité locale conservée, mais aucune copie distante en clair.
  ARCHIVE="${BACKUP_DIR}/state-${STAMP}.tar.gz"
  mv "${PLAIN_ARCHIVE}" "${ARCHIVE}"
  echo "⚠ BACKUP_ENCRYPTION_KEY absente : archive locale seulement" >&2
fi
# purge des archives de plus de 30 jours
find "${BACKUP_DIR}" \( -name 'state-*.tar.gz' -o -name 'state-*.tar.gz.enc' \) \
  -mtime +30 -delete
echo "Sauvegarde : ${ARCHIVE}"

# ── copie hors-site : branche `backups` du dépôt GitHub (clé de déploiement,
# ~btcquant/.ssh/backup_deploy). Best-effort : un échec réseau ne doit jamais
# faire échouer la sauvegarde locale. ────────────────────────────────────────
REPO="${ROOT}/backups-repo"
if [ -d "${REPO}/.git" ] && [[ "${ARCHIVE}" == *.enc ]]; then
  (
    cd "${REPO}"
    cp "${ARCHIVE}" .
    find . -maxdepth 1 -name 'state-*.tar.gz.enc' -mtime +30 -delete
    git add -A
    git -c commit.gpgsign=false commit -q -m "backup ${STAMP}" || exit 0
    git push -q origin backups
    echo "Copie hors-site poussée (branche backups)."
  ) || echo "⚠ push hors-site échoué — la sauvegarde locale reste valide"
fi
