#!/usr/bin/env bash
# Archive quotidienne de state/ (track record paper), conservée 30 jours.
# Appelé par un timer systemd. Chemin projet auto-détecté.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${ROOT}/backups"
OFFHOST_REMOTE="${BACKUP_OFFHOST_REMOTE:-origin}"
OFFHOST_BRANCH="${BACKUP_OFFHOST_BRANCH:-backups}"
if [ -z "${BACKUP_ENCRYPTION_KEY:-}" ] || [[ "${BACKUP_ENCRYPTION_KEY:-}" =~ ^[[:space:]]*$ ]]; then
  echo "BACKUP_ENCRYPTION_KEY is required; refusing plaintext backup publication" >&2
  exit 2
fi
mkdir -p "${BACKUP_DIR}"
# This job is a backup reader. Compaction is a separate scheduled writer and
# must never mutate the authoritative database as a side effect of backup.
PYBIN="${ROOT}/venv/bin/python"; [ -x "${PYBIN}" ] || PYBIN="python3"
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
# Generic state evidence may be copied, but SQLite files and every sidecar are
# never eligible for raw rsync. Each allow-listed database below is captured
# through the Online Backup API instead.
SQLITE_EXCLUDES=(
  --exclude '*.db' --exclude '*.db-*'
  --exclude '*.sqlite' --exclude '*.sqlite-*'
  --exclude '*.sqlite3' --exclude '*.sqlite3-*'
)
rsync -a "${SQLITE_EXCLUDES[@]}" \
  "${ROOT}/state/" "${TMP_DIR}/state/"

while IFS= read -r candidate; do
  candidate_name="$(basename "${candidate}")"
  case "${candidate_name}" in
    btcquant.db|execution-shadow.db|btcquant-testnet.db) ;;
    *) echo "Ignoring unknown SQLite-like state artifact: ${candidate_name}" >&2 ;;
  esac
done < <(
  find "${ROOT}/state" -maxdepth 1 -type f \(
    -name '*.db' -o -name '*.db-*' \
    -o -name '*.sqlite' -o -name '*.sqlite-*' \
    -o -name '*.sqlite3' -o -name '*.sqlite3-*' \
  \) -print
)

"${PYBIN}" "${ROOT}/scripts/backup_database.py" \
  "${ROOT}/state/btcquant.db" "${TMP_DIR}/state/btcquant.db"
for optional_db in execution-shadow.db btcquant-testnet.db; do
  if [ -e "${ROOT}/state/${optional_db}" ]; then
    "${PYBIN}" "${ROOT}/scripts/backup_database.py" \
      "${ROOT}/state/${optional_db}" "${TMP_DIR}/state/${optional_db}"
  fi
done
PLAIN_ARCHIVE="${TMP_DIR}/state-${STAMP}.tar.gz"
tar -czf "${PLAIN_ARCHIVE}" -C "${TMP_DIR}" state
ARCHIVE="${BACKUP_DIR}/state-${STAMP}.tar.gz.enc"
ENCRYPTED_TMP="${TMP_DIR}/state-${STAMP}.tar.gz.enc"
if [ -e "${ARCHIVE}" ]; then
  echo "Archive finale deja presente : ${ARCHIVE}" >&2
  exit 3
fi
openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
  -in "${PLAIN_ARCHIVE}" -out "${ENCRYPTED_TMP}" \
  -pass env:BACKUP_ENCRYPTION_KEY
# Verify that decryption reproduces the plaintext before publishing the final name.
ROUNDTRIP_ARCHIVE="${TMP_DIR}/roundtrip.tar.gz"
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
  -in "${ENCRYPTED_TMP}" -out "${ROUNDTRIP_ARCHIVE}" \
  -pass env:BACKUP_ENCRYPTION_KEY
cmp --silent "${PLAIN_ARCHIVE}" "${ROUNDTRIP_ARCHIVE}"
# Do not report a completed archive until its bytes and containing directory
# have been durably flushed.
fsync_file() {
  "${PYBIN}" -c 'import os, sys; fd=os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)' "$1"
}
fsync_directory() {
  "${PYBIN}" -c 'import os, sys; flags=os.O_RDONLY | getattr(os, "O_DIRECTORY", 0); fd=os.open(sys.argv[1], flags); os.fsync(fd); os.close(fd)' "$1"
}
fsync_file "${ENCRYPTED_TMP}"
mv --no-clobber "${ENCRYPTED_TMP}" "${ARCHIVE}"
fsync_file "${ARCHIVE}"
fsync_directory "${BACKUP_DIR}"
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
    if find . -maxdepth 1 -type f \
      \( -name 'state-*.tar.gz' -o -name '*.db' -o -name '*.db-*' \
      -o -name '*.sqlite' -o -name '*.sqlite-*' \
      -o -name '*.sqlite3' -o -name '*.sqlite3-*' -o -name '.env' \) \
      -print -quit | grep -q .; then
      echo "Refusing: off-host repository contains an unauthorized plaintext artifact" >&2
      exit 4
    fi
    if git ls-files | grep -Ev '^state-[0-9]{8}-[0-9]{4}\.tar\.gz\.enc$' | grep -q .; then
      echo "Refusing: off-host repository contains a non-whitelisted tracked file" >&2
      exit 5
    fi
    cp -- "${ARCHIVE}" .
    find . -maxdepth 1 -name 'state-*.tar.gz.enc' -mtime +30 -delete
    git add -u -- '*.tar.gz.enc'
    git add -- "$(basename "${ARCHIVE}")"
    git -c commit.gpgsign=false commit -q -m "backup ${STAMP}" || exit 0
    git push -q "${OFFHOST_REMOTE}" "HEAD:refs/heads/${OFFHOST_BRANCH}"
    echo "Off-host backup pushed."
  ) || echo "Off-host push failed; local encrypted backup remains available" >&2
fi
