#!/usr/bin/env bash
# Installation btcquant sur VPS Ubuntu/Debian.
# Usage : décompresser l'archive puis, depuis le dossier du projet :
#   sudo bash deploy/install.sh
set -euo pipefail

echo "── Dépendances système ──"
apt-get update -y
apt-get install -y python3 python3-venv rsync openssl \
                    debian-keyring debian-archive-keyring apt-transport-https curl gnupg

echo "── Caddy (reverse proxy TLS automatique) ──"
# Certificat Let's Encrypt provisionné et renouvelé automatiquement pour le
# domaine du Caddyfile (voir deploy/Caddyfile) — aucune manip manuelle de
# certificat. Dépôt officiel Caddy (pas dans les dépôts Debian/Ubuntu de base).
if ! command -v caddy &>/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

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
# accès par lien secret : dashboard/app.py lit DASHBOARD_TOKEN (voir le
# commentaire « capability URL » en tête de fichier). Pas de mot de passe.
# DASHBOARD_HOST=127.0.0.1 : Flask n'écoute qu'en local, Caddy (ci-dessous)
# est seul exposé publiquement et fait la terminaison TLS.
if [ ! -f /opt/btcquant/.env ]; then
  TOKEN=$(openssl rand -hex 24)
  cat > /opt/btcquant/.env <<EOF
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8666
DASHBOARD_TOKEN=${TOKEN}
EOF
  chmod 600 /opt/btcquant/.env
fi

chown -R btcquant:btcquant /opt/btcquant

echo "── Caddyfile (reverse proxy) ──"
cp /opt/btcquant/deploy/Caddyfile /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl reload caddy

echo "── Services & timers systemd ──"
cp /opt/btcquant/deploy/btcquant-*.service /etc/systemd/system/
cp /opt/btcquant/deploy/btcquant-*.timer /etc/systemd/system/
chmod +x /opt/btcquant/scripts/backup_state.sh /opt/btcquant/scripts/rebalance_safe.sh
systemctl daemon-reload
# services longue durée
systemctl enable --now btcquant-trend btcquant-carry btcquant-dashboard
# timers (digest quotidien, bilan hebdo, watchdog, sauvegarde, rééquilibrage mensuel)
systemctl enable --now btcquant-digest.timer btcquant-weekly.timer \
                       btcquant-watchdog.timer btcquant-backup.timer \
                       btcquant-rebalance.timer

echo
echo "════════════════════════════════════════════════════════════"
echo " Installation terminée."
TOKEN=$(grep '^DASHBOARD_TOKEN=' /opt/btcquant/.env | cut -d= -f2)
echo " Dashboard — lien secret (à mettre en favori, il pose un cookie 1 an) :"
echo "   https://tandemalgo.duckdns.org/?k=${TOKEN}"
echo
echo " ⚠ Pare-feu : ouvrir 80/tcp et 443/tcp (Caddy). Le 8666 n'a plus besoin"
echo "   d'être exposé (Flask écoute en local uniquement) :"
echo "     ufw allow 80/tcp && ufw allow 443/tcp"
echo " Statut   : systemctl status caddy btcquant-trend btcquant-carry btcquant-dashboard"
echo " Journaux : journalctl -u btcquant-trend -f   |   journalctl -u caddy -f"
echo "════════════════════════════════════════════════════════════"
