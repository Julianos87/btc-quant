# Audit logiciel complet — TANDEM / btc-quant

**Date** : 26 juillet 2026
**Révision auditée** : `9d58fcf8fadf72fa89874d914365cee4edca9b23`
**Branche** : `main`, un commit devant `origin/main` au moment de l'audit
**Positionnement évalué** : logiciel appelé à gérer plusieurs millions d'euros
**Conclusion de portée** : base paper sérieuse ; système non qualifiable en argent réel

## 1. Résumé exécutif

Le dépôt a beaucoup progressé depuis son état de prototype. La séparation
`domain` / `execution` / `entrypoints` / `reporting`, la persistance SQLite
transactionnelle, les gardes empêchant le live, les tests de crash et la chaîne
de dépendances figée constituent un socle réel. Ce n'est plus un assemblage IA
jetable.

Ce n'est néanmoins pas encore un logiciel de trading professionnel. Le défaut
principal n'est plus l'absence de garde-fous élémentaires ; c'est l'écart entre
ce que les mécanismes de qualification prétendent prouver et ce qu'ils mesurent
réellement :

1. la campagne paper exige au moins cinq ordres carry, mais le chemin paper du
   carry ne journalise aucun ordre ; le passage normal vers le testnet est donc
   impossible ;
2. la « couverture » d'une journée est validée par un seul point d'equity et le
   drawdown de qualification n'utilise qu'un point quotidien ; plusieurs heures
   d'indisponibilité ou un creux intrajournalier peuvent être invisibles ;
3. les seuils de fraîcheur de la qualification (6 h et 3 h) contredisent les SLO
   d'exploitation (10 min et 20 min) ;
4. les adaptateurs live et la saga carry ne sont pas au niveau requis : les
   stops de remplacement ne sont ni journalisés ni idempotents et le carry live
   ne modélise pas réellement le compte sur marge, les frais, le basis ni
   l'equity distante ;
5. le contrôle CI censé vérifier `requirements.txt` échoue systématiquement à
   cause du chemin de sortie inscrit dans l'en-tête généré par `uv`.

Le verrouillage de l'argent réel est donc la bonne décision. Il ne faut pas le
retirer à l'issue de la seule campagne actuelle.

### Verdict

- **Paper trading personnel, sous surveillance** : acceptable après correction
  des priorités critiques et hautes.
- **Testnet comme expérimentation manuelle** : possible avec procédures
  renforcées, mais pas comme promotion automatique fiable.
- **Argent réel, a fortiori plusieurs millions d'euros** : refus formel.
- **Architecture à jeter entièrement** : non.
- **Sous-systèmes à reconstruire avant le réel** : qualification, orchestration
  d'exécution live, gestion des ordres protecteurs et carry live.

## 2. Méthode et preuves

### Périmètre revu

- 124 fichiers Python/JS/CSS/HTML formatés par Ruff ;
- package `src/btcquant`, dashboard, scripts, configurations, CI, systemd,
  déploiement, sauvegardes, documentation et tests ;
- historique récent et différence entre `origin/main` et la révision auditée ;
- audits antérieurs, utilisés comme historique et non comme preuve.

### Contrôles exécutés

| Contrôle | Résultat |
|---|---|
| Pytest | **278/278 réussis** |
| Couverture package | **81 % branches**, 3 480 instructions |
| Ruff | réussi |
| Ruff format | 124 fichiers conformes |
| MyPy | réussi sur 55 modules de `src` |
| `pip check` | aucune dépendance cassée |
| `pip-audit` via OSV | aucune vulnérabilité connue |
| SBOM CycloneDX | conforme au lock |
| Provenance des références | conforme |
| Syntaxe JS | réussie |
| Recherche de secrets | aucun secret détecté |
| Export de production | contenu conforme, mais contrôle CI non reproductible à cause de l'en-tête |
| Syntaxe Bash locale | non exécutable dans cet environnement Windows/WSL ; la CI ne fait qu'un `bash -n` |

### Limites de l'audit

- aucune connexion au VPS de production ;
- aucun ordre envoyé sur un exchange ;
- aucune campagne de charge longue ;
- aucune restauration sur VM Linux vierge ;
- aucun test navigateur automatisé ;
- aucune preuve que les règles GitHub de protection de branche sont activées.

Les conclusions sur le testnet/live sont donc une revue du code et des contrats,
pas une certification d'intégration exchange.

## 3. Constats prioritaires

### C1 — La qualification paper → testnet est bloquée par conception

**Sévérité** : critique pour la promotion ; aucun impact d'ordre réel tant que le
verrou reste actif.

`ReadinessPolicy` exige cinq ordres terminaux par moteur
(`execution/readiness.py:27`, `:199-209`). Le chemin normal de `CarryRunner`
paper applique seulement le coût de bascule puis écrit l'état et l'equity
(`execution/carry_runner.py:227-240`, `:301-313`) ; il n'appelle
`begin_order...` que lorsque `live_broker` est présent. Or l'entrypoint carry
interdit précisément `--live` (`entrypoints/carry.py:51-55`).

Il n'existe donc aucun chemin utilisateur normal permettant de produire les
cinq ordres carry requis avant de débloquer le testnet. Le test de readiness
masque cette contradiction en insérant lui-même un ordre carry synthétique
(`tests/test_readiness.py:51-65`).

**Correction** :

- journaliser des intentions et résultats d'ordres simulés pour chaque bascule
  du carry paper, avec les mêmes statuts et références que le futur live ;
- créer un test de campagne complet utilisant uniquement les entrypoints
  publics, sans insertion SQL/test directe ;
- refuser toute mention « qualification PASS » tant que ce test end-to-end
  n'existe pas.

### C2 — La readiness ne mesure pas l'uptime ni le drawdown annoncés

**Sévérité** : critique avant toute promotion automatique.

La couverture quotidienne est un ensemble de dates possédant au moins un point
d'equity pour chaque moteur (`readiness.py:340-350`). Un moteur vivant une
minute par jour peut donc afficher 100 % de « présence ». Cela ne prouve ni
99,5 % d'uptime ni les SLO de fraîcheur.

Le drawdown ne retient que le dernier échantillon de chaque jour
(`readiness.py:380-388`) puis additionne deux moteurs potentiellement observés à
des instants différents (`:403-408`). Un drawdown intrajournalier profond peut
disparaître avant le point retenu. Les flux sont appliqués au minimum des deux
timestamps, alors que les deux valeurs d'equity restent celles de leurs derniers
points respectifs.

Enfin, la readiness tolère 6 h de retard trend et 3 h carry
(`readiness.py:32-33`), tandis que `docs/RELIABILITY_SLO.md:9-10` fixe 10 et
20 minutes. Le taux de rejet est aussi 2 % dans le code, 5 % dans le SLO.

**Correction** :

- calculer la disponibilité en buckets attendus (minute ou cinq minutes) ;
- mesurer le drawdown sur la série combinée horodatée, alignée et neutralisée
  des flux, pas sur une clôture quotidienne ;
- partager une configuration de seuils unique entre readiness, health,
  watchdog, dashboard et documentation ;
- ajouter des tests négatifs prouvant qu'une longue panne et un flash drawdown
  font échouer la campagne.

### C3 — Le contrôle CI de l'export des dépendances est voué à échouer

**Sévérité** : haute.

La CI exporte dans `${RUNNER_TEMP}/requirements.txt` puis compare le fichier
entier avec le fichier versionné. `uv` inscrit la commande, donc le chemin de
sortie, dans l'en-tête. La reproduction locale donne un diff sur la première
ligne même lorsque toutes les dépendances et tous les hashes sont identiques.

**Correction** :

- générer et comparer les deux fichiers avec `uv export --no-header`, ou
- supprimer l'en-tête avant comparaison.

Ajouter un test local du workflow avec `act` ou une CI réellement exécutée. Un
pipeline rouge par construction détruit la valeur de la gouvernance.

### C4 — Le cycle de vie des stops live n'est pas transactionnel

**Sévérité** : critique avant live, moyenne tant que le live reste bloqué.

Les ordres marché reçoivent un identifiant stable et sont journalisés avant
l'appel externe. Les stops ne bénéficient pas du même protocole :

- `CcxtBroker.place_stop()` fabrique un identifiant dépendant du temps et d'un
  compteur (`ccxt_broker.py:89-91`, `:213-267`) ;
- un timeout à la création peut laisser un stop distant dont l'identifiant
  local est inconnu ;
- le runner crée le remplacement avant d'annuler l'ancien
  (`runner.py:647-656`, également `:489-496`), mais ne journalise pas cette saga
  avant les appels ;
- un crash ou un échec d'annulation peut laisser deux stops et le redémarrage ne
  sait rechercher que le stop précédemment persisté ;
- après une entrée live, la position est mutée avant la pose du stop
  (`runner.py:564-575`).

Le comportement fail-closed de `ProtectiveStopService` est bon une fois l'état
connu, mais il ne résout pas l'ambiguïté créée à la frontière de l'appel.

**Correction** : créer une table/agrégat `protective_orders`, un
`client_order_id` déterministe dérivé de l'intention de position, une saga
`PLANNED → SUBMITTED → ACTIVE → REPLACED/CANCELED/FILLED`, et une récupération
par identifiant client après timeout. Tester chaque frontière avec crash et
timeout sur sandbox réelle.

### C5 — Le carry live est un prototype dormant, pas un moteur

**Sévérité** : critique avant live ; correctement neutralisée aujourd'hui.

Points positifs : compensation de jambe, statuts `UNBALANCED`, intention
persistée avant la saga et verrou de démarrage.

Points bloquants :

- l'entrypoint affirme à juste titre que le live n'est pas implémenté ;
- `CarryBroker` achète sur le compte spot standard, alors que le modèle x3
  suppose un emprunt/margin de deux fois le capital ;
- le runner live ne met pas l'equity à jour avec les fills, les frais, le
  slippage, le basis, le coût d'emprunt réel ou la marge distante ;
- `CarryLegFill` ne transporte aucun frais ;
- la réconciliation compare tout le solde BTC du compte à la position short,
  sans isoler les avoirs antérieurs ni un sous-compte ;
- les erreurs après timeout n'ont pas de récupération par identifiant client au
  niveau des deux jambes ;
- le script testnet ouvre 150 USDT, mais ne qualifie ni le portfolio margin, ni
  l'emprunt x3, ni la liquidation, ni la comptabilité du produit.

**Décision recommandée** : sortir ce code du runtime principal vers un package
`experimental/testnet_carry` ou le supprimer jusqu'au lancement d'un chantier
dédié. Sa présence dans `execution/` donne une impression de maturité trompeuse.

### C6 — Le backtest et les métriques ont encore des divergences matérielles

**Sévérité** : haute pour la confiance dans les chiffres.

- Le kill switch est mis à jour après la décision de clôture
  (`backtest/engine.py:371-375`). Une violation à la clôture t ne demande la
  sortie qu'à la clôture t+1, exécutée à l'ouverture t+2 : deux barres
  d'exposition supplémentaires.
- L'exposition est la somme des `bars_held` de chaque objet trade
  (`backtest/metrics.py:43-46`). En cas de sorties partielles, plusieurs trades
  réutilisent la durée de la même position et peuvent gonfler l'exposition,
  potentiellement au-delà de 100 %.
- Une entrée simulée rejetée n'est pas retentée : la demande est effacée avant
  le fill (`backtest/engine.py:164-195`), tandis que le runner peut réévaluer le
  signal sur une barre suivante.
- Les hypothèses de timing funding/stop sont conservatrices mais asymétriques :
  un débit est appliqué sur une barre de stop, un crédit peut être omis
  (`backtest/engine.py:231-243`). Ce choix doit être exposé comme scénario,
  pas enfoui dans le moteur.

**Correction** : formaliser une horloge et un modèle d'événements communs,
ajouter des tests de parité trace-à-trace et recalculer les références après
chaque correction.

### C7 — La suite de tests est verte mais sa forme ne suit pas le risque

**Sévérité** : haute.

Couverture faible sur les points les plus sensibles :

| Zone | Couverture observée |
|---|---:|
| `entrypoints/trend.py` | 40 % |
| `entrypoints/readiness.py` | 42 % |
| `entrypoints/carry.py` | 56 % |
| `execution/carry_broker.py` | 62 % |
| `execution/ccxt_broker.py` | 66 % |
| `carry.py` | 69 % |
| `execution/runner.py` | 72 % |

Le seuil global de 80 % permet donc aux adaptateurs externes et aux entrypoints
de rester sous-testés grâce aux modules purs à 90–100 %. Il n'y a pas de test
navigateur, de test système Linux, de test sandbox automatisé, de test de charge,
de test de migration sur anciennes bases réelles ni de test de concurrence
SQLite multi-processus.

**Correction** : seuils par package critique, matrices de crash, tests contractuels
en sandbox planifiés, Playwright pour le dashboard et installation/restauration
sur VM éphémère.

### H1 — Trois god objects concentrent encore la dette

**Sévérité** : haute maintenabilité.

- `StateStore` : 1 345 lignes, schéma, migration, commandes, queries, event log,
  incidents, reporting et readiness ;
- `LiveRunner` : 801 lignes, orchestration, state mapping, funding, stops,
  ordres, accounting, risque, données et boucle ;
- `dashboard/app.py` : 750 lignes et 20 routes ;
- `dashboard/static/dashboard.js` : 1 136 lignes, état, I/O, traduction, DOM et
  graphiques.

La taille seule n'est pas le problème. Ces fichiers changent pour des raisons
indépendantes et leurs tests doivent connaître trop de détails.

**Correction** : repositories spécialisés, services applicatifs, contrôleurs
Flask par blueprint et modules frontend par domaine.

### H2 — Le stockage croît sans requêtes bornées

**Sévérité** : haute à horizon pluriannuel.

Le trend écrit un point par minute, soit environ 525 600 points/an ; le carry
environ 105 120. `read_equity`, `read_orders`, `read_events`, `read_trades` et
`read_flows` chargent tout en mémoire. `execution_health` lit tous les ordres
avant de garder les 200 derniers. Le dashboard invalide son cache à chaque
écriture WAL et reconstruit des Series complètes.

La compaction existe et est appelée par le backup quotidien, mais son échec est
explicitement ignoré. Elle ne constitue pas une politique de rétention
observable.

**Correction** :

- ajouter `LIMIT`, `WHERE ts >=` et agrégations SQL ;
- conserver le brut récent puis des rollups horaires/journaliers ;
- mesurer taille, latence et durée de compaction ;
- tester une base synthétique de cinq ans.

### H3 — Calculs et I/O répétés au même instant

**Sévérité** : moyenne.

À chaque nouvelle barre, les trois slots 4 h font chacun un fetch de 1 000
bougies et recalculent les indicateurs sur le même symbole/timeframe
(`runner.py:599-680`). Cela triple réseau et calcul. Les scripts de référence
recalculent également plusieurs préparations identiques.

**Correction** : cache de frame préparée par `(venue, symbol, timeframe,
last_closed_bar)` et calcul partagé des indicateurs communs. Ne pas paralléliser
les ordres ; paralléliser uniquement les recherches indépendantes.

### H4 — Sauvegarde hors-site insuffisamment observable

**Sévérité** : haute exploitation.

- Le push hors-site est best-effort et son échec ne fait pas échouer le service
  ni n'envoie de notification (`backup_state.sh:51-65`).
- AES-256-CBC via `openssl enc` chiffre mais n'apporte pas une authentification
  cryptographique moderne de l'archive.
- La purge d'un fichier dans une branche Git ne le supprime pas de l'historique ;
  la rétention annoncée de 30 jours est donc fausse côté objet Git.
- Il n'existe aucune preuve automatisée d'un restore périodique complet.

**Correction** : stockage objet versionné avec politique de rétention, chiffrement
authentifié (`age` ou KMS), alerte sur échec, métrique de fraîcheur de backup et
exercice de restauration programmé.

### H5 — Le watchdog promet plus qu'il ne fait

**Sévérité** : moyenne.

Le docstring annonce une tentative de restart systemd
(`entrypoints/watchdog.py:3-7`), mais le code ne redémarre rien ; il ouvre un
incident et notifie. Le choix « alerte sans restart » peut être sain pour un
système financier, mais il doit être explicite et le runbook doit décrire
l'escalade.

### H6 — Pagination de données potentiellement infinie

**Sévérité** : moyenne robustesse.

`data._fetch_paginated()` ne coupe la non-progression que si le batch contient
plus d'une ligne. Un exchange renvoyant répétitivement une seule vieille ligne
peut faire reculer ou stagner le curseur. Le test existant couvre justement un
dernier batch d'une ligne, mais pas une ligne non progressive.

**Correction** : arrêter dès `last_ts < cursor`, indépendamment de la taille, et
borner le nombre de pages.

## 4. Architecture générale

### Ce qui est bien construit

- `src/` layout et package installable ;
- domaine de décision déterministe séparé des I/O ;
- interfaces `Broker`, `MarketDataPort`, `ClockPort`, `Notifier` ;
- entrypoints minces pour trend/carry ;
- stratégies de recherche exclues du registre runtime ;
- persistance centralisée et transactions `BEGIN IMMEDIATE` ;
- intentions d'ordres marché persistées avant l'effet externe ;
- dépendances divisées en groupes production/dev/research ;
- services systemd durcis et déploiements par releases immuables ;
- références historiques assorties de hashes de provenance.

Un senior pourrait conserver cette direction.

### Ce qu'un senior n'aurait pas laissé dans cet état

- une qualification dont les métriques et le parcours réel sont incohérents ;
- une classe de stockage de 1 345 lignes faisant commande, query, migration et
  projection ;
- du live incomplet dans le même namespace que le runtime paper qualifié ;
- des identités d'ordre protecteur non persistées ;
- des constantes 60/40, BTC, Hyperliquid, 6 000/4 000 et noms de moteurs
  disséminés dans dashboard, scripts, readiness et schéma ;
- quatre sources de vérité de seuils : readiness, health, watchdog et SLO ;
- un frontend de 1 136 lignes sans tests comportementaux ;
- une CI dont un contrôle déterministe est cassé.

### Architecture cible

```text
domain/
  portfolio, strategy, risk, execution, qualification
application/
  run_trend_bar, run_carry_tick, reconcile, qualify, rebalance
ports/
  market_data, broker, order_store, state_store, clock, notifier
adapters/
  exchanges/binance, exchanges/hyperliquid
  persistence/sqlite/
  notifications/telegram
entrypoints/
  cli, workers, web
research/
  backtests, experiments, generated references
```

Une architecture hexagonale légère est pertinente. Un DDD complet avec
repositories génériques et dizaines d'agrégats serait excessif. Les agrégats
utiles sont `Portfolio`, `StrategyAccount`, `OrderIntent`,
`ProtectiveOrderSaga`, `CarryPairSaga` et `QualificationCampaign`.

## 5. Qualité du code

### Duplication

- percentile dupliqué dans `health.py` et `readiness.py` ;
- calcul de slippage dupliqué dans les mêmes fichiers ;
- seuils et chaînes de statuts répétés dans schema, runners, health, readiness,
  dashboard et scripts ;
- logique de lecture/reporting enveloppée par plusieurs fonctions triviales
  dans `dashboard/app.py` ;
- constantes portefeuille et venues répétées.

Créer des enums et value objects partagés, sans fabriquer un « utils.py »
générique.

### Code mort ou trompeur

- branches live de `CarryRunner` inaccessibles par l'entrypoint ;
- `CarryBroker` accessible seulement au script testnet manuel ;
- six wrappers `scripts/*.py` ne font qu'appeler les commandes installées ;
- anciens chemins JSON/CSV restent nécessaires à la migration, mais doivent
  avoir une date de retrait ;
- `StateStore.record_trade()` inscrit toujours l'événement sous `trend` :
  l'API générique est trompeuse, même si les appels actuels sont trend/tests.

### Typage

Le succès MyPy est utile mais ne signifie pas typage strict :

- nombreuses signatures `dict`, `dict[str, Any]`, `list[list]` ;
- états sérialisés non typés ;
- dashboard et scripts hors du périmètre MyPy ;
- protocoles partiels, brokers encore basés sur chaînes ;
- `StrategySlot` supprime explicitement un argument `capital_fraction`, signe
  d'une interface en transition.

Remplacer progressivement les payloads critiques par dataclasses/Pydantic ou
TypedDict validés aux frontières. Ne pas typer chaque DataFrame cellule par
cellule : typer ses schémas d'entrée/sortie.

### Exceptions et logs

Points positifs : exceptions d'exécution dédiées, fail-closed de réconciliation,
timeouts réseau, backoff, logs sans secret.

Dettes :

- boucles principales capturent toute `Exception` et continuent indéfiniment ;
- plusieurs repositories legacy avalent toutes les exceptions ;
- pas de taxonomie claire transitoire/permanente/fatale ;
- logs non structurés, pas de `correlation_id` propagé de bout en bout ;
- certaines erreurs de notification sont volontairement perdues.

## 6. Nettoyage précis

### Suppression immédiate sûre — fichiers locaux non suivis

À supprimer quand aucun processus local n'en dépend :

- `.coverage` ;
- `.dashboard-local.err.log`, `.dashboard-local.out.log` ;
- `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.hypothesis/` ;
- `__pycache__/`, `*.pyc` ;
- `.uv-cache/` si l'on accepte de retélécharger ;
- `.venv/` seulement si l'environnement peut être recréé par `uv sync`.

`state/`, `data/`, `backups/` et `reports/` ne sont pas des déchets : ce sont
des données locales non versionnées. Ne pas les supprimer sans sauvegarde.

### Candidats suivis à supprimer après migration documentée

1. `scripts/run_live.py`
2. `scripts/run_carry.py`
3. `scripts/readiness.py`
4. `scripts/rebalance.py`
5. `scripts/watchdog.py`
6. `scripts/daily_digest.py`

Ce sont des wrappers de compatibilité. Les retirer après avoir remplacé tous les
usages par les six commandes `btcquant-*` et annoncé une version de dépréciation.

### À déplacer ou archiver, pas supprimer aveuglément

- `SECURITY_AUDIT.md` : rapport périmé (88 tests, ancien dashboard, assertions
  devenues fausses). Déplacer sous `audit/history/` avec son commit de portée.
- `audit/REMEDIATION_STATUS.md` : historique utile, à figer sous
  `audit/history/` ; ne plus le présenter comme état courant.
- `config.yaml` x1 : déplacer sous `research/configs/` si le profil déployé est
  exclusivement `config_4x.yaml`.
- `CarryBroker` et les branches live carry : isoler dans `experimental/` ou
  supprimer puis reconstruire lors du vrai chantier margin.
- anciens JSON/CSV de migration : garder le code une période définie, mesurer
  l'absence d'anciens déploiements, puis retirer dans une migration majeure.

### À conserver

- `requirements.txt` : utilisé par le déploiement ;
- `uv.lock` : reproductibilité ;
- `sbom.cdx.json` : supply chain ;
- `dashboard/yearly_reference.json` et `audit/baseline_reference.json` :
  assets utilisés et vérifiés ;
- scripts backup/restore/preflight : utiles malgré les corrections requises ;
- tests : aucun fichier entier n'est obsolète, mais `test_audit_fixes.py` doit
  être redistribué par domaine pour éviter un fourre-tout historique.

### Dépendances

Aucune dépendance directe ne peut être déclarée inutilisée :

- pandas/numpy/PyYAML : cœur ;
- ccxt : data/exchanges ;
- Flask/Gunicorn/itsdangerous : dashboard ;
- matplotlib : groupe research ;
- outils dev : CI/tests.

Le problème est la taille transitive de CCXT, pas une dépendance morte. Conserver
le groupe exchange séparé et surveiller son SBOM.

## 7. Architecture Python moderne

| Sujet | Évaluation |
|---|---|
| Packages/imports | bonne base, aucun cycle évident détecté |
| Interfaces | présentes mais incomplètes autour des stores/stops/carry |
| Composition/DI | bonne progression dans `LiveRunner` |
| Héritage | raisonnable, ABC broker/strategy justifiés |
| Dataclasses/enums | bien employés dans le domaine, insuffisants pour états/statuts |
| Typage | intermédiaire, non strict, `Any` dominant aux frontières |
| Exceptions | début de taxonomie, encore trop de `Exception` génériques |
| Configuration | validation manuelle partielle, pas de modèle versionné |
| Migrations | additives et embarquées, pas d'outil/version de downgrade |

Le projet suit plusieurs bonnes pratiques Python 3.11, mais ne satisfait pas un
niveau « plateforme financière » : les contrats de données et les migrations
doivent devenir explicites et versionnés.

## 8. Performance

### Impact élevé

1. Requêtes SQLite non bornées et séries d'equity croissantes.
2. Recalcul complet des projections dashboard à chaque mutation WAL.
3. Trois fetchs/préparations identiques à chaque barre trend.

### Impact moyen

4. Une nouvelle connexion SQLite par opération et deux transactions séparées
   pour checkpoint/equity à chaque tick.
5. `read_events` et `read_orders` récupèrent tout avant slicing côté Python.
6. Les scripts de référence refont plusieurs préparations identiques.

### Impact faible

7. Quelques copies de DataFrame et conversions `iloc/to_numpy` évitables.
8. Compression gzip synchrone dans le worker Flask ; acceptable au volume
   actuel.

La parallélisation des moteurs d'ordre serait dangereuse et inutile. En
recherche multi-actifs, les backtests indépendants peuvent être distribués par
processus, avec seeds et artefacts reproductibles.

## 9. Robustesse et sécurité

### Points forts

- argent réel bloqué inconditionnellement ;
- testnet soumis à qualification et confirmation de session ;
- secrets hors dépôt ;
- comparaison de token en temps constant ;
- cookie HttpOnly/SameSite et Secure derrière proxy ;
- headers CSP, frame, MIME, referrer ;
- aucune injection shell/SQL triviale détectée ;
- dépendances figées, hashes, SBOM, Dependabot et audit CVE ;
- services systemd sans privilèges, filesystem restreint ;
- sauvegarde SQLite cohérente et restore protégé contre path traversal ;
- transaction ordre/checkpoint/trade pour les ordres marché.

### Risques restants

- qualification insuffisante et actuellement impraticable ;
- stops live ambigus ;
- aucun SAST/CodeQL ni secret scanner en CI ;
- protection de branche/CODEOWNERS non prouvée côté GitHub ;
- backup offsite silencieux et rétention Git trompeuse ;
- authentification dashboard mono-token, sans MFA, audit de connexions ni rate
  limiting ; acceptable pour un dashboard personnel, pas multi-utilisateur ;
- données financières dans SQLite non chiffrées au repos ;
- dépendance à un unique VPS et à une unique base locale ;
- pas de haute disponibilité ni de fencing empêchant deux runners actifs sur le
  même compte.

Pour plusieurs millions, il faut surtout un contrôle d'identité du worker
unique, un lease/fencing distribué, une séparation des comptes, un journal
externe immuable et une supervision indépendante du VPS.

## 10. Lisibilité, maintenabilité et évolutivité

### Compréhension par un nouveau développeur

Le README explique bien le produit et les risques financiers. Le flux nominal
est compréhensible en une demi-journée. Le flux de crash/recovery, les statuts,
les multiples timestamps funding et les interactions stop/ordre/checkpoint
exigent toutefois de lire plusieurs gros fichiers et des tests historiques.

### Notes fonctionnelles

| Capacité | Note /10 | Motif |
|---|---:|---|
| Maintenance | 6.0 | tests solides, mais gros orchestrateurs et chaînes d'état |
| Évolution | 5.0 | ports présents, produit encore fortement codé BTC/60-40 |
| Débogage | 6.5 | SQLite/incidents/logs utiles, peu de tracing structuré |
| Testabilité | 7.0 | DI et tests nombreux, gaps système/exchange/frontend |

### Scalabilité à 2 ans

Oui pour un portefeuille personnel paper à un ou quelques actifs, après
optimisation du stockage et modularisation. Non si l'on empile les fonctionnalités
dans `LiveRunner`, `StateStore` et `dashboard.js`.

### Scalabilité à 5 ans

Non dans l'architecture actuelle pour une entreprise :

- **nouveaux exchanges** : données assez extensibles, ordres et credentials
  Binance-spécifiques ;
- **nouvelles stratégies** : relativement facile dans le registre, mais
  préparation et portefeuille restent couplés ;
- **plusieurs utilisateurs** : impossible sans modèle tenant, auth et isolation ;
- **plusieurs bases** : impossible sans repositories/Unit of Work explicites ;
- **plusieurs brokers** : interface de départ correcte, contrats avancés trop
  implicites ;
- **plusieurs IA** : aucun besoin métier établi ; YAGNI. Si des agents sont
  ajoutés, ils ne doivent jamais être dans la chaîne directe d'émission d'ordre
  sans règles déterministes et approbation.

## 11. Standards

| Standard | État |
|---|---|
| Clean Code | moyen : bons noms/commentaires, fonctions et fichiers trop gros |
| SOLID | moyen+ : DI/ports progressent ; SRP violé par runner/store/dashboard |
| DRY | moyen : logique financière plutôt centralisée, seuils/statuts dupliqués |
| KISS | moyen : SQLite monolithique simple, mais event log + projections + legacy s'empilent |
| YAGNI | faible sur live carry dormant ; bon sur l'absence d'IA/microservices |
| Clean Architecture | partielle : domaine séparé, application/infrastructure encore mêlées |
| Hexagonal | partielle et pertinente : ports présents, adapters incomplets |
| DDD | léger seulement ; suffisant si les agrégats critiques sont formalisés |

Ne pas transformer ce petit système en microservices. Un monolithe modulaire,
un seul processus d'écriture par compte et une base transactionnelle suffisent
longtemps. Le besoin est la rigueur des contrats, pas la multiplication des
services.

## 12. Ce qu'un Lead Engineer garderait, supprimerait et réécrirait

### Garder

- stratégies et indicateurs purs ;
- `domain.decision` et le simulateur déterministe ;
- modèles de risque après correction temporelle ;
- idée des ports, DI et entrypoints ;
- SQLite transactionnel et journal d'intentions ;
- tests de crash/recovery existants ;
- pipeline uv/lock/SBOM ;
- durcissement systemd et releases atomiques ;
- provenance des backtests et avertissements financiers.

### Supprimer ou isoler

- wrappers de compatibilité après dépréciation ;
- état historique présenté comme documentation courante ;
- live carry du runtime qualifié ;
- constantes portefeuille dispersées ;
- fallback legacy sans date de retrait ;
- API de store générique quand elle code en réalité `trend`.

### Réécrire

1. readiness autour d'événements/buckets réellement mesurés ;
2. saga des stops externes ;
3. carry live, à partir des contrats réels du compte margin ;
4. séparation command/query du store ;
5. orchestration `LiveRunner` en cas d'usage testnet/live ;
6. frontend en modules testables si le dashboard continue de grandir.

## 13. Roadmap détaillée

Les estimations sont celles d'un senior connaissant Python/CCXT, hors attente
de campagne de 90 jours.

### Priorité critique — avant de considérer le testnet comme qualifié

| Tâche | Difficulté | Gain | Estimation | Risque |
|---|---|---|---:|---|
| Corriger le diff CI `requirements.txt` | faible | pipeline fiable | 0,5 j | faible |
| Journaliser les ordres carry paper | moyenne | débloque readiness réelle | 2–4 j | recalcul historique |
| Test end-to-end campagne via entrypoints | moyenne | supprime la fausse preuve | 3–5 j | temps/test fixtures |
| Refaire uptime par buckets | moyenne | SLO mesurable | 3–5 j | volume SQL |
| Refaire drawdown aligné intrajournalier | élevée | preuve de risque correcte | 4–7 j | références changent |
| Source unique de seuils | moyenne | cohérence docs/code | 2–3 j | migration policy v2 |
| Maintenir argent réel bloqué | faible | évite perte financière | continu | aucun |

### Priorité haute — avant runner testnet continu

| Tâche | Difficulté | Gain | Estimation | Risque |
|---|---|---|---:|---|
| Saga transactionnelle des stops | très élevée | évite position non protégée/double stop | 2–4 sem. | exchange-specific |
| Tests sandbox de timeout/crash/stop partiel | élevée | preuve d'intégration | 1–2 sem. | flakiness/quota |
| Corriger temporalité kill switch backtest | moyenne | chiffres cohérents | 2–4 j | référence historique |
| Corriger exposition sur fills partiels | faible | métriques exactes | 1–2 j | faible |
| Séparer repositories SQLite | élevée | maintenabilité/testabilité | 2–3 sem. | migration API |
| Requêtes bornées + rollups | moyenne | tenue cinq ans | 1–2 sem. | rétention |
| VM éphémère install/restore/rollback | élevée | preuve exploitation | 1–2 sem. | CI Linux |
| Alerte backup + restore périodique | moyenne | vraie reprise | 3–6 j | stockage externe |

### Priorité moyenne

| Tâche | Difficulté | Gain | Estimation | Risque |
|---|---|---|---:|---|
| Découper `LiveRunner` en use cases | élevée | évolutivité | 2–3 sem. | régression |
| Découper Flask en blueprints | moyenne | lisibilité | 3–5 j | faible |
| Découper JS + tests Playwright | élevée | qualité UI | 1–2 sem. | outillage |
| Modèles de config/état typés et versionnés | élevée | sécurité des changements | 1–2 sem. | migration |
| Cache partagé des frames préparées | moyenne | 3× moins d'I/O à la barre | 2–4 j | invalidation |
| Logs JSON + corrélation ordre | moyenne | diagnostic | 3–5 j | dashboards |
| SAST et secret scanning | faible | supply chain | 1–2 j | faux positifs |
| Archiver audits/docs périmés | faible | clarté | 1 j | aucun |

### Priorité faible

| Tâche | Difficulté | Gain | Estimation | Risque |
|---|---|---|---:|---|
| Retirer wrappers après dépréciation | faible | nettoyage | 0,5 j | usages locaux |
| Retirer fallback legacy après échéance | moyenne | simplification | 2–4 j | anciens états |
| Optimisations pandas mineures | faible | performance marginale | 1–3 j | faible |
| Déplacer le profil x1 en research | faible | arborescence claire | 0,5 j | docs |

### Chantier séparé — carry réel

Ne pas l'estimer comme un simple refactor. Prévoir 6 à 12 semaines minimum :

1. choisir le modèle de compte/subaccount/margin ;
2. obtenir les séries réelles borrow/basis/fees ;
3. concevoir accounting double entrée ;
4. implémenter saga et recovery des deux jambes ;
5. qualifier liquidation, transfert, funding et intérêts ;
6. tester sandbox puis capital négligeable avec limites indépendantes ;
7. faire une revue externe finance de marché + sécurité.

## 14. Audit de maturité

### Notes actuelles

| Domaine | Note /10 |
|---|---:|
| Architecture | 6.0 |
| Qualité du code | 6.5 |
| Performance | 5.5 |
| Sécurité | 7.0 |
| Documentation | 6.5 |
| Tests | 7.0 |
| Maintenabilité | 6.0 |
| Évolutivité | 5.0 |
| Lisibilité | 6.5 |
| Robustesse | 6.0 |
| **Globale paper** | **6.2** |
| **Aptitude argent réel important** | **2.0** |

La note paper n'est pas une moyenne autorisant le live. Une chaîne financière
est limitée par son maillon critique : qualification, ordre protecteur,
réconciliation et reprise externe.

## 15. Rapport final

### Excellent

- verrouillage explicite de l'argent réel ;
- intentions d'ordres marché persistées avant effet ;
- tests de crash locaux et transaction ordre/checkpoint/trade ;
- reproductibilité uv/lock/hashes/SBOM ;
- lucidité du README sur drawdown, emprunt et limites.

### Bon

- architecture de domaine émergente ;
- suite de tests pure rapide ;
- validation des données OHLCV ;
- déploiement par releases et systemd durci ;
- auth dashboard adaptée à un usage personnel ;
- absence de secret et de vulnérabilité connue.

### Moyen

- typage ;
- structure du dashboard ;
- performance longue durée ;
- configuration multi-profil ;
- observabilité et documentation opérationnelle.

### Mauvais

- readiness impossible par le flux paper normal ;
- readiness qui mesure une présence quotidienne au lieu de l'uptime ;
- drawdown de qualification sous-échantillonné ;
- CI export cassée par construction ;
- live carry trompeur et incomplet ;
- dette concentrée dans quatre très gros fichiers ;
- absence de tests système/exchange/frontend.

### À refaire absolument

1. qualification paper → testnet ;
2. saga des stops live ;
3. carry live complet ou suppression/isolation de son prototype ;
4. calcul temporel commun backtest/paper pour kill switch et fills ;
5. couche persistence en repositories bornés ;
6. preuve d'exploitation par VM vierge, restore et sandbox.

### Décision CTO

Je conserverais le dépôt et son historique, mais je gèlerais toute fonctionnalité
de stratégie pendant la phase critique. Je consacrerais d'abord quatre à huit
semaines à rendre la qualification honnête, les données temporelles exactes et
les frontières d'ordre récupérables. Ensuite seulement commencerait une nouvelle
campagne paper.

Même après cette campagne, le passage à plusieurs millions d'euros exigerait une
revue indépendante, une séparation des comptes, une supervision hors VPS, un
fencing mono-writer, des limites exchange indépendantes du logiciel et une
montée en capital progressive. Le dépôt actuel n'est pas à une case à cocher de
la production financière ; il dispose toutefois d'assez de bonnes fondations
pour éviter une réécriture totale.
