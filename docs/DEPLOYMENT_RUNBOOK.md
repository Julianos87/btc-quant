# Runbook de déploiement VPS

Ce runbook couvre le **paper trading** puis la campagne P1 sur le **testnet
Hyperliquid**. Toute exécution mainnet reste verrouillée par la Safety Baseline.

## Architecture des releases

- `/opt/btcquant/releases/<FULL_GIT_SHA>` : code et virtualenv immuables ;
- `/opt/btcquant/current` : lien atomique vers la release active ;
- `/opt/btcquant/previous` : release de rollback ;
- `/opt/btcquant/state`, `backups`, `data` et `.env` : données partagées,
  jamais remplacées par un déploiement.
- `BTCQUANT_ROOT=/opt/btcquant` est posé **dans les units source**. Ne pas
  dépendre d'un drop-in hôte `btcquant-*.service.d` pour cette variable :
  `WorkingDirectory=/opt/btcquant/current` se résout vers le répertoire
  physique de la release, et `Path.cwd().resolve()` suivrait alors
  `current/state` (symlink) que la path-safety refuse.
- `create-release.sh` construit et fume les launchers **sous le ROOT
  final**. Ne pas rsync une release construite ailleurs : les shebangs
  venv resteraient collés au préfixe de staging.
- Sauvegarde : `APP_ROOT` = release physique (`pwd -P` du script) pour
  le venv et `backup_database.py` ; `RUNTIME_ROOT` = `BTCQUANT_ROOT`
  (obligatoire, `/opt/btcquant`) pour `state/`, `backups/` et
  `backups-repo/`. Une invocation ad-hoc sans `BTCQUANT_ROOT` est
  refusée : pas de repli par `release/state`.
- Rééquilibrage : le binaire vient de `current/venv`, mais
  `BTCQUANT_ROOT=/opt/btcquant`. Jamais `BTCQUANT_ROOT=.../current`.

Le script refuse une mise à jour sans `.env`, clé de chiffrement, release active
ou clone Git propre. Il refuse également une horloge non synchronisée, moins
d’un Gio libre, de mauvaises permissions sur `.env` ou une base SQLite
corrompue. Il sauvegarde SQLite avant la bascule.

Si des positions Trend paper sont **OPEN** au moment d'un déploiement v6, ne
pas démarrer la campagne formelle v6 tant que tous les slots ne sont pas
FLAT. Voir `docs/TREND_V6_OPEN_POSITION_CONTINUITY.md`.

## Qualification staging obligatoire

Utiliser une VM Ubuntu de même version que le VPS, sans clés d’exchange et avec
une copie anonymisée de `state/`.

```bash
sudo bash deploy/install.sh
systemctl is-active btcquant-dashboard btcquant-trend btcquant-carry
curl --fail http://127.0.0.1:8666/healthz
curl --fail http://127.0.0.1:8666/readyz
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

## Migration SQLite explicite

Un démarrage de service ne migre jamais une base ancienne. Le code v6 refuse une
base v4/v5 tant qu'une migration explicite n'a pas été effectuée. Le numéro
`app_schema_version` vient de `metadata.schema_version`; `PRAGMA schema_version`
reste seulement un cookie interne SQLite et ne sert jamais de gate applicatif.

Le chemin migration est distinct du code-only. Après acquisition du lock, la
release cible est construite et validée, puis **tous** les writers et timers sont
arrêtés et vérifiés : carry, trend, dashboard, watchdog, compact, backup,
rebalance, shadow et testnet. Le script refuse tout état actif ou inconnu :

```bash
sudo systemctl stop btcquant-carry btcquant-trend btcquant-dashboard \
  btcquant-watchdog btcquant-compact btcquant-backup \
  btcquant-rebalance btcquant-rebalance-pending btcquant-shadow \
  btcquant-hyperliquid-testnet btcquant-hyperliquid-watchdog \
  btcquant-watchdog.timer btcquant-hyperliquid-watchdog.timer \
  btcquant-compact.timer btcquant-backup.timer \
  btcquant-rebalance.timer btcquant-rebalance-pending.timer
sudo env \
  DEPLOY_REMOTE=origin \
  DEPLOY_BRANCH=main \
  BTCQUANT_CANONICAL_REPOSITORY=github.com/Julianos87/btc-quant.git \
  BTCQUANT_CANONICAL_REMOTE_ALIASES='github-backup=github.com' \
  bash /opt/btcquant/current/deploy/update.sh \
  --sha <FULL_GIT_SHA> --migration --engines
```

Le gate appelle SQLite pour consolider le WAL, crée ensuite le backup vérifié,
puis exécute `python -m btcquant.entrypoints.migrate --confirm-migration`.
La DB est revalidée avant le switch atomique. Après le switch, le démarrage du
premier service du writer set — y compris le dashboard, qui reste writer-capable
via son chemin readiness — franchit la frontière irréversible. Une panne avant
ce démarrage peut restaurer ce backup vérifié puis repasser à l'ancien code. Dès
que ce premier writer est démarré, même sans écriture métier observée, aucune
restauration automatique ni démarrage de l'ancien binaire n'est permis :
`MANUAL RECOVERY REQUIRED`.

Le chemin code-only (`update.sh --sha ...`) refuse toute DB sous le schéma cible
et ne touche pas à la DB; il peut donc faire un rollback de code automatique si
le health check échoue. Une DB v6 ne doit jamais être utilisée avec un ancien
binaire v5.

## Mise à jour

La mise à jour exige un SHA complet exactement égal à la branche canonique
configurée (`DEPLOY_REMOTE`/`DEPLOY_BRANCH`, sans nom de remote implicite) et une
URL correspondant à `BTCQUANT_CANONICAL_REPOSITORY`. Un clone dirty, un fichier
non suivi pertinent ou une résolution de dépendances implicite sont bloquants :

```bash
sudo env \
  DEPLOY_REMOTE=origin \
  DEPLOY_BRANCH=main \
  BTCQUANT_CANONICAL_REPOSITORY=github.com/Julianos87/btc-quant.git \
  BTCQUANT_CANONICAL_REMOTE_ALIASES='github-backup=github.com' \
  bash /opt/btcquant/current/deploy/update.sh \
  --sha <FULL_GIT_SHA>
```

Le redémarrage des moteurs est une décision distincte pendant une fenêtre de
maintenance :

```bash
sudo env \
  DEPLOY_REMOTE=origin \
  DEPLOY_BRANCH=main \
  BTCQUANT_CANONICAL_REPOSITORY=github.com/Julianos87/btc-quant.git \
  BTCQUANT_CANONICAL_REMOTE_ALIASES='github-backup=github.com' \
  bash /opt/btcquant/current/deploy/update.sh \
  --sha <FULL_GIT_SHA> --engines
```

La séquence code-only est : lock non bloquant, vérification Git canonique,
staging, `uv sync --frozen`, tests et manifeste, preflight de schéma, switch
atomique, redémarrage borné et health check. Toute erreur restaure `current` et
`previous`. La séquence migration est celle décrite ci-dessus et ne restaure la
DB automatiquement que tant que l'absence d'écriture cible est démontrée.

## Rollback

```bash
sudo bash /opt/btcquant/current/deploy/update.sh --rollback
```

Le rollback refuse une release sans manifeste ou un ancien code incompatible
avec le schéma courant. Une migration SQLite n'est jamais annulée implicitement
par un rollback de code : restaurer uniquement un backup vérifié et après
quiescence des writers, sinon `MANUAL RECOVERY REQUIRED`.

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

## Observation maker mainnet sans ordre

`btcquant-shadow.service` est activé automatiquement par `install.sh` et
`update.sh` lorsque l'entrypoint est présent dans la release. Il ne charge
aucun fichier de secrets et n'utilise que le carnet public Hyperliquid mainnet.
La base distincte `/opt/btcquant/state/execution-shadow.db` peut être inspectée
sans interrompre la collecte :

```bash
systemctl is-active btcquant-shadow
journalctl -u btcquant-shadow --since today
/opt/btcquant/current/venv/bin/btcquant-shadow \
  --database /opt/btcquant/state/execution-shadow.db status
curl --fail http://127.0.0.1:8666/readyz
```

Le collecteur absorbe les indisponibilités réseau temporaires avec un backoff
plafonné. Le watchdog ouvre un incident si le heartbeat du carnet dépasse cinq
minutes et le résout automatiquement à la reprise.

Le statut `SHADOW_PROXY_ONLY` est intentionnel : le market-through ne connaît
pas la position dans la file et ne constitue donc pas un fill réel. Conserver
au moins 30 jours de données avant d'interpréter la qualification proxy.

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
