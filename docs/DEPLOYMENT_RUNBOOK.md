# Runbook de déploiement VPS

Ce runbook couvre le **paper trading** puis la campagne P1 sur le **testnet
Hyperliquid**. Toute exécution mainnet reste verrouillée par la Safety Baseline.

## Architecture des releases

- `/opt/btcquant/releases/<commit>` : code et virtualenv immuables ;
- `/opt/btcquant/current` : lien atomique vers la release active ;
- `/opt/btcquant/previous` : release de rollback ;
- `/opt/btcquant/state`, `backups`, `data` et `.env` : données partagées,
  jamais remplacées par un déploiement.

Le script refuse une mise à jour sans `.env`, clé de chiffrement, release active
ou clone Git propre. Il refuse également une horloge non synchronisée, moins
d’un Gio libre, de mauvaises permissions sur `.env` ou une base SQLite
corrompue. Il sauvegarde SQLite avant la bascule.

## Qualification staging obligatoire

Utiliser une VM Ubuntu de même version que le VPS, sans clés d’exchange et avec
une copie anonymisée de `state/`.

```bash
sudo bash deploy/install.sh
systemctl is-active btcquant-dashboard btcquant-trend btcquant-carry
curl --fail http://127.0.0.1:8666/healthz
sudo -u btcquant /opt/btcquant/current/venv/bin/python \
  /opt/btcquant/current/scripts/inspect_state.py
```

Vérifier ensuite :

1. démarrage et arrêt propres des trois services ;
2. absence d’ordre externe et mode paper dans les logs ;
3. création d’un checkpoint SQLite ;
4. redémarrage après `kill -9` d’un moteur ;
5. récupération exacte des positions, ordres et incidents ;
6. accès dashboard uniquement via `/login` et HTTPS ;
7. exécution manuelle du backup et du watchdog ;
8. rollback vers la release précédente.

Un arrêt systemd doit produire dans chaque journal le message confirmant le
checkpoint final. Les runners refusent désormais toute décision si les bougies
sont périmées, désordonnées, dupliquées, trouées ou financièrement impossibles.

## Exercice de restauration

La clé doit provenir du gestionnaire de secrets ou du `.env` root-only. Ne
jamais l’afficher dans le terminal ou les logs.

```bash
export BACKUP_ENCRYPTION_KEY="$(
  sed -n 's/^BACKUP_ENCRYPTION_KEY=//p' /opt/btcquant/.env | tail -n 1
)"
RESTORE_DIR="$(mktemp -d /tmp/btcquant-restore.XXXXXX)"
/opt/btcquant/current/venv/bin/python \
  /opt/btcquant/current/scripts/verify_backup.py \
  /opt/btcquant/backups/state-YYYYMMDD-HHMM.tar.gz.enc \
  --extract-to "${RESTORE_DIR}"
```

Le résultat doit annoncer `integrity: ok`. Comparer ensuite les nombres
d’ordres, positions, trades, événements et incidents avec
`scripts/inspect_state.py`. Supprimer uniquement le répertoire temporaire
explicitement créé après validation.

## Mise à jour

La mise à jour standard ne redémarre que le dashboard :

```bash
sudo bash /opt/btcquant/current/deploy/update.sh
```

Le redémarrage des moteurs doit être une décision explicite pendant une fenêtre
de maintenance :

```bash
sudo bash /opt/btcquant/current/deploy/update.sh --engines
```

La séquence est : pull fast-forward, refus d’un clone modifié, sauvegarde,
construction isolée, compilation/import, validation systemd, bascule atomique,
redémarrage, sonde HTTP. Toute erreur après la bascule restaure automatiquement
la release précédente.

## Rollback

```bash
sudo bash /opt/btcquant/current/deploy/update.sh --rollback
```

Le rollback manuel rebascule le code et redémarre le dashboard. Si les moteurs
avaient été redémarrés pendant la release défectueuse, les arrêter puis vérifier
l’état SQLite et les ordres avant de les relancer. Une migration SQLite est
additive, mais un rollback de code ne constitue jamais à lui seul un rollback
de données.

## Go/no-go production paper

Le déploiement est refusé si l’un de ces points échoue :

- suite locale et CI entièrement verte ;
- archive chiffrée restaurée sur une copie ;
- `systemd-analyze verify` sans erreur ;
- smoke tests staging réussis ;
- procédure de rollback exécutée réellement ;
- copie hors-site confirmée ;
- opérateur disponible pour surveiller logs, incidents et positions.

Ce feu vert ne vaut pas qualification testnet ou argent réel.

## Portail P1 Hyperliquid testnet

Le testnet ne doit être activé qu'après le `PASS` final de la campagne paper.
Créer sur Hyperliquid testnet un API wallet dédié à ce seul processus. L'adresse
interrogée doit être celle du compte principal ou du sous-compte ; la clé privée
doit être celle de l'API wallet autorisé. Ne jamais installer la clé privée du
portefeuille principal sur le VPS.

Ajouter avec l'éditeur de secrets de l'hôte, sans afficher les valeurs :

```text
HYPERLIQUID_WALLET_ADDRESS=0x…
HYPERLIQUID_PRIVATE_KEY=0x…
TELEGRAM_BOT_TOKEN=…
TELEGRAM_CHAT_ID=…
```

Puis lancer le portail explicite :

```bash
sudo bash /opt/btcquant/current/deploy/start-hyperliquid-testnet.sh \
  --i-accept-hyperliquid-testnet-orders
```

Le portail :

1. vérifie les formats des secrets et la qualification paper v2 ;
2. démarre une campagne P1 immuable de 30 jours dans
   `/opt/btcquant/state/btcquant-testnet.db` ;
3. exécute le smoke test Hyperliquid et remet obligatoirement le compte à plat ;
4. journalise l'entrée et la sortie du smoke test ;
5. arrête le moteur paper, crée l'approbation locale et active le runner testnet ;
6. active un watchdog deux-minutes qui alerte sur moteur silencieux, ordre
   ambigu, position sans stop, transition de stop pendante, rejet ou slippage.

Contrôles quotidiens :

```bash
systemctl is-active btcquant-hyperliquid-testnet
journalctl -u btcquant-hyperliquid-testnet --since today
sudo -u btcquant /opt/btcquant/current/venv/bin/btcquant-readiness status \
  --database /opt/btcquant/state/btcquant-testnet.db
```

La campagne P1 ne passe qu'après 30 jours, 99,5 % de disponibilité, deux ordres
smoke terminaux, aucun incident ou ordre ambigu, au plus 5 % de rejets et un
slippage p95 inférieur ou égal à 20 bps. Finalisation :

```bash
sudo -u btcquant /opt/btcquant/current/venv/bin/btcquant-readiness finalize \
  --database /opt/btcquant/state/btcquant-testnet.db
```

Arrêt d'urgence :

```bash
sudo bash /opt/btcquant/current/deploy/stop-hyperliquid-testnet.sh
```

L'arrêt du service ne liquide pas arbitrairement une position : vérifier
immédiatement la position et le stop sur l'interface Hyperliquid testnet. Si le
stop est absent ou l'état divergent, fermer manuellement en `reduceOnly`, puis
conserver l'incident ouvert jusqu'à la réconciliation SQLite.
