# Suivi de remédiation de l’audit logiciel

État vérifié le 26 juillet 2026 sur le worktree local.

Légende :

- [x] terminé dans le code local et couvert par des vérifications ;
- [~] partiellement traité : amélioration réelle, mais l’exigence initiale
  n’est pas entièrement satisfaite ;
- [ ] non traité ou insuffisamment traité ;
- **Non déployé** : le correctif existe localement mais n’est pas encore actif
  sur le VPS.

Cette checklist ne transforme pas une correction locale en validation pour
argent réel. Le testnet et le live restent verrouillés.

## 1. Constats critiques

### C1 — Élévation de privilèges systemd

- [x] Code et virtualenv prévus en `root:root`.
- [x] Seuls `state/` et `backups/` sont prévus inscriptibles par `btcquant`.
- [x] Watchdog exécuté avec l’utilisateur `btcquant`, plus avec root.
- [x] Rééquilibrage root déplacé dans un helper minimal installé hors de
  l’arbre applicatif modifiable.
- [x] `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`
  et `ReadWritePaths` ajoutés aux services principaux.
- [x] `CapabilityBoundingSet=`, `RestrictSUIDSGID` et `LockPersonality` sont
  définis sur tous les services non privilégiés.
- [ ] Correctifs non encore déployés et revérifiés sur le VPS.

**Statut C1 : [~] partiel tant que le déploiement et la vérification VPS ne
sont pas faits.**

### C2 — Kill switch non immédiat

- [x] Risque recalculé à chaque tick avant le traitement des stratégies.
- [x] Liquidation tentée immédiatement au tick courant.
- [x] Nouvelles entrées interdites lorsque le moteur est `HALTED`.
- [x] Moteur maintenu en veille après liquidation pour éviter une boucle de
  redémarrage systemd.
- [x] Test de liquidation immédiate ajouté.

**Statut C2 : [x] terminé dans le code local.**

### C3 — Réconciliation fail-open

- [x] Erreur réseau de réconciliation devenue bloquante.
- [x] Désaccord local/exchange devenu bloquant.
- [x] Retour de réconciliation vérifié par les runners.
- [x] Échec au démarrage si la réconciliation n’est pas certaine.
- [x] Carry bloqué si sa réconciliation échoue.
- [x] Incidents `PENDING`/`UNBALANCED` empêchant une reprise normale.
- [x] Tests fail-closed et reprise ajoutés.
- [x] Réconciliation via le port explicite `Broker.net_position`, sans accès à
  l’objet interne CCXT.

**Statut C3 : [x] risque critique traité ; dette d’interface restante.**

### C4 — Cycle de vie des stops

- [x] Une sortie market est envoyée avant l’annulation du stop existant.
- [x] Une sortie échouée conserve la position et son stop.
- [x] Une sortie partielle protège le reliquat avant d’annuler l’ancien stop.
- [x] Un ratchet pose le nouveau stop avant d’annuler l’ancien.
- [x] Gaps de stop simulés de manière conservatrice.
- [x] Fills partiels simulés et testés pour paper/backtest.
- [x] Un stop exchange partiellement exécuté déclenche un incident persistant
  et bloque le moteur, sans matérialiser une fausse sortie totale.
- [x] Un stop annulé/rejeté/expiré sur l’exchange est automatiquement recréé au
  tick suivant.
- [x] La stratégie spot Binance utilise `STOP_LOSS` market au déclenchement et
  refuse fail-closed un marché qui ne déclare pas ce contrat.
- [ ] Les comportements spécifiques de chaque exchange ne sont pas validés
  contractuellement sur sandbox.

**Statut C4 : [~] nettement amélioré, mais pas prêt pour le réel.**

### C5 — Carry réel

- [x] Brokers externes centralement verrouillés.
- [x] Fermeture incomplète conservée en `UNBALANCED` au lieu de se déclarer
  faussement `FLAT`.
- [x] Position et quantité locales conservées en cas d’échec.
- [x] État carry persisté transactionnellement.
- [x] Test de fermeture incomplète ajouté.
- [x] Saga persistante `OPENING → HEDGING → OPEN → CLOSING`, avec états
  `PARTIAL`, `REJECTED` et `UNBALANCED`.
- [x] Attente des statuts terminaux et rapprochement des quantités réellement
  remplies sur les deux jambes.
- [x] Quantité commune calculée et revalidée avec les précisions spot et perp.
- [~] Basis spot/perp, taux d’emprunt variable, haircuts, maintenance margin,
  frais et seuil de liquidation sont modélisés dans le backtest carry ; les
  transferts entre comptes et règles portfolio margin propres au compte réel
  doivent encore être validés sur sandbox.
- [~] Le modèle accepte les séries observées spot/perp et d’emprunt et signale
  explicitement lorsqu’elles manquent ; leur acquisition authentifiée et leur
  rapprochement comptable restent à qualifier.
- [ ] Aucune validation prolongée sur testnet.

**Statut C5 : [~] sécurisé par désactivation, mais carry réel non reconstruit.**

### C6 — Transaction ordre/état local

- [x] SQLite WAL devenu source de vérité.
- [x] Intention d’ordre écrite avant l’effet externe.
- [x] Identifiant métier stable transmis comme `clientOrderId`.
- [x] Identifiant d’ordre exchange conservé.
- [x] Résultat d’ordre, checkpoint, position et trade validés dans une seule
  transaction locale.
- [x] Timeout externe ambigu maintenu `PENDING`.
- [x] Recherche de l’ordre existant au redémarrage.
- [x] Effet externe possible classé `UNBALANCED`, jamais auto-appliqué.
- [x] Migration JSON/CSV idempotente.
- [x] Tests de crash avant envoi, après envoi, avant checkpoint et après
  transaction atomique.

**Statut C6 : [x] première version professionnelle mono-nœud terminée.**

### C7 — Parité backtest/paper

- [x] Noyau de décision déterministe partagé.
- [x] Événements typés d’entrée, sortie, funding et resserrement de stop.
- [x] Simulateur d’exécution commun backtest/paper.
- [x] Frais, slippage, rejets, participation volume, impact, latence et fills
  partiels centralisés.
- [x] Tests de parité du noyau de décision.
- [~] Le backtest et le runner gardent des calendriers d’exécution différents
  par leurs adaptateurs.
- [~] Les venues de données et d’exécution ne sont pas encore identiques dans
  tous les profils.
- [~] Tous les checkpoints runtime sont rejouables depuis les événements avec
  vérification SHA-256 ; le calendrier complet backtest/paper/live n’est pas
  encore un flux événementiel unique.
- [ ] Pas de provenance/hash systématique pour toutes les données et sorties.

**Statut C7 : [~] fondation commune créée, parité totale non atteinte.**

### C8 — Funding temporel

- [x] Colonnes distinctes pour paiement de funding et filtre de signal.
- [x] Test empêchant le look-ahead du filtre funding.
- [x] Funding intégré au noyau de décision commun.
- [x] Financement du levier carry ajouté au modèle synthétique.
- [x] Funding modélisé comme un événement horodaté indépendant de la barre.
- [x] Le live récupère les paiements natifs depuis le dernier checkpoint et les
  applique exactement une fois.
- [x] Le backtest distingue le paiement à l’ouverture de ceux postérieurs aux
  ordres, afin de ne jamais facturer rétroactivement une nouvelle position.

**Statut C8 : [x] correction temporelle terminée dans le code local.**

## 2. Architecture générale

- [x] Layout `src/` conservé.
- [x] Nouveau domaine pur `btcquant.domain` pour décision et exécution simulée.
- [x] SQLite centralise état, positions, ordres, événements, trades, equity,
  flux, incidents et qualification.
- [x] Journal et métriques opérationnelles séparés des fichiers JSON/CSV.
- [~] Funding, risque, passerelle d’ordres, comptabilité des positions, cycle
  des stops et dépendances runtime extraits ; orchestration et checkpoints
  restent dans `LiveRunner`.
- [x] `BacktestEngine.run` a été ramené à une orchestration de phases ; fills
  d’entrée/sortie, intrabar et décision de clôture sont isolés et testés.
- [~] Authentification, repository de reporting SQLite/CSV partagé et calculs
  financiers extraits de `dashboard/app.py` ; les routes restent encore
  monolithiques.
- [x] `dashboard/index.html` ne contient plus les ~2 100 lignes CSS/JavaScript :
  les assets sont séparés, servis statiquement et validés par la CI.
- [~] Ports, adaptateurs et entrypoints existent pour le runtime critique ; il
  ne s’agit pas encore d’une Clean Architecture complète sur chaque module.
- [x] Runtime, recherche et dashboard sont séparés par packages et groupes de
  dépendances `core/exchange/dashboard/research`.
- [~] Les exécutables déployés n’utilisent plus `sys.path.insert` ; quelques
  outils ponctuels de recherche exécutables depuis le checkout le conservent.
- [x] Six vrais entrypoints CLI installés pilotent trend, carry, readiness,
  watchdog, digest et rééquilibrage.

## 3. Qualité du code et dette

- [~] Noyau métier, funding, risque, machine des stops et phases carry/backtest
  extraits ; l’orchestration principale trend reste substantielle.
- [~] Authentification, accès aux données et analytics dashboard extraits ;
  routes et frontend restent des god files.
- [~] Dictionnaires non typés : événements et exécution typés, config et états
  encore largement sous forme de dictionnaires.
- [x] Duplication du noyau de décision backtest/paper fortement réduite.
- [x] Duplication du simulateur de fills fortement réduite.
- [~] Accès SQLite/legacy et formules d'equity, d'apports et de drawdown
  partagés entre dashboard et digest ; assemblage des rapports encore dupliqué.
- [x] Le reporting échoue explicitement si SQLite est illisible, sans afficher
  silencieusement un ancien JSON/CSV comme état courant.
- [x] Politique partagée de retry exponentiel et circuit breaker pour les
  lectures réseau ; créations d’ordres explicitement non rejouées.
- [~] Constantes dupliquées réduites, mais pas éliminées.
- [~] Hiérarchie d’exceptions d’exécution introduite ; les erreurs readiness et
  stockage restent à spécialiser.
- [~] Plusieurs commentaires contradictoires corrigés ; audit complet des
  commentaires non terminé.
- [x] `ruff check` passe.
- [x] `ruff format --check` est imposé en CI.
- [x] Mypy passe sur les 54 modules source.
- [~] Frontend découpé en assets et valeurs externes échappées ; le JavaScript
  conserve encore un état global et n’est pas migré vers des modules ES.

## 4. Nettoyage

- [x] Caches et artefacts exclus de Git.
- [x] `state/`, données et backups préservés.
- [x] `Position.notional()` inutilisé supprimé.
- [x] `CarryBroker.funding_rate()` inutilisé supprimé.
- [x] `CarryBroker.free_usdt()` inutilisé supprimé.
- [x] Stockage mort de `StrategySlot.capital_fraction` supprimé.
- [x] `sma()` et `rsi()` sans consommateur ont été supprimés plutôt que
  conservés artificiellement.
- [x] `config_3x.yaml` est archivé sous `research/configs/`.
- [x] `TrendSwing`, `IntradayBreakout` et walk-forward sont isolés sous
  `btcquant.research`, hors registre runtime et hors environnement production.
- [x] `SECURITY_AUDIT.md` est marqué comme archive historique limitée.
- [x] `dashboard/backtest_reference.json` manuel et périmé a été supprimé ;
  l’API lit la baseline générée et traçable.
- [x] `dashboard/yearly_reference.json` embarque commit, hash de l’arbre source,
  hash de config et hash des données ; la CI refuse toute divergence.
- [x] Workflow CI réactivé sur push et pull request.
- [x] Badge Python aligné sur Python ≥3.11.
- [x] `.claude/launch.json` obsolète a été supprimé.
- [x] Dépendances segmentées en groupes core/exchange/dashboard/research/dev ;
  `requirements.txt` de production est généré depuis le lock.

## 5. Architecture Python

- [x] Aucun cycle d’import introduit.
- [x] `Broker.stop_status()` fait maintenant partie de l’interface.
- [x] Recherche d’ordre externe représentée dans l’interface broker.
- [x] Dataclasses/enums utilisés pour décisions, ordres et simulateur.
- [x] Schéma SQLite versionné avec migrations jusqu’à v3.
- [x] Sections requises, coûts, timeframe, marché, fractions de capital et
  paramètres de risque validés au chargement.
- [ ] Pas de modèle Pydantic/msgspec complet.
- [x] `RiskConfig` immuable refuse valeurs non finies, négatives et ratios hors
  bornes.
- [x] `Position.direction` utilise l’enum signé `Direction`, normalisé dès la
  construction et refusant toute valeur autre que long/short.
- [ ] Beaucoup de chaînes d’état restent non centralisées.
- [~] Les précisions, prix, quantités et notionnels sont normalisés et comparés
  en `Decimal` aux frontières exchange ; NumPy/pandas conserve des `float`
  dans le domaine quantitatif.
- [x] Broker, Venue, Notifier et Clock injectés via des ports explicites.
- [ ] Singletons globaux du dashboard toujours présents.

## 6. Performance

- [x] Equity et métriques opérationnelles peuvent être lues depuis SQLite.
- [x] Compaction SQLite de l’equity disponible.
- [x] Cache réseau du dashboard et sérialisation des appels exchange présents.
- [x] Simulateur évite de recalculer certaines règles de fill.
- [ ] Cache OHLCV toujours réécrit intégralement.
- [ ] Features EMA/ATR/ADX recalculées séparément pour chaque stratégie.
- [ ] Boucle principale du backtest toujours fondée sur des itérations Python.
- [~] Dashboard ne dépend plus uniquement des CSV, mais plusieurs métriques
  restent recalculées.
- [ ] Pas de stockage Parquet partitionné.
- [ ] Expériences de paramètres non parallélisées proprement.

## 7. Robustesse

- [x] Validation OHLC ajoutée dans le simulateur d’exécution.
- [x] Transactions SQLite et contraintes d’intégrité.
- [x] Reprise déterministe après crash.
- [x] Timeouts externes ambigus traités fail-closed.
- [x] Incidents persistants, dédupliqués et résolus automatiquement.
- [x] Watchdog, health metrics, digest et cockpit opérationnel.
- [x] Sauvegarde SQLite cohérente via l’API backup.
- [x] Test de soak de 250 ordres.
- [x] Trous, doublons, ordre, fraîcheur, valeurs finies et invariants OHLCV
  contrôlés avant chaque décision.
- [x] Synchronisation NTP bloquante dans le preflight de l’hôte.
- [x] `SIGTERM`/`SIGINT` déclenchent un arrêt coopératif et un checkpoint final.
- [x] Retries de lecture bornés et circuit breaker réseau partagé, avec horloge
  et attente injectables pour les tests.
- [x] SLO, indicateurs de fraîcheur et budget d’erreur formalisés dans
  `docs/RELIABILITY_SLO.md`.
- [x] Les ordres ambigus ne sont jamais « consommés » : `PENDING` et
  `UNBALANCED` constituent une quarantaine transactionnelle durable, plus
  adaptée qu’une DLQ de messages inexistante dans cette architecture.
- [x] Le déchiffrement est testé à chaque sauvegarde par comparaison exacte ;
  extraction isolée, prévention du path traversal et `PRAGMA integrity_check`
  sont automatisés.
- [x] Backup hors-site chiffré ; une archive en clair reste locale et ne peut
  jamais être poussée.

## 8. Sécurité

- [x] Aucun secret codé en dur détecté.
- [x] Fichiers sensibles ignorés.
- [x] YAML chargé avec `safe_load`.
- [x] Pas d’injection évidente ou d’exécution dynamique dangereuse.
- [x] Comparaison constante du jeton dashboard.
- [x] En-têtes défensifs HTTP et TLS déjà présents côté production historique.
- [x] Actions GitHub épinglées par SHA.
- [x] Permissions CI explicites `contents: read`.
- [x] Installation CI verrouillée par `uv.lock`.
- [x] Fichier d’installation production épinglé avec hashes SHA-256 des
  artefacts autorisés.
- [x] `pip-audit` intégré à la CI et aucune vulnérabilité connue lors du dernier
  contrôle.
- [x] Jeton dashboard transmis par formulaire `POST`, jamais dans l’URL.
- [x] Manifest sans secret et `start_url` fixé à `/`.
- [x] Cookie signé, `HttpOnly`, `SameSite=Strict`, limité à 12 heures et
  `Secure` derrière TLS.
- [~] `script-src` refuse désormais tout script inline ; `style-src` conserve
  temporairement `unsafe-inline` à cause des styles dynamiques existants.
- [x] Les exceptions Telegram ne journalisent que leur classe ; test de
  non-régression avec une exception contenant volontairement le token.
- [x] Backups hors-site chiffrés et déchiffrement vérifié avant publication.
- [~] Releases immuables par commit, virtualenv propre, bascule atomique et
  rollback automatique ajoutés ; artefacts non encore signés.
- [x] SBOM de production CycloneDX 1.5 généré par `uv` et vérifié en CI.
- [x] Dependabot configuré pour `uv` et GitHub Actions.
- [x] `LICENSE`, `SECURITY.md`, `CHANGELOG`, `CONTRIBUTING` et `CODEOWNERS`
  ajoutés ; les changements risque/stratégie exigent une revue humaine.

## 9. Tests et qualité automatisée

- [x] Suite passée de 88 à 278 tests.
- [x] Tests du kill switch immédiat.
- [x] Tests de réconciliation fail-closed.
- [x] Tests de sorties nulles et partielles.
- [x] Tests de déséquilibre carry.
- [x] Tests de migration JSON/CSV et de schéma SQLite.
- [x] Tests de crash à toutes les frontières critiques locales.
- [x] Tests de timeout après ordre potentiellement exécuté.
- [x] Tests du simulateur : rejets, partials, latence, impact et gaps.
- [x] Tests d’incidents/watchdog.
- [x] Tests du protocole de qualification PASS/FAIL.
- [x] CI automatique sur push, pull request et Python 3.11/3.12.
- [x] Lint, format, typage et audit dépendances en CI.
- [x] Couverture de branches du runtime qualifié à 81,37 % lors du dernier
  relevé complet.
- [x] Seuil CI porté à 80 %.
- [x] Couverture de branches imposée.
- [x] Tests property-based Hypothesis sur les invariants de sizing.
- [~] Tests frontend de chargement des assets, CSP, échappement et syntaxe
  JavaScript ajoutés ; parcours navigateur local réel validé sans erreur
  console, mais pas encore automatisé dans la CI.
- [~] Syntaxe Bash testée localement et en CI, unités vérifiées par
  `systemd-analyze` avant activation ; test d’intégration systemd en VM absent.
- [ ] Pas de tests contractuels nocturnes sur sandbox exchange.

## 10. Observabilité et qualification

- [x] Prix de référence et slippage enregistrés par ordre.
- [x] Fill ratio, rejets, partials et slippage p95 calculés.
- [x] Détection des ordres `PENDING` trop anciens et `UNBALANCED`.
- [x] Incidents visibles dans l’API, le dashboard, le digest et la CLI.
- [x] Protocole paper → testnet versionné dans SQLite.
- [x] Dix-sept contrôles automatiques PASS/FAIL.
- [x] Testnet bloqué sans campagne `PASSED` récente.
- [x] Argent réel verrouillé séparément.
- [x] Campagne locale v1 démarrée.
- [ ] Campagne non démarrée sur le VPS de production.
- [ ] Observation de 90 jours non réalisée.
- [ ] Qualification testnet non obtenue.
- [ ] Aucun ordre testnet envoyé.

## 11. Plan de refactoring initial — synthèse

- [x] Bloquer techniquement tous les modes live.
- [~] Propriété et confinement systemd prêts, déploiement à faire.
- [x] Kill switch immédiat et réconciliation fail-closed.
- [~] Cycle de vie stops/fills partiels sécurisé ; contrats sandbox exchange
  encore absents.
- [x] Journal transactionnel des ordres et état.
- [~] Saga carry reconstruite ; modèle basis/emprunt/haircut/marge/liquidation
  ajouté, acquisition authentifiée et qualification exchange incomplètes.
- [x] Funding événementiel et timing.
- [~] Moteur de transitions partagé.
- [~] Config validée, enums et version d’état.
- [~] Tests de panne/reprise/contractuels : panne et reprise faites,
  contractuels exchange absents.
- [x] CI, format et typing réactivés.
- [x] Coverage gate de branches fixé à 80 %.
- [~] Déploiement durci, versionné et atomique avec rollback ; qualification
  staging réelle encore à faire.
- [~] Validation qualité/fraîcheur des données.
- [~] SQLite/repositories : SQLite fait, ports repositories incomplets.
- [~] Authentification et repository backend extraits ; découpage
  analytics/routes/frontend à poursuivre.
- [x] Runtime et research séparés dans le package et les dépendances.
- [x] Références générées avec provenance source/config/données et contrôle CI.
- [x] Méthodes mortes identifiées supprimées, ancien profil archivé et registre
  runtime débarrassé des stratégies expérimentales.
- [x] Documentation, runbooks, SLO, sécurité, contribution et changelog ajoutés.

## 12. Roadmap — état des phases

### Phase 0 — Mise en sécurité

- [x] Verrouillage paper/live.
- [x] Suppression fonctionnelle du carry live par verrou central.
- [~] Permissions systemd corrigées localement, non redéployées.
- [x] CI automatique, Ruff, Mypy, couverture minimale et audit dépendances.
- [x] Interdiction du capital réel documentée.

### Phase 1 — Fondations métier

- [~] Modèles typés immuables.
- [~] Configuration validée.
- [~] Interfaces par capacités pour broker, données de marché et notification.
- [x] Notifier et Clock injectables ; `SystemClock` assemblée dans l’entrypoint.
- [x] SQLite WAL avec migrations.
- [x] Journal d’événements et identifiants déterministes.
- [~] Invariants portefeuille/risque.

### Phase 2 — Moteur unifié

- [~] Machine de décision commune.
- [~] Événements métier typés.
- [~] Checkpoints runtime entièrement rejouables et hashés ; replay d’un
  calendrier unique backtest/paper/live encore incomplet.
- [x] Tests golden de non-régression du backtest et parité du noyau.
- [x] Baseline et référence annuelle portent leurs hashes source/config/données
  et sont contrôlées en CI.

### Phase 3 — Exécution sûre

- [~] Adaptateur testnet présent mais non qualifié.
- [~] Statuts et fills partiels.
- [x] Réconciliation bloquante.
- [x] Recovery après timeout/crash.
- [~] Stops protecteurs surveillés et recréés ; validation sandbox absente.
- [x] Kill switch immédiat.
- [x] Parcours testnet automatisé avec nettoyage garanti et double porte
  qualification PASS + confirmation explicite de session.

### Phase 4 — Carry

- [ ] Validation par spécialiste exchange/portfolio margin.
- [~] Coût d’emprunt synthétique ajouté.
- [~] Basis, emprunt variable, haircuts, maintenance margin et liquidation
  modélisés ; paramètres et flux réels du compte restent à qualifier.
- [x] État `UNBALANCED`, compensation et conservation des reliquats.
- [x] Saga deux jambes avec quantités réellement remplies.
- [ ] Testnet prolongé.

### Phase 5 — Exploitation

- [x] Releases immuables par commit et rollback automatique.
- [x] Dashboard servi par Gunicorn, lié uniquement à `127.0.0.1`.
- [x] Métriques d’exécution, incidents persistants, watchdog et alertes
  dédupliquées structurés.
- [x] Diagnostics, runbook de déploiement et runbook d’incident documentés.
- [~] Sauvegardes cohérentes et chiffrées, restauration clean-room, schéma,
  moteurs et ordres ambigus vérifiés ; exercice système complet sur une
  machine vierge encore absent.
- [x] Gouvernance, permissions minimales et rotation des clés formalisées.
- [x] Revue humaine obligatoire des changements stratégie/risque documentée
  dans `CONTRIBUTING.md` et protégée par `CODEOWNERS`.

## Verdict actualisé

Les risques **C2, C3, C6 et C8** sont traités dans le code local. **C4** est
sécurisé mais attend encore les contrats sandbox propres aux exchanges. **C5**
dispose maintenant d’une saga fiable, mais son modèle financier et sa
qualification testnet restent insuffisants pour le réel. **C1** reste ouvert
jusqu’au déploiement des unités durcies ; **C7** reste une parité partielle.

Le dépôt est devenu une base paper nettement plus robuste et observable. Il
n’est toujours pas validé pour gérer du capital réel important. Les principaux
chantiers restants sont :

1. exécuter le parcours stops et saga carry sur les sandboxes de chaque exchange ;
2. brancher les séries authentifiées de basis/emprunt et les règles exactes du
   compte portfolio margin ;
3. terminer les modèles/configurations typés et réduire l’état global frontend ;
4. automatiser le parcours navigateur et tester l’installation/restauration sur
   une machine vierge ;
5. déployer les durcissements puis achever la campagne paper et la
   qualification testnet.
