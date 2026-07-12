#!/usr/bin/env bash
# Installation btcquant sur VPS Ubuntu/Debian.
# Usage : décompresser l'archive puis, depuis le dossier du projet :
#   sudo bash deploy/install.sh
set -euo pipefail

echo "── Dépendances système ──"
apt-get update -y
apt-get install -y python3 python3-venv rsync openssl

echo "── Utilisateur de service ──"
id -u btcquant &>/dev/null || useradd -r -m -s /usr/sbin/nologin btcquant

echo "── Copie vers /opt/btcquant ──"
mkdir -p /opt/btcquant
rsync -a --exclude venv --exclude __pycache__ --exclude .git ./ /opt/btcquant/

echo "── Environnement Python ──"
python3 -m venv /opt/btcquant/venv
/opt/btcquant/venv/bin/pip install --quiet --upgrade pip
/opt/btcquant/venv/bin/pip install --quiet -r /opt/btcquant/requirements.txt

echo "── Configuration dashboard (.env) ──"
if [ ! -f /opt/btcquant/.env ]; then
  PASS=$(openssl rand -base64 15)
  cat > /opt/btcquant/.env <<EOF
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8666
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=${PASS}
EOF
  chmod 600 /opt/btcquant/.env
fi

chown -R btcquant:btcquant /opt/btcquant

echo "── Services & timers systemd ──"
cp /opt/btcquant/deploy/btcquant-*.service /etc/systemd/system/
cp /opt/btcquant/deploy/btcquant-*.timer /etc/systemd/system/
chmod +x /opt/btcquant/scripts/backup_state.sh
systemctl daemon-reload
# services longue durée
systemctl enable --now btcquant-trend btcquant-carry btcquant-dashboard
# timers (digest quotidien, watchdog, sauvegarde, rééquilibrage mensuel)
systemctl enable --now btcquant-digest.timer btcquant-watchdog.timer \
                       btcquant-backup.timer btcquant-rebalance.timer

echo
echo "════════════════════════════════════════════════════════════"
echo " Installation terminée."
echo " Dashboard : http://$(hostname -I | awk '{print $1}'):8666"
grep DASHBOARD_USER /opt/btcquant/.env | sed 's/^/ Identifiant : /;s/DASHBOARD_USER=//'
grep DASHBOARD_PASSWORD /opt/btcquant/.env | sed 's/^/ Mot de passe : /;s/DASHBOARD_PASSWORD=//'
echo
echo " ⚠ Ouvrir le port si pare-feu actif :  ufw allow 8666/tcp"
echo " Statut   : systemctl status btcquant-trend btcquant-carry btcquant-dashboard"
echo " Journaux : journalctl -u btcquant-trend -f"
echo "════════════════════════════════════════════════════════════"
