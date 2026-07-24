# Audit logiciel complet — TANDEM (btc-quant)

**Date** : 2026-07-24
**Auditeur** : revue manuelle fichier par fichier (posture Lead Engineer / CTO)
**Périmètre** : intégralité du dépôt — `src/btcquant`, `dashboard/`, `scripts/`, `deploy/`, `tests/`, config, CI, dépendances, documentation
**Taille** : ~6 360 lignes Python + 2 451 lignes HTML/JS (dashboard) ; 33 fichiers `.py` ; 88 tests (tous verts)

> **Verdict en une phrase** : ce dépôt est **très au-dessus** de ce que produit typiquement une IA livrée à elle-même — code lisible, honnête, documenté, testé sur les points sensibles. Mais pour la barre « entreprise gérant plusieurs millions d'euros », il reste **trois écarts structurels** (dashboard monolithique, couplage mono-actif/mono-utilisateur, état applicatif en fichiers JSON sur un seul nœud) et **un écart de fond qui n'est pas logiciel** : la stratégie elle-même (drawdown simulé −53 %, carry négatif 4 années sur 8) et l'absence totale de validation en argent réel. **Le logiciel n'est pas le principal facteur de risque de ce projet — la stratégie et la résilience opérationnelle le sont.**

---

## 0. Ce qu'il faut retenir avant de lire le détail

Un CTO honnête vous dirait ceci en premier, avant toute discussion d'architecture :

1. **Le code est bon. Il n'est pas le problème.** Un développeur senior serait agréablement surpris. La très grande majorité des dépôts « faits par une IA » sont un plat de spaghettis non testé ; celui-ci ne l'est pas.
2. **Le risque numéro un est financier et opérationnel, pas logiciel.** Tout tourne sur **un seul VPS partagé**, avec l'état stocké dans des **fichiers JSON locaux**. Si ce disque meurt, l'historique et les positions disparaissent. Aucun code propre ne compense ça.
3. **La « scalabilité multi-X » demandée (multi-exchanges, multi-stratégies, multi-utilisateurs, multi-bases, multi-IA) n'existe pas aujourd'hui** et le projet n'est pas construit pour. C'est le plus grand écart avec vos objectifs déclarés. Ce n'est pas grave *si* l'objectif réel reste « un portefeuille perso mono-utilisateur » — c'est bloquant si l'objectif est un produit.
4. **Ne pas confondre « ça marche » et « c'est prêt pour des millions ».** Le README le dit lui-même : composants live *non validés*, drawdown > 50 %, carry dépendant d'une hypothèse de taux d'emprunt non observable. Cette honnêteté est un atout rare — respectez-la, ne l'écrasez pas sous un vernis « pro ».

---

## 1. Architecture générale

### Ce qui est bien conçu (un senior l'aurait fait ainsi)

- **Séparation cœur / bords propre.** `src/btcquant/` (la logique) est isolé de `scripts/` (les commandes), `dashboard/` (la vue), `deploy/` (l'infra). Le package est installable (`pyproject` + `src/` layout), ce qui est la bonne pratique moderne.
- **Le pattern Ports & Adapters (hexagonal) est réellement présent** là où il compte :
  - `Strategy` (ABC) = port de domaine ; `TrendLS`, `TrendSwing`, `IntradayBreakout` = implémentations.
  - `Broker` (ABC) = port d'exécution ; `PaperBroker` / `CcxtBroker` = adaptateurs papier / réel derrière **la même interface**. C'est exactement ce qu'on veut : le runner ne sait pas s'il trade en réel ou en simulation.
  - `Venue` = adaptateur de données live qui **normalise** les divergences entre Binance (funding 8 h) et Hyperliquid (funding horaire). Très bien pensé.
- **La parité backtest/live est un principe d'architecture explicite** et tenu : mêmes classes de stratégie, même séquencement (décision à la clôture, exécution à l'ouverture suivante), même comptabilité du funding. C'est la propriété la plus difficile à obtenir dans un système de trading, et elle est là.
- **Le registre de stratégies** (`STRATEGY_REGISTRY`) + instanciation par config permet plusieurs instances de la même classe (ensemble Donchian 20/55/100). Extensible proprement.

### Ce qui ne va pas

| Problème | Gravité | Détail |
|---|---|---|
| **Dashboard monolithique** | Élevée | `dashboard/app.py` (1 030 l.) mélange auth, cache, gzip, fetch exchange, parsing d'état, calcul de métriques et ~15 routes. `dashboard/index.html` (2 451 l.) embarque tout le CSS/JS inline. C'est le point noir de l'architecture. |
| **Couplage mono-actif** | Élevée | `symbol: BTC/USDT` est câblé partout ; le dashboard code en dur `BTC`, `carry_equity * 3.0`, `6000.0`, `4000.0`. Ajouter ETH/SOL demanderait de toucher des dizaines d'endroits. Le README promet « plus tard : ETH/USDT, SOL/USDT » mais rien n'est prévu pour. |
| **Config non typée** | Moyenne | `load_config()` renvoie un `dict` brut. Seul `risk` est converti en dataclass (`RiskConfig`). Les sections `costs`, `execution`, `data` sont trimballées en dict et lues par clé un peu partout — pas de validation, pas d'autocomplétion, fautes de frappe silencieuses. |
| **État = fichiers JSON locaux** | Élevée (opérationnel) | Positions, cash, équity vivent dans `state/*.json` sur le disque du VPS. Écriture atomique OK, mais **aucune réplication, aucune sauvegarde hors-nœud garantie** (il y a un `backup_state.sh` mais vers où ?). Single point of failure. |

**Réponse directe à votre question** : *un développeur senior aurait-il construit le cœur ainsi ?* → **Oui, à 80 %.** Le domaine (stratégies, moteur, risque, brokers, venue) est du travail de professionnel. **Non** pour le dashboard (monolithe à découper) et **non** pour la persistance (fichiers plats là où il faudrait au minimum SQLite, idéalement une vraie base).

---

## 2. Qualité du code

### Points forts

- **Aucun code mort massif.** Les deux stratégies désactivées (`trend_swing`, `intraday_breakout`) sont **volontairement conservées** comme base de recherche, avec justification écrite dans `config.yaml` (« perdant out-of-sample… base de travail pour itérer »). Ce n'est pas du code mort, c'est de la R&D documentée. Décision défendable.
- **Nommage et commentaires excellents.** Les commentaires expliquent le *pourquoi* (ex. « 1000 barres et non warmup+60 : une EMA200 n'est pas convergée à 280 barres… mesuré : 3,1 % des barres »). C'est le signe d'un vrai raisonnement, pas d'un remplissage IA.
- **Typage moderne** (`from __future__ import annotations`, `X | None`, `list[tuple[...]]`).

### Duplication réelle à corriger

1. **`_with_retries()` dupliqué à l'identique** dans `ccxt_broker.py` et `carry_broker.py` (même corps, même `MAX_RETRIES=4`, même backoff). → Extraire dans un `execution/_ccxt_utils.py` ou une classe de base `CcxtClient`.
2. **`_client_order_id()` / `_coid()` dupliqués** (même schéma d'idempotence) dans les deux brokers. → Idem.
3. **Instanciation ccxt répétée 6×** avec le même dict `{"enableRateLimit": True, "timeout": 30_000}` (`data.py`, `carry.py`, `venue.py`, `ccxt_broker.py`×2, `carry_broker.py`×2). → Une factory `make_exchange(id, **opts)`.
4. **Logique de coupe-circuit dupliquée** — voir §6, c'est le plus grave : `risk.KillSwitch` (utilisé par le backtest) et `LiveRunner._update_kill_switches` (réécrit à la main) implémentent **deux fois** la même règle drawdown/perte-journalière. Le projet vend la parité backtest/live, et pourtant le kill-switch a deux implémentations qui peuvent diverger en silence.
5. **`_append_equity` / lecture CSV** — le pattern « ouvrir en append, écrire l'en-tête si nouveau » est répété (runner, carry_runner, `_record_trade`). Mineur.

### Complexité inutile / anti-patterns

- **`except Exception:` avaleur dans les boucles principales** (`runner.py:430`, `carry_runner.py:179`) : « on continue ». Nécessaire pour la résilience, **mais** aucune alerte n'est émise si l'erreur se répète 1 000 fois de suite (voir §6). Un bot qui échoue à chaque tick paraîtrait « vivant » au watchdog tant qu'il réécrit l'état.
- **`format_metrics` utilise des `lambda` assignées** (`pct = lambda v: ...`) — désactivé exprès dans ruff (`E731`), mais un `def` serait plus lisible. Cosmétique.
- **Dashboard : `summary()` est une fonction de 130 lignes** qui fait 9 fetchs, calcule le PnL du jour, l'exposition, le levier, l'allocation… C'est une god-function.

### Dette technique — ce qui va faire mal plus tard

- Les **constantes financières dupliquées entre config et dashboard** (capital initial 6000/4000, levier 3.0) : si vous changez l'allocation dans la config, **le dashboard mentira** sans erreur. C'est de la dette silencieuse dangereuse (affichage financier faux).
- Le **couplage par nom de fichier** : le dashboard lit `state/live_state_4x.json` en dur, mais `config.yaml` déclare `state/live_state.json` et `config_3x.yaml` déclare `live_state_3x.json`. Changer de profil casse le dashboard sans le dire.

---

## 3. Plan de nettoyage

| Élément | Verdict | Action |
|---|---|---|
| `config_3x.yaml` | **Orphelin** — référencé nulle part (aucun script, aucun service ne le lance ; le carry x3 tourne via `run_carry.py --leverage 3`, pas via ce YAML). | **Supprimer**, ou le fusionner dans une config paramétrée (§11). |
| `scripts/multiasset_experiments.py` | Script de recherche exploratoire (163 l.), pas du code de production. | **Déplacer** dans un dossier `experiments/` ou `notebooks/` pour clarifier son statut. |
| `dashboard/backtest_reference.json`, `yearly_reference.json` | Utiles (référence affichée), régénérés par script. | **Garder**, mais documenter leur régénération. |
| Stratégies désactivées | R&D documentée. | **Garder** (décision assumée), mais isoler dans un sous-package `strategies/experimental/` pour ne pas les confondre avec la prod. |
| `SECURITY_AUDIT.md` | Audit réel, daté, précis. | **Garder** ; à terme, fusionner les rapports d'audit dans un `docs/`. |
| Dépendances | `matplotlib` n'est utilisé que par `run_backtest.py` (génération PNG). `flask` seulement par le dashboard. | Aucune dépendance morte détectée. Sain. À terme, `matplotlib`/`flask` pourraient être des **extras optionnels** (`pip install btcquant[dashboard]`). |
| Scripts | Tous référencés par un service/timer ou l'usage manuel documenté. | Aucun script mort. |
| README / docs | À jour, cohérents avec le code. Le README est **excellent** et honnête. | Garder. |

**Bilan nettoyage** : le dépôt est déjà propre. Un seul vrai orphelin (`config_3x.yaml`), un script à reclasser (`multiasset_experiments.py`). C'est remarquable.

---

## 4. Architecture Python

- **Packages/imports** : structure `src/` correcte, imports relatifs cohérents. **Pas de dépendance circulaire** (le seul import différé, `from .reconcile import reconcile` dans `runner.py:400`, n'est pas dû à un cycle — c'est un import paresseux au démarrage live, acceptable mais gratuit ; il pourrait remonter en tête de fichier).
- **Interfaces/héritage** : `Strategy` et `Broker` sont de vraies ABC avec `@abstractmethod`. Composition privilégiée à l'héritage (le runner *compose* broker + venue + slots). Bon.
- **Injection de dépendances** : faite « à la main » via constructeurs (`LiveRunner(slots, broker, risk, …)`). Pas de framework DI — **et c'est le bon choix** à cette échelle (KISS). Un senior n'ajouterait pas de conteneur DI ici.
- **Dataclasses/enums/constantes** :
  - Dataclasses bien utilisées (`Position`, `Trade`, `RiskConfig`, `Fill`, `BacktestResult`).
  - **Aucun `Enum`** là où il en faudrait : `direction` est un `int` +1/−1 avec des commentaires partout, `market` est un `str` « spot »/« perp », `exit_reason` est un `str` libre (« stop », « signal », « kill_switch », « end_of_data »…). → **`Enum` recommandé** pour `Direction`, `Market`, `ExitReason` : élimine des classes entières de bugs (un `"prep"` mal orthographié, un +2 impossible).
  - Constantes bien nommées et localisées (`BARS_PER_YEAR`, `PAYMENTS_PER_YEAR`, `DEFAULT_BORROW_RATE_ANN`).
- **Exceptions** : usage correct de `raise … from None`, exceptions ccxt catchées finement dans les brokers/data. Mais **aucune exception métier propre** (`class InsufficientDataError(Exception)`, `class ReconciliationError`) — tout passe par `ValueError`/`RuntimeError` génériques.
- **Typage** : bon au niveau des signatures, **mais aucun `mypy`/`pyright` en CI**. Le typage n'est donc pas *vérifié* — il documente sans garantir. Pour un système financier, ajouter un type-checker est un gain net.

**Respecte-t-il les bonnes pratiques Python modernes ?** → **Oui à 85 %.** Manquent : les `Enum` de domaine, un type-checker en CI, et la validation de config (Pydantic serait ici parfaitement justifié).

---

## 5. Performance

Contexte : ce système traite quelques milliers de barres 4h et tourne en boucle toutes les 60 s. **La performance n'est pas un problème réel** — mais vous l'avez demandé, voici le classement par impact.

| Optimisation | Impact | Détail |
|---|---|---|
| **Indicateurs vectorisés** ✅ déjà fait | — | `ema/atr/adx/donchian` en pandas/numpy pur, pas de boucle Python. Bien.
| **Moteur de backtest = boucle Python barre par barre** | Faible | `for i in range(start, len(data))` avec `data.iloc[i]` par ligne. Sur ~15 000 barres c'est <1 s ; sur du 1m multi-années (~5 M barres) ça deviendrait pénible. Pré-extrait déjà les colonnes en numpy (`opens`, `highs`…) — bien joué. À ne PAS optimiser prématurément (YAGNI). |
| **`strategy.prepare(df)` fait `df.copy()`** | Faible | Copie complète à chaque appel. Nécessaire pour l'immutabilité (testé). OK. |
| **Dashboard : appels ccxt sérialisés derrière un verrou** | Moyen (latence UX) | Commentaire honnête : « 9 fetchs simultanés au chargement… on sérialise ». Un cache TTL est en place (`_cached`), et un `_warm_loop` préchauffe. Correct, mais la première visite « à froid » est lente. |
| **CSV d'équity append-only, une ligne / 60 s** | Moyen (I/O + croissance) | ~1 440 lignes/jour (trend). `compact_equity.py` (cron) compacte l'ancien à l'heure — **c'est un pansement sur un design append-only**. Une vraie série temporelle (SQLite, Parquet) éliminerait le script. |
| **`walk_forward` : grid-search séquentiel** | Faible | Parallélisable (`joblib`/`multiprocessing`) si les grilles grossissent. Aujourd'hui inutile. |

**Conclusion perf** : rien d'urgent. Le seul vrai gain structurel serait de **remplacer les CSV append-only par SQLite** (supprime `compact_equity.py`, requêtes plus rapides, pas de « dernière ligne torn » à gérer — d'ailleurs un test existe déjà pour ce cas, `test_digest_tolerates_torn_last_line`, preuve que le design CSV crée ses propres bugs).

---

## 6. Robustesse

C'est le chapitre le plus important pour de l'argent réel, et le bilan est **mitigé**.

### Ce qui est solide

- **Écriture d'état atomique** (`tmp` + `os.replace`) — testé. Un crash pendant l'écriture ne corrompt pas l'état. Excellent réflexe.
- **Retries réseau avec backoff exponentiel** dans `data.py`, `ccxt_broker.py`, `carry_broker.py`.
- **Idempotence des ordres** via `clientOrderId` déterministe — un retry ne double pas un ordre. C'est du niveau pro et rare.
- **Comptabilisation du fill réellement exécuté** (`order["filled"]`, jamais la quantité demandée) — évite les positions fantômes. Testé.
- **Réconciliation état local ↔ exchange** au démarrage live (`reconcile.py`, `CarryBroker.reconcile`).
- **Gestion de la jambe orpheline du carry** : si le short perp échoue après l'achat spot, on revend le spot ; si ça échoue aussi → alerte « POSITION LONGUE NUE, intervention manuelle ». Scénario critique correctement pensé.
- **Kill-switches** : drawdown max → liquidation + veille ; perte journalière → lockout. Le stop suiveur ne peut jamais être desserré (testé).

### Ce qui est fragile

1. **⚠ Kill-switch dupliqué (le risque n°1 de robustesse).** `KillSwitch` (backtest) et `LiveRunner._update_kill_switches` (live) sont **deux implémentations séparées** de la même règle. Un bug corrigé dans l'une ne l'est pas dans l'autre. Pour un système qui *promet* la parité, c'est la faille conceptuelle la plus dangereuse. → **Unifier** : le live doit réutiliser `KillSwitch`.
2. **`except Exception: … on continue` sans compteur d'échecs.** Si le réseau gèle ou qu'une exception se répète indéfiniment, le bot boucle en écrivant un état frais → le **watchdog le croit vivant** alors qu'il ne trade plus. Il manque un « N échecs consécutifs → notifier + éventuellement s'arrêter ».
3. **Validation des entrées quasi absente.** La config n'est pas validée (types, plages : un `leverage: -3` ou `risk_per_trade: 5.0` passerait). `position_size` se protège (retourne 0 si incohérent) mais en amont rien ne vérifie que la config a du sens.
4. **Pas de timeout global sur un tick.** `_wait_closed` a un timeout de 30 s par ordre, mais un `_process_bar` qui fait plusieurs appels réseau peut cumuler. Acceptable, mais non borné.
5. **Reprise après erreur** : bonne au niveau *état* (reload JSON), mais **il n'y a pas de test d'intégration du runner** qui simule un crash-reprise complet. Les briques sont testées, pas l'assemblage.

---

## 7. Sécurité

Déjà audité (`SECURITY_AUDIT.md`, 2026-07-23) et de façon sérieuse. Je confirme et complète.

- **Secrets** : ✅ tout est en variables d'environnement (`BINANCE_API_KEY/SECRET`, `TELEGRAM_*`, `DASHBOARD_TOKEN`). Aucun secret commis. `.gitignore` exclut `.env`, `state/`, `backups/`, `data/`. Impeccable.
- **Injections** : ✅ pas d'`eval`/`exec`/`os.system`/`shell=True` ; `yaml.safe_load` uniquement ; le seul `subprocess` (watchdog) a des arguments fixes.
- **Dashboard** : capability-URL (jeton aléatoire, cookie 1 an), comparaison en temps constant (`hmac.compare_digest`), 404 au lieu de 401, en-têtes de sécurité, cookie `Secure` derrière TLS, `ProxyFix` 1-hop. Sérieux.
- **Dépendances** : CVE `setuptools` déjà corrigé via bump ccxt. Bonne hygiène.

### Points que je soulève en plus (non traités par l'audit existant)

- **Le modèle capability-URL reste faible pour de l'argent réel.** Un jeton dans une URL fuit facilement (historique navigateur, logs proxy, partage d'écran). Pour un dashboard qui affichera un jour des positions à plusieurs zéros, c'est léger. → à terme : vraie auth (mot de passe + 2FA) si l'enjeu monte.
- **Pas de rotation ni de scoping des clés API.** Recommandation forte : clés API exchange **en lecture+trading uniquement, JAMAIS retrait**, IP-whitelistées sur l'IP du VPS. (À vérifier côté exchange — hors dépôt, mais critique.)
- **VPS partagé** avec d'autres projets (smc-spot, neobank, pumpbot…). Pour un système financier, la co-location avec du code tiers sur la même machine est un risque : une faille dans un autre projet peut lire `state/` ou `.env`. → un système « millions d'euros » doit avoir sa **propre machine isolée**.
- **Pas de `pip-audit`/`dependabot` en CI automatique** (la CI est en sommeil, cf. §Tests). Le CVE a été trouvé manuellement — bien, mais ça devrait être automatique.

---

## 8. Lisibilité

**Excellente — c'est le point le plus fort du projet.** Un développeur qui découvre le dépôt comprend vite grâce à :

- des **docstrings de module** qui expliquent le *pourquoi* et les invariants (le contrat anti-look-ahead dans `base.py`, le séquencement barre par barre dans `engine.py`) ;
- un **README exemplaire** : honnête sur les limites, chiffré, avec l'historique des corrections datées ;
- des commentaires qui documentent les décisions *contre-intuitives* (pourquoi 1000 barres, pourquoi deux colonnes de funding, pourquoi le carry x1 est meilleur que x3).

Le flux de données est traçable : `data.py` → `strategy.prepare` → `engine`/`runner` → `broker`/`venue` → `state/` → `dashboard`. Les responsabilités sont claires **sauf dans le dashboard**, où `app.py` fait trop de choses.

Réserve mineure : tout est en **français** (code commenté, docs, messages). Cohérent et assumé, mais ça limite la contribution externe et l'usage d'outillage anglophone. Décision de produit, pas un défaut.

---

## 9. Maintenabilité — notes détaillées

| Critère | Note /10 | Justification |
|---|---:|---|
| Facilité de maintenance | 7 | Cœur excellent ; dashboard et duplications brokers pénalisent. |
| Facilité d'évolution | 6 | Ajouter une *stratégie* est trivial (registre) ; ajouter un *actif* ou un *exchange de trading* est difficile (couplage BTC/USDT, dashboard en dur). |
| Facilité de débogage | 7 | Logs structurés, journal de trades CSV, état lisible en JSON. Manque : corrélation d'IDs, niveaux de log configurables. |
| Facilité de test | 7 | Domaine très testable et testé ; l'exécution live et le dashboard le sont beaucoup moins (dépendances réseau non mockées). |

---

## 10. Scalabilité — la réponse franche

Vous demandez si le projet supportera « plusieurs exchanges, stratégies, utilisateurs, bases de données, brokers, IA ». Réponse honnête, axe par axe :

| Axe | État aujourd'hui | Verdict à 2 ans / 5 ans |
|---|---|---|
| **Nouvelles stratégies** | Registre + config. | ✅ **Déjà scalable.** C'est le seul axe vraiment prêt. |
| **Nouveaux exchanges (données)** | `Venue` normalise Binance/Hyperliquid. | 🟡 Extensible avec effort par exchange. Bon socle. |
| **Nouveaux brokers (trading)** | Interface `Broker` propre. | 🟡 Ajouter un broker = une classe. Mais tout suppose BTC/USDT. |
| **Nouveaux actifs** | `symbol` câblé, dashboard en dur. | 🔴 **Non prêt.** Refonte nécessaire (paramétrer l'univers d'actifs). |
| **Plusieurs utilisateurs** | Aucun concept d'utilisateur. État global unique, dashboard mono-tenant. | 🔴 **Non prêt du tout.** Ce serait une réécriture (auth, isolation des états, base multi-tenant). |
| **Plusieurs bases de données** | Aucune base : fichiers JSON/CSV. | 🔴 **Non prêt.** Il n'y a même pas *une* base. |
| **Plusieurs IA** | Non applicable / non conçu. | 🔴 Hors périmètre actuel. |

**Conclusion scalabilité** : le projet est un **excellent moteur mono-actif / mono-utilisateur / mono-nœud**. Il **n'est pas** une plateforme multi-tenant, et rien dans l'architecture actuelle ne le prépare à le devenir. **Dans 2 ans, s'il reste un portefeuille perso BTC, il tiendra sans problème.** S'il doit devenir un produit multi-utilisateurs, il faut considérer que la couche exécution/état/dashboard sera **réécrite**, en gardant le domaine (stratégies, moteur, risque, métriques) qui, lui, est réutilisable tel quel.

---

## 11. Respect des standards

| Principe | Note | Écarts |
|---|---|---|
| **Clean Code** | Bon | Fonctions courtes et nommées *sauf* `summary()` et le dashboard. Commentaires de qualité. |
| **SOLID** | Bon sur S/O/L/I, faible sur D | *Single Responsibility* violé par `dashboard/app.py`. *Open/Closed* respecté (stratégies/brokers extensibles sans modifier le moteur). *Liskov* OK (PaperBroker/CcxtBroker substituables). *Interface Segregation* OK. *Dependency Inversion* : le runner dépend d'abstractions (`Broker`, `Strategy`) — bien — mais la config concrète en dict fuit partout. |
| **DRY** | Moyen | Violé : `_with_retries`, `_coid`, instanciation ccxt, **kill-switch** (le plus grave), constantes capital/levier entre config et dashboard. |
| **KISS** | Excellent | Pas de sur-ingénierie, pas de framework inutile, pas d'abstraction spéculative. Un vrai point fort. |
| **YAGNI** | Excellent | Le levier, le short, le funding sont là parce qu'ils servent. Pas de « au cas où ». |
| **Clean/Hexagonal Architecture** | Partiel | Ports/adapters présents (Broker, Venue, Strategy) — c'est déjà rare et bien. Mais pas de couche *application* explicite ni de *domain model* isolé de pandas (le domaine dépend directement de `pd.Series`/`pd.DataFrame`, ce qui couple la logique métier à pandas). |
| **DDD** | Peu pertinent | À cette taille, DDD serait de la sur-ingénierie. Les `Position`/`Trade` sont déjà de bons objets de domaine. Ne pas forcer DDD ici. |

Écart conceptuel notable : **le domaine dépend de pandas**. `entry_signal(row: pd.Series)` fait que la logique de stratégie est indissociable de pandas. Pour du trading systématique c'est un choix pragmatique courant et acceptable ; un puriste hexagonal l'isolerait, mais **je ne le recommande pas** (coût > bénéfice ici).

---

## 12. Ce qu'un Lead Engineer referait

**Ce qu'il garderait tel quel (c'est bon) :**
- Tout `src/btcquant/{indicators,risk,carry}.py`, `strategies/`, `backtest/{engine,metrics,walkforward}.py`, `execution/{broker,venue,reconcile}.py`.
- Le README, les docstrings, la discipline de tests, l'écriture d'état atomique, l'idempotence des ordres.
- Le pattern ports/adapters.

**Ce qu'il supprimerait / reclasserait :**
- `config_3x.yaml` (orphelin).
- `multiasset_experiments.py` → `experiments/`.
- Les stratégies désactivées → `strategies/experimental/`.

**Ce qu'il réécrirait complètement :**
- **Le dashboard** (`app.py` + `index.html`). Découpage en blueprints Flask (`auth`, `market`, `portfolio`, `analytics`) + un vrai front séparé, ou au minimum un templating. C'est le chantier n°1 de qualité.
- **La couche de persistance** : passer de JSON/CSV à **SQLite** (une base, un schéma, des transactions). Supprime `compact_equity.py`, les bugs de « ligne torn », et pose les fondations multi-actifs.

**Ce qu'il factoriserait (sans réécrire) :**
- Unifier le kill-switch (une seule implémentation, backtest + live).
- Extraire un `CcxtClient` de base (retries + coid + factory) hérité par les deux brokers.
- Introduire une config typée (Pydantic) et des `Enum` de domaine.
- Centraliser les constantes capital/levier/allocation (source unique lue par runner ET dashboard).

---

## 13. Plan de refactoring priorisé

### 🔴 Priorité CRITIQUE (à faire avant toute pensée d'argent réel)

| Tâche | Difficulté | Gain | Temps | Risque |
|---|---|---|---|---|
| **Unifier le kill-switch** (live réutilise `risk.KillSwitch`) | Moyenne | Élimine le risque de divergence backtest/live du coupe-circuit | 0,5–1 j | Faible (couvert par tests si on en ajoute) |
| **Compteur d'échecs consécutifs + alerte** dans les boucles `run_forever` | Faible | Détecte un bot « zombie » que le watchdog croit vivant | 0,5 j | Faible |
| **Centraliser capital/levier/allocation** (une source lue par runner + dashboard) | Faible | Supprime le risque d'affichage financier faux | 0,5 j | Faible |
| **Isoler le VPS de production** (machine dédiée, clés API sans retrait + IP-whitelist) | Moyenne (infra) | Supprime le risque de compromission par co-location | 1 j | Moyen |

### 🟠 Priorité HAUTE

| Tâche | Difficulté | Gain | Temps | Risque |
|---|---|---|---|---|
| **Découper le dashboard** en blueprints + séparer front | Élevée | Maintenabilité, testabilité | 3–5 j | Moyen (bien tester les routes) |
| **Config typée (Pydantic) + validation de plages** | Moyenne | Sécurité, fin des fautes silencieuses | 1–2 j | Faible |
| **`Enum` de domaine** (`Direction`, `Market`, `ExitReason`) | Faible | Robustesse, moins de bugs de chaîne | 1 j | Faible |
| **Réactiver + durcir la CI** (mypy/pyright + pip-audit + le hook local en garde) | Faible | Filet automatique | 0,5 j | Faible |

### 🟡 Priorité MOYENNE

| Tâche | Difficulté | Gain | Temps | Risque |
|---|---|---|---|---|
| **Migrer l'état JSON/CSV → SQLite** | Élevée | Supprime `compact_equity.py`, fiabilité, base du multi-actifs | 3–4 j | Moyen (migration de données) |
| **Factoriser `CcxtClient`** (retries/coid/factory) | Faible | DRY | 0,5 j | Faible |
| **Tests d'intégration runner** (crash-reprise mocké) | Moyenne | Confiance sur l'assemblage | 2 j | Faible |

### 🟢 Priorité FAIBLE

- Supprimer `config_3x.yaml`, reclasser `multiasset_experiments.py` et les stratégies expérimentales.
- Rendre `matplotlib`/`flask` des extras optionnels.
- Passer les `lambda` de `format_metrics` en `def`.

---

## 14. Audit de maturité — notes /10

> Barre de notation : « logiciel destiné à gérer plusieurs millions d'euros », pas « projet perso ». Un projet perso solo mériterait +1,5 partout.

| Domaine | Note | Commentaire |
|---|---:|---|
| **Architecture** | **6,5** | Cœur hexagonal excellent ; dashboard monolithe et persistance fichiers plombent. |
| **Qualité du code** | **7,5** | Propre, documenté ; duplications brokers/kill-switch/dashboard à corriger. |
| **Performance** | **7,0** | Vectorisé, largement suffisant ; CSV append-only est le seul vrai design perfectible. |
| **Sécurité** | **7,0** | Hygiène des secrets exemplaire ; capability-URL et VPS partagé limitent pour du « millions ». |
| **Documentation** | **9,0** | Le point fort. Rare à ce niveau, y compris chez des équipes pro. |
| **Tests** | **6,5** | 88 tests unitaires solides sur le domaine ; manque intégration/live/dashboard ; CI en sommeil. |
| **Maintenabilité** | **6,5** | Excellente sur le cœur, faible sur dashboard. |
| **Évolutivité** | **5,0** | Stratégies : oui. Actifs/utilisateurs/bases : non — écart majeur avec vos objectifs. |
| **Lisibilité** | **8,5** | Nommage et commentaires de très haut niveau. |
| **Robustesse** | **6,5** | Idempotence/atomicité/réconciliation excellentes ; kill-switch dupliqué et absence d'alerte sur échec répété. |

### 🎯 Note globale : **6,8 / 10**

**Interprétation :** pour un projet largement écrit par une IA et maintenu par un non-développeur, c'est un résultat **remarquable** (la moyenne de cette catégorie est autour de 3–4). Pour la barre « entreprise, millions d'euros, multi-tenant », il faut viser **8,5+**, ce qui demande le plan §13 **et** — surtout — une validation réelle de la stratégie et de l'exécution, aujourd'hui inexistante.

---

## 15. Rapport final

### ✅ Ce qui est EXCELLENT
- La **documentation** (README, docstrings, historique daté des corrections) et l'**honnêteté intellectuelle** (limites assumées, drawdown affiché, hypothèses du carry explicitées).
- La **discipline anti-look-ahead** et la **parité backtest/live** comme principes d'architecture.
- L'**idempotence des ordres**, l'**écriture d'état atomique**, la **gestion de la jambe orpheline** du carry — du réflexe de professionnel du trading.
- Le **pattern ports/adapters** (Broker, Venue, Strategy).

### 🟢 Ce qui est BON
- La qualité générale du code, le nommage, le typage des signatures, l'absence de sur-ingénierie (KISS/YAGNI exemplaires).
- La couverture de tests du domaine (indicateurs, moteur, financement du carry, parité funding).
- L'hygiène de sécurité (secrets, injections, dépendances).

### 🟡 Ce qui est MOYEN
- La **persistance** (JSON/CSV append-only avec scripts-pansements).
- La **config** (dict non typé, trois fichiers dupliqués, un orphelin).
- La **robustesse opérationnelle** des boucles (échecs avalés sans alerte, kill-switch dupliqué).
- La **CI** (en sommeil, pas de type-checker ni d'audit auto).

### 🔴 Ce qui est MAUVAIS / à refaire
- **Le dashboard** (`app.py` 1 030 l. + `index.html` 2 451 l. monolithiques, constantes financières en dur qui peuvent mentir). **À réécrire.**
- **L'aptitude au multi-actifs / multi-utilisateurs / multi-bases** : inexistante. Si c'est un objectif réel, la couche exécution+état+vue est **à reconstruire** (le domaine, lui, se garde).

### Ce qui DOIT absolument être refait avant de l'utiliser pour de l'argent réel
1. **Valider réellement l'exécution live sur testnet** (elle est codée, jamais validée — le README le dit).
2. **Unifier le kill-switch** et **ajouter l'alerte sur échec répété** (§6, §13).
3. **Isoler l'infrastructure** (machine dédiée, clés API restreintes, sauvegarde d'état hors-nœud).
4. **Regarder la stratégie en face** : −53 % de drawdown simulé, carry négatif 4 années sur 8, edge fragile. *Aucune qualité de code ne rend une stratégie perdante gagnante.* C'est le vrai sujet « millions d'euros ».

---

## Roadmap de transformation vers un niveau professionnel

**Phase 0 — Sécuriser l'existant (1–2 semaines)** *[ne rien casser, boucher les trous critiques]*
→ Kill-switch unifié · compteur d'échecs + alerte · constantes financières centralisées · CI réactivée avec mypy + pip-audit · infra isolée. **Résultat : le mono-actif perso devient fiable.**

**Phase 1 — Assainir (2–4 semaines)**
→ Config Pydantic + Enums de domaine · factorisation `CcxtClient` · migration état → SQLite (supprime les scripts-pansements) · tests d'intégration runner · nettoyage (orphelins, reclassement R&D). **Résultat : dette technique résorbée, base saine.**

**Phase 2 — Réécrire le dashboard (2–4 semaines)**
→ Backend en blueprints testables · front séparé lisant une API propre · plus aucune constante financière en dur. **Résultat : la partie la plus faible passe au niveau du reste.**

**Phase 3 — Décision produit (si et seulement si l'objectif est une plateforme)**
→ Univers d'actifs paramétré · notion d'utilisateur + isolation d'état · base multi-tenant. **C'est un nouveau projet bâti sur le domaine actuel, pas une évolution incrémentale — à ne lancer que si l'objectif business le justifie.**

**Phase transverse et permanente — la stratégie**
→ Continuer la validation walk-forward, le suivi paper-vs-backtest, et **ne jamais passer en réel sans avoir tenu le paper trading assez longtemps pour observer un vrai drawdown**. C'est là, et non dans le code, que se gagnent ou se perdent les millions.

---

*Rapport établi par revue manuelle exhaustive du dépôt à la date du 2026-07-24. Les 88 tests passent, `ruff` est vert. Ce document est une évaluation d'ingénierie ; il ne constitue pas un conseil financier.*
