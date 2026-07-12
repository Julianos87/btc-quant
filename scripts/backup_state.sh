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
tar -czf "${BACKUP_DIR}/state-${STAMP}.tar.gz" -C "${ROOT}" state
# purge des archives de plus de 30 jours
find "${BACKUP_DIR}" -name 'state-*.tar.gz' -mtime +30 -delete
echo "Sauvegarde : ${BACKUP_DIR}/state-${STAMP}.tar.gz"
