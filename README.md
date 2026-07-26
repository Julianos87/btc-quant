# TANDEM — portefeuille systématique BTC trend + carry

> Deux moteurs complémentaires : le **trend** cherche à capter les tendances du marché ; le **carry** étudie une exposition delta-neutre au funding.

[![Python](https://img.shields.io/badge/Python-%E2%89%A53.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
![Mode](https://img.shields.io/badge/mode-paper%20trading-orange)
![Status](https://img.shields.io/badge/statut-exp%C3%A9rimental-blue)

## Présentation

TANDEM est un portefeuille systématique expérimental sur Bitcoin composé de deux poches :

| Poche | Allocation initiale | Profil paper |
|---|---:|---|
| Trend following | 60 % | Ensemble Donchian long/short à x4 |
| Cash-and-carry | 40 % | Modèle de funding à x3 |

Le VPS exécute actuellement les deux moteurs en **paper trading**. Aucun ordre réel n’est envoyé par les services actifs.

## Profil réellement exécuté

### Trend — 60 %

Le service `btcquant-trend` lance :

```bash
btcquant-trend --config config_4x.yaml
```

Le capital initial de la poche est de 6 000 USDT. Il est réparti entre trois horizons :

| Sous-stratégie | Canal de Donchian | Allocation de la poche |
|---|---:|---:|
| `trend_ls_20` | 20 périodes | 33,33 % |
| `trend_ls_55` | 55 périodes | 33,33 % |
| `trend_ls_100` | 100 périodes | 33,34 % |

Chaque stratégie utilise également un régime EMA 50/200, un filtre ADX, un stop ATR et un dimensionnement par risque et volatilité.

Le profil `config.yaml` à x1 reste disponible comme profil de recherche prudent, mais ce n’est pas celui lancé par le service VPS principal.

### Carry — 40 %

Le service `btcquant-carry` lance :

```bash
btcquant-carry --capital 4000 --leverage 3
```

Le modèle paper simule une position spot longue et une position perpétuelle courte. Il entre lorsque le funding annualisé lissé dépasse 3 % et sort lorsqu’il devient négatif.

### Coût de financement du levier — chiffré depuis le 18 juillet 2026

Un carry à levier L immobilise L×capital de spot alors qu’on ne dispose que du
capital : les (L−1)×capital manquants doivent être **empruntés**, et cet emprunt
se paie tant que la position est ouverte. Jusqu’au 18 juillet 2026 le modèle
créditait `capital × funding × levier` sans jamais débiter ce portage, ce qui
surestimait le rendement d’environ un facteur deux et produisait un Sharpe de 12.

`backtest_carry()` et `CarryRunner` appliquent désormais la même formule :

```
rendement par période = L × funding − (L−1) × borrow_rate_ann / paiements_par_an
```

Le taux est réglable (`--borrow-rate`, `DEFAULT_BORROW_RATE_ANN = 10 %/an`). Il
n’est pas observable a posteriori et **varie fortement — il monte précisément
quand le funding monte**, les deux traduisant la même demande de levier. Les
chiffres du carry sont donc conditionnels à cette hypothèse :

| Taux d’emprunt | CAGR x3 | Sharpe | Max DD |
|---:|---:|---:|---:|
| 0 % (ancien modèle) | +36,1 % | 12,00 | -3,8 % |
| 5 % | +25,6 % | 8,99 | -5,9 % |
| **10 % (défaut)** | **+16,0 %** | **5,89** | **-11,6 %** |
| 15 % | +7,1 % | 2,74 | -29,9 % |
| 20 % | -1,1 % | -0,45 | -51,1 % |

À levier 1, la position est intégralement financée par le capital : aucun
emprunt, donc aucune dépendance à ce taux. C’est le seul profil réalisable sans
compte sur marge, et il présente le meilleur couple rendement/risque :

| Levier | CAGR | Sharpe | Max DD |
|---:|---:|---:|---:|
| **x1** | +10,8 % | **12,00** | **-1,3 %** |
| x2 | +13,4 % | 7,45 | -5,8 % |
| x3 (profil actif) | +16,0 % | 5,89 | -11,6 % |
| x4 | +18,6 % | 5,11 | -18,8 % |

Passer de x1 à x3 gagne 5 points de CAGR mais multiplie le drawdown par 9 et
divise le Sharpe par deux, tout en introduisant une dépendance à un taux
d’emprunt volatil. Le profil paper reste à x3 pour l’instant ; **ce choix mérite
d’être rediscuté avant tout passage en réel**.

## Résultats historiques de référence

Le fichier `dashboard/yearly_reference.json`, régénéré le 18 juillet 2026, rejoue le profil paper 60/40 sur la période du 10 septembre 2019 au 18 juillet 2026 avec funding historique :

| Année | Portefeuille 60/40 | Trend | Carry | BTC |
|---|---:|---:|---:|---:|
| 2019, partielle | +26,7 % | +34,3 % | -1,1 % | -28,7 % |
| 2020 | +157,3 % | +180,6 % | +41,3 % | +302,0 % |
| 2021 | +20,2 % | +11,4 % | +107,2 % | +59,8 % |
| 2022 | -1,0 % | +0,3 % | -8,3 % | -64,2 % |
| 2023 | +199,9 % | +233,7 % | +3,7 % | +155,6 % |
| 2024 | +55,0 % | +56,9 % | +17,8 % | +121,3 % |
| 2025 | -20,7 % | -21,3 % | -6,4 % | -6,3 % |
| 2026, partielle | -9,3 % | -9,6 % | -3,2 % | -26,6 % |

La colonne carry intègre le coût de financement du levier x3 (voir plus bas).
Une fois ce coût chiffré, **le carry devient négatif 4 années sur 8** — dont
2025 et 2026, où les deux poches perdent simultanément. La corrélation annuelle
trend/carry reste faible (+0,06), mais la diversification protège moins que ce
que suggéraient les chiffres non financés.

La référence du moteur trend à x4 indique également :

| Indicateur trend | Valeur |
|---|---:|
| Trades | 479 |
| Trades par an | 64,6 |
| Win rate | 38,0 % |
| Perte moyenne | -3,08 % |
| Gain moyen | +9,59 % |
| Drawdown maximal historique | -52,9 % |
| Plus longue série de pertes | 21 trades |

Ces chiffres intègrent le filtre d'entrée funding, resté inactif en backtest
jusqu'au 18 juillet 2026 (voir ci-dessous). Le gain de 2021 sur la poche trend
(+0,3 % → +11,4 %) vient de là : c'est l'année où le funding a été le plus
durablement extrême, donc celle où le filtre écarte le plus d'entrées.

Ces chiffres sont des résultats de simulation. Ils ne constituent pas une garantie de performance future.

## Parité backtest / paper

Le moteur de backtest et le runner partagent :

- les mêmes classes de stratégie ;
- le même noyau de décision déterministe (`btcquant.domain.decision`) pour
  l'avancement des positions, le funding, le resserrement des stops et les
  demandes d'entrée/sortie ;
- le même dimensionnement par risque et volatilité ;
- les mêmes frais et hypothèses de slippage ;
- le même principe de décision à la clôture d’une bougie ;
- une comptabilité du funding sur les positions perpétuelles ;
- des coupe-circuits de drawdown et de perte journalière.

Le noyau métier ne passe aucun ordre et n'effectue aucun I/O : il reçoit une
barre et un état de position, puis renvoie un nouvel état indépendant accompagné
d'événements typés. Le backtest exécute toujours les demandes à l'ouverture
suivante ; le runner les transmet à son broker dès la clôture observée. Cette
différence d'adaptateur est explicite et n'est plus mélangée aux règles de
stratégie.

### Simulateur d'exécution commun

Le backtest et `PaperBroker` utilisent également le même
`ExecutionSimulator`. Il centralise :

- frais et slippage défavorable ;
- limite de participation au volume et fills partiels ;
- impact de marché proportionnel à la participation ;
- rejets déterministes à partir de l'identifiant d'ordre et d'une seed ;
- prix retardé pour les scénarios de latence ;
- déclenchement conservateur des stops, gaps inclus ;
- rejeu idempotent d'un même ordre.

Sans configuration supplémentaire, le modèle reproduit exactement l'ancien
comportement (fill complet, sans rejet ni impact additionnel). Les scénarios de
stress optionnels se configurent sous `execution.simulation` :

```yaml
execution:
  mode: paper
  simulation:
    rejection_rate: 0.01
    max_volume_participation: 0.05
    market_impact_bps: 10.0
    min_qty: 0.0001
    seed: 42
```

`fee_rate` et `slippage_bps` restent exclusivement dans `costs`. Une latence
non nulle exige une vraie observation de prix retardée : le simulateur refuse
explicitement de l'inventer à partir d'une seule bougie.

### Écart funding — corrigé le 18 juillet 2026

Le backtest ne créait que la colonne `funding_rate` (somme des paiements par
barre, utilisée pour le P&L). `TrendLS` lisant une colonne distincte, `funding`,
pour filtrer les entrées extrêmes, ce filtre était **silencieusement inactif en
backtest** alors qu’il l’était en paper — les trois configs le déclarant pourtant
actif (`funding_long_max: 0.0008`).

`carry.add_funding_columns()` produit désormais les deux colonnes, qui n’ont ni
la même unité ni le même usage :

| Colonne | Contenu | Usage |
|---|---|---|
| `funding_rate` | somme des paiements tombant dans la barre | P&L |
| `funding` | dernier taux 8 h connu à la clôture | filtre d’entrée |

La distinction n’est pas cosmétique : les paiements tombent à 00/08/16 UTC, donc
**une barre 4 h sur deux reçoit un paiement nul**. Alimenter le filtre avec
`funding_rate` l’aurait rendu actif une barre sur deux et aurait sous-estimé le
taux d’un facteur deux sur les autres. La colonne `funding` est l’équivalent
backtest de `Venue.funding_rate_8h()` côté live.

Effet mesuré sur le profil x4 : 488 → 479 trades, Sharpe combiné 1,34 → 1,41,
drawdown maximal inchangé. `tests/test_funding_parity.py` fige la correction.

## État du projet

| Composant | État |
|---|---|
| Backtest trend | Implémenté |
| Paper trading trend x4 | Actif |
| Backtest carry | Funding, basis, emprunt variable, haircuts, marge et liquidation |
| Paper trading carry x3 | Actif |
| Rééquilibrage 60/40 | Implémenté |
| Dashboard et suivi des apports | Implémentés |
| Persistance | SQLite transactionnel, migration JSON/CSV automatique |
| Exécution trend testnet | Qualification PASS + confirmation explicite de session |
| Exécution carry testnet | Qualification PASS + confirmation explicite de session |
| Utilisation en argent réel | Non validée |

### Persistance et reprise

`state/btcquant.db` est la source de vérité unique. Elle journalise les
checkpoints des moteurs, positions, intentions et résultats d'ordres, événements,
trades, courbes d'équity et flux de capital. SQLite fonctionne en WAL avec
transactions `BEGIN IMMEDIATE`, contraintes d'intégrité et sauvegarde en ligne.
Chaque checkpoint est également inclus dans le journal avec un SHA-256
canonique ; `StateStore.replay_engine_state()` reconstruit l'état depuis les
événements et refuse un journal altéré.

Au premier démarrage, les anciens `*_state.json`, `equity_*.csv`, `trades.csv`
et `flows.csv` sont importés une seule fois puis conservés uniquement comme
sauvegarde froide.

Chaque ordre externe reçoit désormais un `client_order_id` stable, transmis à
l'exchange et relié à l'intention SQLite. Après un crash :

- ordre absent de l'exchange ou terminal sans fill : classement automatique
  `RECOVERED_ABORTED`, reprise autorisée ;
- timeout de recherche : ordre maintenu `PENDING`, démarrage interdit mais
  nouvelle tentative possible au prochain redémarrage ;
- ordre ouvert, partiel ou rempli sans checkpoint local certain : classement
  `UNBALANCED`, démarrage interdit jusqu'à réconciliation manuelle ;
- ordre paper interrompu : abandon automatique, car aucun effet externe ne
  survit au processus.

Le résultat d'ordre, le checkpoint des positions et le trade éventuel restent
validés dans une seule transaction. Un crash pendant cette transaction laisse
l'intégralité de l'opération en `PENDING`, jamais un demi-état.

### Observabilité d'exécution

SQLite conserve également le prix de référence précédant chaque ordre. Le
watchdog et le dashboard en dérivent, sur une fenêtre glissante :

- ratio réellement rempli ;
- taux de rejet et de fills partiels ;
- slippage moyen et percentile 95 ;
- ordres `PENDING` trop anciens ;
- ordres `UNBALANCED`.

Les anomalies ouvrent un incident persistant et dédupliqué dans la table
`incidents`. Une même anomalie n'envoie qu'une notification ; elle est résolue
automatiquement lorsque la condition disparaît et sera notifiée à nouveau si
elle réapparaît. Les incidents sont visibles dans `/api/operations`, dans le
cockpit du dashboard, le bilan quotidien et `scripts/inspect_state.py`.

### Qualification paper → testnet

Le passage au testnet est maintenant un contrôle bloquant, pas une décision
visuelle prise depuis le dashboard. Le protocole v1 impose notamment :

- 90 jours d'observation et 95 % de présence quotidienne des deux moteurs ;
- au moins 30 trades clôturés, 50 ordres terminaux et 5 par moteur ;
- aucun ordre non résolu ni incident ouvert ;
- au plus 2 % de rejets, 10 % de fills partiels et 20 bps de slippage p95 ;
- un drawdown supérieur à -45 %, des états moteurs frais et aucun kill-switch.

Les seuils sont copiés dans SQLite au démarrage : une campagne conserve donc
ses règles même si une future version du protocole change. Les rapports
`PASS`/`FAIL` sont eux aussi historisés.

```powershell
btcquant-readiness start
btcquant-readiness status
btcquant-readiness finalize
```

`finalize` refuse de terminer une campagne tant qu'un seul critère échoue.
`scripts/test_testnet.py` et les brokers externes exigent ensuite la preuve
d'une campagne `PASSED` récente, puis une confirmation limitée à la session :

```powershell
$env:BTCQUANT_ENABLE_TESTNET = "I_ACCEPT_TESTNET_ORDERS"
python scripts/test_testnet.py
```

Le script referme ses positions dans un bloc de nettoyage même si un contrôle
intermédiaire échoue. L'argent réel reste verrouillé inconditionnellement.

Diagnostic local :

```bash
python scripts/inspect_state.py
```

## Installation

Le déploiement VPS utilise des releases immuables, une bascule atomique et un
rollback. La procédure staging, restauration et exploitation est détaillée dans
[`docs/DEPLOYMENT_RUNBOOK.md`](docs/DEPLOYMENT_RUNBOOK.md). Elle doit être
exécutée intégralement avant toute mise à jour du VPS.

Prérequis : Python 3.11 ou version ultérieure (le VPS tourne 3.12). Sous 3.10,
les dépendances basculeraient sur la majeure précédente de pandas.

### Avec uv (recommandé)

`uv.lock` fige les versions exactes de toutes les dépendances, y compris
transitives : l'environnement obtenu est identique sur le poste local, en CI et
sur le VPS.

```bash
git clone https://github.com/Julianos87/btc-quant.git
cd btc-quant
uv sync            # crée .venv et installe les versions figées
uv run pytest -q   # vérification
```

Les groupes `exchange` et `dashboard` font partie de l'environnement standard.
Les dépendances graphiques et expérimentales sont isolées :

```bash
uv sync --group research
```

### Avec pip

`requirements.txt` est **généré** depuis `uv.lock` et contient des versions
épinglées. C'est la voie utilisée par `deploy/install.sh` sur le VPS.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt
pip install -e . --no-deps
```

### Modifier une dépendance

Ne jamais éditer `requirements.txt` à la main : il serait écrasé et ferait
installer des versions non testées. Passer par `pyproject.toml` :

```bash
uv add <paquet>          # ou éditer [project.dependencies] puis : uv lock
uv export --no-default-groups --group exchange --group dashboard \
  --no-emit-project -o requirements.txt
```

## Qualité

Les tests et le lint tournent automatiquement à chaque push et pull request
(voir `.github/workflows/tests.yml`), sur Python 3.11 et 3.12.

```bash
uv run pytest -q      # tests
uv run ruff check .   # lint
```

Un hook `pre-push` rejoue le lint et les tests localement et refuse le push si
l'un des deux échoue. Il est versionné dans `.githooks/`, mais git n'active pas
un dossier de hooks tout seul : après un nouveau clone, il faut rejouer

```bash
git config core.hooksPath .githooks
```

Pour outrepasser ponctuellement : `git push --no-verify`.

Sous Windows, si pytest échoue avec `PermissionError` sur le dossier temporaire
système, lui en donner un accessible : `uv run pytest -q --basetemp=.pytest-tmp`.

## Utilisation

Télécharger ou actualiser les données :

```bash
python scripts/download_data.py
```

Rejouer le profil trend actif :

```bash
python scripts/run_backtest.py --config config_4x.yaml
```

Lancer le paper runner trend actif :

```bash
btcquant-trend --config config_4x.yaml
```

Lancer le paper runner carry :

```bash
btcquant-carry --capital 4000 --leverage 3
```

Générer la référence annuelle du portefeuille :

```bash
python scripts/make_yearly_reference.py
```

Lancer le dashboard :

```bash
python -m dashboard.app
```

## Structure du dépôt

```text
config.yaml                  profil de recherche à x1
config_4x.yaml               profil trend actif en paper
src/btcquant/
  data.py                    données et cache
  indicators.py              indicateurs techniques
  risk.py                    dimensionnement et coupe-circuits
  carry.py                   modèle et backtest du carry
  domain/                    décisions et exécution métier déterministes
  strategies/                stratégies autorisées par le runtime
  backtest/                  moteur de backtest partagé
  execution/                 brokers et runners
  reporting/                 repository et calculs de reporting partagés
  research/                  walk-forward et stratégies expérimentales
research/configs/            profils historiques non déployables
scripts/                     commandes et maintenance
dashboard/                   serveur, HTML et assets statiques séparés
deploy/                      services et timers VPS
tests/                       tests automatisés
.github/                     CI, Dependabot et ownership
pyproject.toml               dépendances et outillage (source de vérité)
uv.lock                      versions figées
requirements.txt             export figé de uv.lock (généré)
sbom.cdx.json                inventaire CycloneDX des dépendances de production
```

## Limites

- Le profil trend x4 a connu un drawdown simulé supérieur à 50 %.
- Les résultats du carry dépendent d'une hypothèse de taux d'emprunt (10 %/an par défaut) qui n'est ni observée ni garantie, et qui se dégrade justement quand le funding est élevé.
- Le carry x3 exige un compte sur marge : il n'est pas réalisable avec le seul capital de la poche.
- Les coûts réels, les fills partiels et les risques de marge peuvent différer fortement de la simulation.
- Les composants live ne sont pas validés pour une utilisation en production.

## Avertissement

Ce projet est expérimental et destiné à la recherche. Il ne constitue pas un conseil financier. Le trading de cryptoactifs, de produits dérivés et de stratégies à effet de levier peut entraîner une perte importante ou totale du capital.
