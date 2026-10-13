#!/usr/bin/env bash
# Archive quotidienne de state/ (track record paper), conservée 30 jours.
# Appelé par un timer systemd.
#
# APP_ROOT     : arbre physique de la release (venv + scripts).
# RUNTIME_ROOT : BTCQUANT_ROOT obligatoire = /opt/btcquant en production.
# Jamais de repli sur <release>/state (symlink rejeté par path-safety).
set -euo pipefail
APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [ -z "${BACKUP_ENCRYPTION_KEY:-}" ] || [[ "${BACKUP_ENCRYPTION_KEY:-}" =~ ^[[:space:]]*$ ]]; then
  echo "BACKUP_ENCRYPTION_KEY is required; refusing plaintext backup publication" >&2
  exit 2
fi
if [ -z "${BTCQUANT_ROOT:-}" ]; then
  echo "BTCQUANT_ROOT is required; refusing backup through release/state" >&2
  exit 2
fi
RUNTIME_ROOT="$(cd "${BTCQUANT_ROOT}" && pwd -P)"
STATE_DIR="${RUNTIME_ROOT}/state"
DATA_DIR="${RUNTIME_ROOT}/data"
DATED_STATE_DIR="${STATE_DIR}/carry-dated-experiment"
DATED_DATA_DIR="${DATA_DIR}/carry-dated-experiment"
BACKUP_DIR="${RUNTIME_ROOT}/backups"
if [ -L "${STATE_DIR}" ]; then
  echo "Refusing: runtime state path is a symlink: ${STATE_DIR}" >&2
  exit 2
fi
if [ -L "${DATA_DIR}" ] || [ -L "${DATED_DATA_DIR}" ] || [ -L "${DATED_STATE_DIR}" ]; then
  echo "Refusing: dated Carry backup path is a symlink" >&2
  exit 2
fi
if [ -L "${BACKUP_DIR}" ]; then
  echo "Refusing: backup destination is a symlink" >&2
  exit 2
fi
OFFHOST_REMOTE="${BACKUP_OFFHOST_REMOTE:-origin}"
OFFHOST_BRANCH="${BACKUP_OFFHOST_BRANCH:-backups}"
mkdir -p "${BACKUP_DIR}"
# A single lock covers archive generation and publication. systemd normally
# serializes this oneshot, but manual/recovery starts must not pair another
# invocation's archive with its verification sidecar.
exec 8>"${BACKUP_DIR}/.backup.lock"
if ! flock -n 8; then
  echo "Backup already running; refusing overlapping archive publication" >&2
  exit 75
fi
# This job is a backup reader. Compaction is a separate scheduled writer and
# must never mutate the authoritative database as a side effect of backup.
PYBIN="${APP_ROOT}/venv/bin/python"; [ -x "${PYBIN}" ] || PYBIN="python3"
STAMP="$(date -u +%Y%m%d-%H%M)"
TMP_BASE="${TMPDIR:-/tmp}"
TMP_DIR="$(mktemp -d "${TMP_BASE%/}/btcquant-backup.XXXXXX")"
cleanup() {
  case "${TMP_DIR}" in
    "${TMP_BASE%/}"/btcquant-backup.*) rm -rf -- "${TMP_DIR}" ;;
    *) echo "Refus de supprimer un chemin temporaire inattendu : ${TMP_DIR}" >&2 ;;
  esac
}
trap cleanup EXIT
mkdir -p "${TMP_DIR}/state"
POST_BACKUP_FREE_BYTES="${BTCQUANT_BACKUP_POST_FREE_BYTES:-2684354560}"
if ! [[ "${POST_BACKUP_FREE_BYTES}" =~ ^[0-9]+$ ]]; then
  echo "BTCQUANT_BACKUP_POST_FREE_BYTES must be a non-negative integer" >&2
  exit 2
fi
# The collector publishes an archive manifest and removes live rows as one
# protected snapshot operation. Hold its cycle lock while taking the SQLite
# Online Backup snapshots. Immutable archives are mirrored separately later.
DATED_SNAPSHOT_LOCK="${DATED_STATE_DIR}/market.sqlite3.snapshot.lock"
DATED_SNAPSHOT_LOCK_ACQUIRED=false
if [ -d "${DATED_STATE_DIR}" ]; then
  exec 9>"${DATED_SNAPSHOT_LOCK}"
  flock -x 9
  DATED_SNAPSHOT_LOCK_ACQUIRED=true
fi
# Size checks happen after taking the same lock as the collector so the source
# state cannot grow or compact between the measurement and its DB snapshots.
# If staging and destination share a filesystem, account for their combined
# peak rather than passing two checks against the same free-space pool.
SOURCE_BYTES="$(du -sb -- "${STATE_DIR}" | awk '{ print $1 }')"
TMP_FREE_BYTES="$(df -B1 --output=avail "${TMP_DIR}" | tail -n 1 | tr -d '[:space:]')"
BACKUP_FREE_BYTES="$(df -B1 --output=avail "${BACKUP_DIR}" | tail -n 1 | tr -d '[:space:]')"
STAGING_REQUIRED_BYTES="$((SOURCE_BYTES * 4 + 134217728))"
TARGET_REQUIRED_BYTES="$((SOURCE_BYTES + 134217728 + POST_BACKUP_FREE_BYTES))"
if [ "$(stat -c %d "${TMP_DIR}")" = "$(stat -c %d "${BACKUP_DIR}")" ]; then
  COMBINED_REQUIRED_BYTES="$((STAGING_REQUIRED_BYTES + POST_BACKUP_FREE_BYTES))"
  if [ "${TMP_FREE_BYTES}" -lt "${COMBINED_REQUIRED_BYTES}" ]; then
    echo "Refusing backup: shared temporary/destination filesystem has ${TMP_FREE_BYTES} B, requires ${COMBINED_REQUIRED_BYTES} B including reserve." >&2
    exit 75
  fi
else
  if [ "${TMP_FREE_BYTES}" -lt "${STAGING_REQUIRED_BYTES}" ]; then
    echo "Refusing backup: temporary space ${TMP_FREE_BYTES} B < conservative staging budget ${STAGING_REQUIRED_BYTES} B." >&2
    exit 75
  fi
  if [ "${BACKUP_FREE_BYTES}" -lt "${TARGET_REQUIRED_BYTES}" ]; then
    echo "Refusing backup: destination free space ${BACKUP_FREE_BYTES} B < artifact bound plus reserve ${TARGET_REQUIRED_BYTES} B." >&2
    exit 75
  fi
fi
# Generic state evidence may be copied, but SQLite files and every sidecar are
# never eligible for raw rsync. Each allow-listed database below is captured
# through the Online Backup API instead.
SQLITE_EXCLUDES=(
  --exclude '*.db' --exclude '*.db-*'
  --exclude '*.sqlite' --exclude '*.sqlite-*'
  --exclude '*.sqlite3' --exclude '*.sqlite3-*'
)
rsync -a "${SQLITE_EXCLUDES[@]}" \
  "${STATE_DIR}/" "${TMP_DIR}/state/"

while IFS= read -r candidate; do
  candidate_name="$(basename "${candidate}")"
  case "${candidate_name}" in
    btcquant.db|execution-shadow.db|btcquant-testnet.db) ;;
    *) echo "Ignoring unknown SQLite-like state artifact: ${candidate_name}" >&2 ;;
  esac
done < <(
  find "${STATE_DIR}" -maxdepth 1 -type f \
    \( -name '*.db' -o -name '*.db-*' -o -name '*.sqlite' \
       -o -name '*.sqlite-*' -o -name '*.sqlite3' -o -name '*.sqlite3-*' \) \
    -print
)
if [ -d "${DATED_STATE_DIR}" ]; then
  while IFS= read -r candidate; do
    relative="${candidate#"${STATE_DIR}/"}"
    case "${relative}" in
      carry-dated-experiment/carry_dated.db|carry-dated-experiment/market.sqlite3) ;;
      *) echo "Ignoring unknown dated experiment SQLite artifact: ${relative}" >&2 ;;
    esac
  done < <(
    find "${DATED_STATE_DIR}" -maxdepth 1 -type f \
      \( -name '*.db' -o -name '*.db-*' -o -name '*.sqlite' \
         -o -name '*.sqlite-*' -o -name '*.sqlite3' -o -name '*.sqlite3-*' \) \
      -print
  )
fi

"${PYBIN}" "${APP_ROOT}/scripts/backup_database.py" \
  "${STATE_DIR}/btcquant.db" "${TMP_DIR}/state/btcquant.db"
for optional_db in \
  execution-shadow.db \
  btcquant-testnet.db \
  carry-dated-experiment/carry_dated.db \
  carry-dated-experiment/market.sqlite3; do
  if [ -e "${STATE_DIR}/${optional_db}" ]; then
    "${PYBIN}" "${APP_ROOT}/scripts/backup_database.py" \
      "${STATE_DIR}/${optional_db}" "${TMP_DIR}/state/${optional_db}"
  fi
done
PLAIN_ARCHIVE="${TMP_DIR}/state-${STAMP}.tar.gz"
tar -czf "${PLAIN_ARCHIVE}" -C "${TMP_DIR}" state
if ${DATED_SNAPSHOT_LOCK_ACQUIRED}; then
  flock -u 9
  exec 9>&-
fi
ARCHIVE="${BACKUP_DIR}/state-${STAMP}.tar.gz.enc"
ENCRYPTED_TMP="${TMP_DIR}/state-${STAMP}.tar.gz.enc"
VERIFICATION="${ARCHIVE}.verification.json"
VERIFICATION_TMP="${TMP_DIR}/state-${STAMP}.tar.gz.enc.verification.json"
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
# Confirm the gzip/tar stream and every member can be read from the encrypted
# bytes that will be published. Individual SQLite snapshots were integrity
# checked by backup_database.py before this archive was built.
tar -tzf "${ROUNDTRIP_ARCHIVE}" >/dev/null
"${PYBIN}" "${APP_ROOT}/scripts/verify_backup.py" \
  "${ENCRYPTED_TMP}" \
  --write-archive-verification-record "${VERIFICATION_TMP}" \
  >"${TMP_DIR}/verification-result.json"
# Do not report a completed archive until its bytes and containing directory
# and its exact-byte verification record have been durably flushed.
fsync_file() {
  "${PYBIN}" -c 'import os, sys; fd=os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)' "$1"
}
fsync_directory() {
  "${PYBIN}" -c 'import os, sys; flags=os.O_RDONLY | getattr(os, "O_DIRECTORY", 0); fd=os.open(sys.argv[1], flags); os.fsync(fd); os.close(fd)' "$1"
}
fsync_file "${ENCRYPTED_TMP}"
fsync_file "${VERIFICATION_TMP}"
mv --no-clobber "${ENCRYPTED_TMP}" "${ARCHIVE}"
mv --no-clobber "${VERIFICATION_TMP}" "${VERIFICATION}"
fsync_file "${ARCHIVE}"
fsync_file "${VERIFICATION}"
fsync_directory "${BACKUP_DIR}"
# purge des archives de plus de 30 jours
find "${BACKUP_DIR}" \( -name 'state-*.tar.gz' -o -name 'state-*.tar.gz.enc' \) \
  -mtime +30 -delete
find "${BACKUP_DIR}" -name 'state-*.tar.gz.enc.verification.json' \
  -mtime +30 -delete
echo "Sauvegarde : ${ARCHIVE}"
echo "Preuve de vérification : ${VERIFICATION}"

# Archive immutable public market evidence incrementally, once per segment.
# This keeps daily ledger backups small while retaining every replay input.
if [ -d "${DATED_DATA_DIR}/archive" ]; then
  EVIDENCE_BACKUP_DIR="${BACKUP_DIR}/carry-dated-evidence"
  if [ -L "${EVIDENCE_BACKUP_DIR}" ]; then
    echo "Refusing: encrypted evidence destination is a symlink" >&2
    exit 2
  fi
  mkdir -p "${EVIDENCE_BACKUP_DIR}"
  "${PYBIN}" "${APP_ROOT}/scripts/backup_carry_dated_evidence.py" \
    --source "${DATED_DATA_DIR}/archive" \
    --destination "${EVIDENCE_BACKUP_DIR}" \
    --min-free-bytes "${POST_BACKUP_FREE_BYTES}"
fi

# ── copie hors-site : branche `backups` du dépôt GitHub (clé de déploiement,
# ~btcquant/.ssh/backup_deploy). Best-effort : un échec réseau ne doit jamais
# faire échouer la sauvegarde locale. ────────────────────────────────────────
REPO="${RUNTIME_ROOT}/backups-repo"
if [ -d "${REPO}/.git" ] && [[ "${ARCHIVE}" == *.enc ]]; then
  (
    cd "${REPO}"
    if find . -type f \
      \( -name 'state-*.tar.gz' -o -name '*.db' -o -name '*.db-*' \
      -o -name '*.sqlite' -o -name '*.sqlite-*' \
      -o -name '*.sqlite3' -o -name '*.sqlite3-*' -o -name '.env' \) \
      -print -quit | grep -q .; then
      echo "Refusing: off-host repository contains an unauthorized plaintext artifact" >&2
      exit 4
    fi
    if git ls-files | grep -Ev '^(state-[0-9]{8}-[0-9]{4}\.tar\.gz\.enc(\.verification\.json)?|carry-dated-evidence/observations-[0-9]{8}-[0-9]+-[0-9]+\.jsonl\.gz(\.manifest\.json)?\.enc)$' | grep -q .; then
      echo "Refusing: off-host repository contains a non-whitelisted tracked file" >&2
      exit 5
    fi
    while IFS= read -r -d '' candidate; do
      if [[ "${candidate}" =~ ^\./state-[0-9]{8}-[0-9]{4}\.tar\.gz\.enc(\.verification\.json)?$ ]] ||
        [[ "${candidate}" =~ ^\./carry-dated-evidence/observations-[0-9]{8}-[0-9]+-[0-9]+\.jsonl\.gz(\.manifest\.json)?\.enc$ ]]; then
        continue
      fi
      echo "Refusing: off-host repository contains an unapproved path: ${candidate}" >&2
      exit 6
    done < <(find . -path './.git' -prune -o \( -type f -o -type l \) -print0)
    mkdir -p carry-dated-evidence
    cp -- "${ARCHIVE}" .
    cp -- "${VERIFICATION}" .
    evidence_git_paths=()
    if [ -d "${BACKUP_DIR}/carry-dated-evidence" ]; then
      while IFS= read -r -d '' evidence_file; do
        evidence_name="$(basename -- "${evidence_file}")"
        if [[ ! "${evidence_name}" =~ ^observations-[0-9]{8}-[0-9]+-[0-9]+\.jsonl\.gz(\.manifest\.json)?\.enc$ ]]; then
          echo "Refusing: unexpected encrypted evidence filename: ${evidence_name}" >&2
          exit 7
        fi
        cp -- "${evidence_file}" carry-dated-evidence/
        evidence_git_paths+=("carry-dated-evidence/${evidence_name}")
      done < <(find "${BACKUP_DIR}/carry-dated-evidence" -maxdepth 1 -type f -name '*.enc' -print0)
    fi
    find . -maxdepth 1 -name 'state-*.tar.gz.enc' -mtime +30 -delete
    find . -maxdepth 1 -name 'state-*.tar.gz.enc.verification.json' -mtime +30 -delete
    git add -A -- \
      ':(glob)state-*.tar.gz.enc' \
      ':(glob)state-*.tar.gz.enc.verification.json' \
      "${evidence_git_paths[@]}"
    git -c commit.gpgsign=false commit -q -m "backup ${STAMP}" || exit 0
    git push -q "${OFFHOST_REMOTE}" "HEAD:refs/heads/${OFFHOST_BRANCH}"
    echo "Off-host backup pushed."
  ) || echo "Off-host push failed; local encrypted backup remains available" >&2
fi
