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

# ── copie hors-site : branche `backups` du dépôt GitHub (clé de déploiement,
# ~btcquant/.ssh/backup_deploy). Best-effort : un échec réseau ne doit jamais
# faire échouer la sauvegarde locale. ────────────────────────────────────────
REPO="${ROOT}/backups-repo"
if [ -d "${REPO}/.git" ]; then
  (
    cd "${REPO}"
    cp "${BACKUP_DIR}/state-${STAMP}.tar.gz" .
    find . -maxdepth 1 -name 'state-*.tar.gz' -mtime +30 -delete
    git add -A
    git -c commit.gpgsign=false commit -q -m "backup ${STAMP}" || exit 0
    git push -q origin backups
    echo "Copie hors-site poussée (branche backups)."
  ) || echo "⚠ push hors-site échoué — la sauvegarde locale reste valide"
fi
