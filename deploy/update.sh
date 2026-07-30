#!/usr/bin/env bash
# Déploiement atomique depuis le clone de service, avec rollback automatique.
set -euo pipefail

ROOT=/opt/btcquant
CLONE=/home/btcquant/btc-quant
CURRENT="${ROOT}/current"
PREVIOUS="${ROOT}/previous"
RESTART_ENGINES=false
TESTNET_APPROVAL="${ROOT}/state/HYPERLIQUID_TESTNET_APPROVED"

restart_selected_engines() {
  if [ -f "${TESTNET_APPROVAL}" ]; then
    systemctl restart btcquant-hyperliquid-testnet
  else
    systemctl restart btcquant-trend btcquant-carry
  fi
}

configure_shadow_service() {
  if [ -x "${CURRENT}/venv/bin/btcquant-shadow" ]; then
    systemctl enable btcquant-shadow.service
    systemctl restart btcquant-shadow.service
  else
    systemctl disable --now btcquant-shadow.service 2>/dev/null || true
  fi
}

configure_pending_rebalance_timer() {
  if [ -f "${CURRENT}/deploy/btcquant-rebalance-pending.timer" ]; then
    systemctl enable --now btcquant-rebalance-pending.timer
  else
    systemctl disable --now btcquant-rebalance-pending.timer 2>/dev/null || true
  fi
}

if [ "$(id -u)" -ne 0 ]; then
  echo "Ce script doit être exécuté par root." >&2
  exit 1
fi

case "${1:-}" in
  "") ;;
  --engines) RESTART_ENGINES=true ;;
  --rollback)
    if [ ! -L "${PREVIOUS}" ]; then
      echo "Aucune release précédente disponible." >&2
      exit 1
    fi
    TARGET="$(readlink -f "${PREVIOUS}")"
    OLD="$(readlink -f "${CURRENT}")"
    ln -sfn "${TARGET}" "${ROOT}/.current-next"
    mv -Tf "${ROOT}/.current-next" "${CURRENT}"
    ln -sfn "${OLD}" "${PREVIOUS}"
    cp "${CURRENT}/deploy/"btcquant-*.service "${CURRENT}/deploy/"btcquant-*.timer \
      /etc/systemd/system/
    install -o root -g root -m 0755 "${CURRENT}/deploy/rebalance-root.sh" \
      /usr/local/libexec/btcquant-rebalance
    systemctl daemon-reload
    configure_pending_rebalance_timer
    configure_shadow_service
    if ! systemctl restart btcquant-dashboard ||
      ! systemctl is-active --quiet btcquant-dashboard ||
      ! curl --fail --silent --show-error --max-time 10 \
        http://127.0.0.1:8666/healthz >/dev/null; then
      echo "Le rollback demandé est invalide ; restauration de ${OLD}." >&2
      ln -sfn "${OLD}" "${ROOT}/.current-next"
      mv -Tf "${ROOT}/.current-next" "${CURRENT}"
      cp "${CURRENT}/deploy/"btcquant-*.service \
        "${CURRENT}/deploy/"btcquant-*.timer /etc/systemd/system/
      systemctl daemon-reload
      configure_pending_rebalance_timer
      configure_shadow_service
      systemctl restart btcquant-dashboard
      exit 1
    fi
    echo "Rollback activé : ${TARGET}"
    exit 0
    ;;
  *)
    echo "Usage : update.sh [--engines|--rollback]" >&2
    exit 2
    ;;
esac

if [ ! -f "${ROOT}/.env" ]; then
  echo "Refus de déployer : ${ROOT}/.env est absent." >&2
  exit 1
fi
if [ ! -L "${CURRENT}" ]; then
  echo "Refus de déployer : ${CURRENT} n'est pas un lien de release." >&2
  exit 1
fi
if ! grep -q '^BACKUP_ENCRYPTION_KEY=.\+' "${ROOT}/.env"; then
  echo "Refus de déployer : BACKUP_ENCRYPTION_KEY est absente." >&2
  exit 1
fi
bash "${CURRENT}/deploy/preflight.sh"

sudo -u btcquant git -C "${CLONE}" pull --ff-only
if [ -n "$(sudo -u btcquant git -C "${CLONE}" status --porcelain --untracked-files=no)" ]; then
  echo "Refus de déployer un clone contenant des modifications suivies." >&2
  exit 1
fi
RELEASE_ID="$(sudo -u btcquant git -C "${CLONE}" rev-parse HEAD)"
OLD_TARGET="$(readlink -f "${CURRENT}")"

# Sauvegarde cohérente avant toute migration de schéma ou bascule.
BACKUP_ENCRYPTION_KEY="$(
  sed -n 's/^BACKUP_ENCRYPTION_KEY=//p' "${ROOT}/.env" | tail -n 1
)"
export BACKUP_ENCRYPTION_KEY
sudo -u btcquant env BACKUP_ENCRYPTION_KEY="${BACKUP_ENCRYPTION_KEY}" \
  "${CURRENT}/scripts/backup_state.sh"

TARGET="$(bash "${CLONE}/deploy/create-release.sh" "${CLONE}" "${RELEASE_ID}")"

rollback_on_error() {
  code=$?
  trap - ERR
  echo "Échec du déploiement ; retour à ${OLD_TARGET}." >&2
  ln -sfn "${OLD_TARGET}" "${ROOT}/.current-next"
  mv -Tf "${ROOT}/.current-next" "${CURRENT}"
  cp "${CURRENT}/deploy/"btcquant-*.service "${CURRENT}/deploy/"btcquant-*.timer \
    /etc/systemd/system/ || true
  systemctl daemon-reload || true
  configure_pending_rebalance_timer || true
  configure_shadow_service || true
  systemctl restart btcquant-dashboard || true
  if ${RESTART_ENGINES}; then
    restart_selected_engines || true
  fi
  exit "${code}"
}
trap rollback_on_error ERR

ln -sfn "${TARGET}" "${ROOT}/.current-next"
mv -Tf "${ROOT}/.current-next" "${CURRENT}"
ln -sfn "${OLD_TARGET}" "${PREVIOUS}"

cp "${CURRENT}/deploy/"btcquant-*.service "${CURRENT}/deploy/"btcquant-*.timer \
  /etc/systemd/system/
install -o root -g root -m 0755 "${CURRENT}/deploy/rebalance-root.sh" \
  /usr/local/libexec/btcquant-rebalance
systemctl daemon-reload
# Vérification après bascule : les ExecStart pointent sur la nouvelle release.
# Toute erreur déclenche le rollback avant le redémarrage des moteurs.
systemd-analyze verify "${CURRENT}/deploy/"*.service "${CURRENT}/deploy/"*.timer
systemctl enable --now btcquant-compact.timer
configure_pending_rebalance_timer
configure_shadow_service
systemctl restart btcquant-dashboard
if ${RESTART_ENGINES}; then
  restart_selected_engines
fi

systemctl is-active --quiet btcquant-dashboard
curl --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:8666/healthz >/dev/null
if ${RESTART_ENGINES}; then
  if [ -f "${TESTNET_APPROVAL}" ]; then
    systemctl is-active --quiet btcquant-hyperliquid-testnet
  else
    systemctl is-active --quiet btcquant-trend
    systemctl is-active --quiet btcquant-carry
  fi
fi

trap - ERR
echo "Release active : ${RELEASE_ID}"
echo "Rollback manuel : sudo bash ${CURRENT}/deploy/update.sh --rollback"
