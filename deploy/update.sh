#!/usr/bin/env bash
# Mise à jour du VPS depuis GitHub — SANS toucher au track record.
#
# Usage (sur le VPS) :
#   sudo bash /opt/btcquant/deploy/update.sh             # dashboard seul
#   sudo bash /opt/btcquant/deploy/update.sh --engines   # + moteurs trend/carry
#
# Depuis le poste local (alias « vps » dans ~/.ssh/config) :
#   ssh vps "sudo bash /opt/btcquant/deploy/update.sh"
#
# Mécanique : le clone git vit dans /home/btcquant/btc-quant (clé de
# déploiement GitHub de l'utilisateur de service). On pull, puis on copie
# vers /opt/btcquant en EXCLUANT state/ et backups/ (l'équity paper accumulée
# — l'écraser ruinerait les critères go/no-go), .env (jeton dashboard,
# Telegram) et backups-repo/ (dépôt de la sauvegarde hors-site).
#
# Les moteurs ne sont redémarrés que sur demande (--engines) : leur état est
# persisté et le redémarrage est sûr, mais on ne les touche pas si seule la
# couche dashboard/scripts a changé.
set -euo pipefail
CLONE=/home/btcquant/btc-quant

sudo -u btcquant git -C "${CLONE}" pull --ff-only

rsync -a --exclude .git --exclude venv --exclude __pycache__ \
      --exclude state --exclude backups --exclude backups-repo \
      --exclude .env --exclude data "${CLONE}/" /opt/btcquant/
chown -R btcquant:btcquant /opt/btcquant
# rsync -a préserve les modes du clone source ; si son umask est permissif,
# ça republie /opt/btcquant en lecture pour tous les comptes du VPS à chaque
# déploiement. On referme systématiquement l'accès "other" ici.
chmod -R o-rwx /opt/btcquant
chmod +x /opt/btcquant/scripts/backup_state.sh /opt/btcquant/scripts/rebalance_safe.sh

cp /opt/btcquant/deploy/btcquant-*.service /opt/btcquant/deploy/btcquant-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart btcquant-dashboard

if [ "${1:-}" = "--engines" ]; then
  systemctl restart btcquant-trend btcquant-carry
fi

echo "Mise à jour appliquée : $(sudo -u btcquant git -C "${CLONE}" log --oneline -1)"
systemctl is-active btcquant-trend btcquant-carry btcquant-dashboard >/dev/null \
  && echo "Services : tous actifs." \
  || { echo "⚠ Un service est inactif :"; systemctl --no-pager status btcquant-trend btcquant-carry btcquant-dashboard | grep -E "●|Active:"; }
