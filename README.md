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
btcquant-trend --config environments/paper/config.yaml
```

Le capital initial de la poche est de 6 000 USDT. Il est réparti entre trois horizons :

| Sous-stratégie | Canal de Donchian | Allocation de la poche |
|---|---:|---:|
| `trend_ls_20` | 20 périodes | 33,33 % |
| `trend_ls_55` | 55 périodes | 33,33 % |
| `trend_ls_100` | 100 périodes | 33,34 % |

Chaque stratégie utilise également un régime EMA 50/200, un filtre ADX, un stop ATR et un dimensionnement par risque et volatilité.

Le profil `environments/dev/config.yaml` à x1 reste disponible comme profil de
recherche prudent, mais ce n’est pas celui lancé par le service VPS principal.

### Carry — 40 %

Le service `btcquant-carry` lance :

```bash
btcquant-carry --capital 4000 --leverage 3
```

Le modèle paper simule une position spot longue et une position perpétuelle courte. Il entre lorsque le funding annualisé lissé dépasse 3 % et sort lorsqu’il devient négatif.

Le dernier paiement comptabilisé est persisté avec l’equity. Après une
interruption, le runner pagine tout l’historique depuis ce checkpoint, même si
l’arrêt dépasse la fenêtre de lissage de 14 jours. Chaque paiement et son
nouveau checkpoint sont enregistrés atomiquement ; un crash pendant le
rattrapage reprend au premier paiement non validé, sans perte ni double crédit.

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
| 0 % (ancien modèle) | +39,4 % | 9,56 | -3,2 % |
| 5 % | +27,7 % | 7,11 | -3,3 % |
| **10 % (défaut)** | **+16,9 %** | **4,59** | **-9,9 %** |
| 15 % | +7,1 % | 2,03 | -32,0 % |
| 20 % | -1,9 % | -0,55 | -54,7 % |

À levier 1, la position est intégralement financée par le capital : aucun
emprunt, donc aucune dépendance à ce taux. C’est le seul profil réalisable sans
compte sur marge, et il présente le meilleur couple rendement/risque :

| Levier | CAGR | Sharpe | Max DD |
|---:|---:|---:|---:|
| **x1** | +11,7 % | **9,56** | **-1,1 %** |
| x2 | +14,3 % | 5,86 | -3,4 % |
| x3 (profil actif) | +16,9 % | 4,59 | -9,9 % |
| x4 | +19,6 % | 3,96 | -17,7 % |

Passer de x1 à x3 gagne 5 points de CAGR mais multiplie le drawdown par 9 et
divise le Sharpe par deux, tout en introduisant une dépendance à un taux
d’emprunt volatil. Le profil paper reste à x3 pour l’instant ; **ce choix mérite
d’être rediscuté avant tout passage en réel**.

## Résultats historiques de référence

Le fichier `dashboard/yearly_reference.json`, régénéré le 28 juillet 2026,
rejoue le profil paper 60/40 sur la période du 10 septembre 2019 au
26 juillet 2026 avec funding historique :

| Année | Portefeuille 60/40 | Trend | Carry | BTC |
|---|---:|---:|---:|---:|
| 2019, partielle | +33,4 % | +41,4 % | -1,1 % | -28,7 % |
| 2020 | +208,3 % | +235,3 % | +41,3 % | +302,0 % |
| 2021 | +3,0 % | -4,1 % | +107,2 % | +59,8 % |
| 2022 | +0,1 % | +1,4 % | -8,3 % | -64,2 % |
| 2023 | +244,6 % | +276,6 % | +3,7 % | +155,6 % |
| 2024 | +60,5 % | +62,0 % | +17,8 % | +121,3 % |
| 2025 | -24,8 % | -25,3 % | -6,4 % | -6,3 % |
| 2026, partielle | -18,6 % | -19,1 % | -3,3 % | -27,6 % |

La colonne carry intègre le coût de financement du levier x3 (voir plus bas).
Une fois ce coût chiffré, **le carry devient négatif 4 années sur 8** — dont
2025 et 2026, où les deux poches perdent simultanément. La corrélation annuelle
trend/carry reste faible (+0,06), mais la diversification protège moins que ce
que suggéraient les chiffres non financés.

La référence du moteur trend à x4 indique également :

| Indicateur trend | Valeur |
|---|---:|
| Trades | 477 |
| Trades par an | 63,8 |
| Win rate | 35,6 % |
| Perte moyenne | -3,27 % |
| Gain moyen | +9,28 % |
| Drawdown maximal historique | -55,7 % |
| Plus longue série de pertes | 21 trades |

Ces chiffres sont régénérés depuis les caches locaux et liés par hash au code,
à la configuration et aux données dans `audit/baseline_reference.json`.

Ces chiffres sont des résultats de simulation. Ils ne constituent pas une garantie de performance future.

## Parité backtest / paper

### Trend

Le moteur de backtest et le runner partagent :

- les mêmes classes de stratégie ;
- le même noyau de décision déterministe (`btcquant.domain.decision`) pour
  l'avancement des positions, le funding, le resserrement des stops et les
  demandes d'entrée/sortie ;
- le même dimensionnement par risque et volatilité ;
- les mêmes frais et hypothèses de slippage ;
- le même principe de décision à la clôture d’une bougie ;
- une comptabilité du funding sur les positions perpétuelles ;
- des coupe-circuits de drawdown et de perte journalière (`PortfolioRiskService`).

Le noyau métier ne passe aucun ordre et n'effectue aucun I/O : il reçoit une
barre et un état de position, puis renvoie un nouvel état indépendant accompagné
d'événements typés. Le backtest exécute toujours les demandes à l'ouverture
suivante ; le runner les transmet à son broker dès la clôture observée. Cette
différence d'adaptateur est explicite et n'est plus mélangée aux règles de
stratégie.

### Carry — parité partielle, à ne pas surestimer

Le backtest et le runner partagent désormais leur politique
(`carry.CarryPolicy`) et leur transition pure
(`domain.carry_decision.decide_carry_payment`). La décision observée au paiement
`t` détermine dans les deux cas l'exposition au paiement `t+1` : le backtest
décale explicitement l'état d'une période, tandis que le runner comptabilise le
paiement courant avant de décider.

Auparavant, les paramètres divergeaient (entrée 3 % contre 5 %, lissage 14 j
contre 7 j), si bien qu'aucune référence publiée ne décrivait le moteur
réellement exécuté.

Deux écarts de simulation subsistent et sont assumés :

| | `backtest_carry()` | `CarryRunner` |
|---|---|---|
| Basis spot/perp | modélisé quand les prix sont fournis | non modélisé |
| Marge, haircut, liquidation | modélisés | non modélisés |

Le paper carry ne peut donc pas perdre par divergence de basis ni être liquidé,
alors que le backtest le peut. Le noyau de décision est unifié, mais la courbe
paper ne qualifie toujours pas les risques de marché et de marge d'une
exécution réelle.

Le carry dispose en revanche désormais de ses propres coupe-circuits — arrêt à
-25 % de drawdown, blocage des entrées à -3 % sur la journée UTC — appliqués par
le même `PortfolioRiskService` que le trend.

Le capital total, l'allocation trend/carry, le levier et les seuils carry sont
déclarés une seule fois dans `environments/paper/config.yaml` (`portfolio:` et `carry:`).
Runner, dashboard, digest, rééquilibrage et références utilisent les mêmes
objets de configuration immuables ; le chargement refuse toute divergence avec
`risk.initial_capital`.

### Simulateur d'exécution commun

Le backtest et `PaperBroker` utilisent également le même
`ExecutionSimulator`. Il centralise :

- frais et slippage défavorable ;
- limite de participation au volume et fills partiels ;
- impact de marché proportionnel à la participation ;
- prime de spread proportionnelle à la volatilité annualisée, avec plafond ;
- rejets déterministes à partir de l'identifiant d'ordre et d'une seed ;
- prix retardé pour les scénarios de latence ;
- déclenchement conservateur des stops, gaps inclus ;
- rejeu idempotent d'un même ordre.

Sans configuration supplémentaire, le modèle conserve le comportement
historique. Les configurations livrées activent un profil `normal` et gardent
un profil `stress` sélectionnable sans modifier le code :

```yaml
execution:
  mode: paper
  simulation:
    profile: normal
    profiles:
      normal:
        max_volume_participation: 0.05
        market_impact_bps: 15.0
        volatility_impact_bps: 1.5
        volatility_reference_annual: 0.40
        volatility_multiplier_cap: 3.0
      stress:
        rejection_rate: 0.01
        max_volume_participation: 0.01
        market_impact_bps: 75.0
        volatility_impact_bps: 6.0
        volatility_reference_annual: 0.40
        volatility_multiplier_cap: 5.0
```

`fee_rate` et `slippage_bps` restent exclusivement dans `costs`. Une latence
non nulle exige une vraie observation de prix retardée : le simulateur refuse
explicitement de l'inventer à partir d'une seule bougie.

Le coût défavorable simulé, en points de base, vaut :
`slippage_bps + prime_volatilité bornée + market_impact_bps × participation`.
La volatilité utilisée est celle déjà observée à la clôture de décision ; une
sortie intrabar utilise la dernière volatilité connue et n’introduit donc aucun
look-ahead.

Les deux scénarios se comparent sans éditer le YAML :

```bash
python scripts/run_backtest.py --config environments/paper/config.yaml --no-refresh \
  --execution-profile stress --no-reports
```

La chronologie d'exécution est identique entre les moteurs Trend : le signal
est calculé sur la clôture de la barre `t`, puis le backtest exécute à
l'ouverture `t+1` tandis que le runner paper utilise le prix de marché observé
au premier tick de `t+1`. Le runner n'utilise plus la clôture déjà passée comme
prix de fill. Les gaps sont ainsi imputés aux deux chemins et le slippage paper
est mesuré contre une référence réellement observable au moment de l'ordre.

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

Effet mesuré à l'époque sur le profil x4 à coûts fixes : 488 → 479 trades,
drawdown maximal inchangé.
`tests/test_funding_parity.py` fige la correction.

Tous les Sharpe, Sortino et volatilités publiés utilisent désormais une seule
convention : rendements de clôture journaliers, 365 périodes par an et
écart-type d'échantillon (`ddof=1`). Cette convention commune rend directement
comparables le backtest, le carry et le reporting live, indépendamment du
timeframe d'exécution.

Référence reproductible du profil x4 : **Sharpe 1,18**, 477 trades et drawdown
maximal de -55,7 %. Elle intègre la convention journalière et le profil
d'exécution normal dépendant de la volatilité et du volume.

Chaque horizon peut désormais ajouter une seule tranche égale à 30 % de sa
quantité initiale après une progression favorable de 0,5 ATR. Le renfort est
borné par le plafond de levier global, exécuté à la barre suivante avec frais
et slippage, et son prix est intégré au prix d'entrée moyen.

> Ces chiffres, comme tous ceux cités ci-dessus, sont vérifiés en CI contre
> `audit/baseline_reference.json` par `scripts/check_reference_provenance.py`.
> Le dépôt a annoncé trois Sharpe différents pour ce même profil jusqu'au
> 27 juillet 2026 ; le contrôle existe pour que cela ne se reproduise pas.

### Validation walk-forward de recherche

La sélection glissante `trend_ls` peut être rejouée sans réseau et publier un
artefact de provenance :

```bash
python scripts/run_walkforward.py trend_ls --config environments/paper/config.yaml --no-refresh \
  --output audit/walkforward_trend_ls_reference.json
```

Cette expérience mesure la stabilité hors échantillon d'une sélection
d'horizon Donchian et de multiple ATR. Elle **ne valide pas** à elle seule
l'ensemble fixe 20/55/100 déployé : cette limite méthodologique est inscrite
dans l'artefact et contrôlée par `check_reference_provenance.py`.

La validation multi-actifs gelée est reproductible avec
`python scripts/multiasset_experiments.py`. Sur la fenêtre commune
BTC/ETH/SOL, elle réduit le drawdown mais n'améliore pas le Sharpe
(ΔSharpe -0,14 ; p≈0,77) : la diversification n'est donc pas adoptée dans le
profil paper. Les résultats et les hashes des trois caches sont conservés dans
`audit/multiasset_reference.json` et vérifiés en CI.

La recherche BTC-only orientée rendement se rejoue avec
`python scripts/research_btc_return.py`. Parmi 108 candidats, la sélection
effectuée sans consulter 2025+ retient Donchian 10/20/40, pondéré
50/30/20, avec un stop à 3,5 ATR. Son historique complet est meilleur
(CAGR 65,1 %, Sharpe 1,29), mais il échoue au test scellé 2025+ et porte le
drawdown à -55,7 %. Il reste donc un candidat de recherche et ne remplace pas
le profil paper. Le protocole et le résultat sont figés dans
`audit/btc_return_research.json`.

Premier chantier d'amélioration isolé : un filtre impose une cassure minimale
de 0 à 60 bps au-delà du canal avant de payer les coûts d'entrée. Le seuil de
30 bps, sélectionné sans consulter 2025+, réduit les trades de 478 à 442 et le
drawdown de -55,3 % à -53,4 %, mais dégrade le CAGR à 49,3 %, le Sharpe à 1,12
et la période scellée 2025+ à -32,7 %/an. Il est donc rejeté et reste désactivé
(`entry_buffer_bps: 0`). L'expérience est reproductible avec
`scripts/research_btc_cost_filter.py` et figée dans
`audit/btc_cost_filter_research.json`.

Les autres pistes BTC ont été testées isolément avant combinaison :

| Piste | Résultat | Décision |
|---|---|---|
| Cible de volatilité 0,8–2,0 | non contraignante à partir de 1,0 | rejet |
| Réduction des shorts | la taille symétrique 1,0 reste sélectionnée | rejet |
| Funding continu | Sharpe 1,24, DD -54,5 %, CAGR 55,5 % | brique candidate |
| Retour à la moyenne | la sélection retient une poche de 0 % | rejet |
| Slippage divisé par deux | CAGR 60,6 %, Sharpe 1,24 | à prouver en shadow |

La combinaison la plus prometteuse sous coûts réduits utilise Donchian
10/20/40 pondéré 50/30/20, stop 3,5 ATR et sizing funding désactivé. Avec un
slippage de base réellement ramené de 5 à 2,5 bps, elle obtient 67,6 % de CAGR,
un Sharpe de 1,32 et un drawdown de -54,6 % ; en stress : 61,4 %, 1,24 et
-56,6 %. Sous les coûts actuels, elle échoue encore au portail d'adoption.
`audit/btc_combined_research.json` sépare explicitement résultat immédiatement
déployable et scénarios d'exécution hypothétiques.

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
| Exécution trend testnet | Hyperliquid uniquement, portail P1 et service VPS opt-in |
| Exécution carry testnet | Désactivée |
| Utilisation en argent réel | Non validée |

### Persistance et reprise

`state/btcquant.db` est la source de vérité unique. Elle journalise les
checkpoints des moteurs, positions, intentions et résultats d'ordres, événements,
trades, courbes d'équity et flux de capital. SQLite fonctionne en WAL avec
transactions `BEGIN IMMEDIATE`, contraintes d'intégrité et sauvegarde en ligne.
Chaque checkpoint est également inclus dans le journal avec un SHA-256
canonique ; `StateStore.replay_engine_state()` reconstruit l'état depuis les
événements et refuse un journal altéré.

Les payloads `trend` et `carry` suivent des `TypedDict` partagés entre runners
et persistance. Leur structure critique est validée au rechargement : un état
incomplet ou d'un type incompatible bloque la reprise au lieu de propager une
valeur ambiguë vers le health check ou le dashboard.

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
- ordres `UNBALANCED` ;
- position sans stop confirmé, transition de stop pendante ou réconciliation
  manuelle exigée.

Les anomalies ouvrent un incident persistant et dédupliqué dans la table
`incidents`. Une même anomalie n'envoie qu'une notification ; elle est résolue
automatiquement lorsque la condition disparaît et sera notifiée à nouveau si
elle réapparaît. Les incidents sont visibles dans `/api/operations`, dans le
cockpit du dashboard, le bilan quotidien et `scripts/inspect_state.py`.

### Qualification paper → testnet

Le passage au testnet est maintenant un contrôle bloquant, pas une décision
visuelle prise depuis le dashboard. Le protocole v2 impose notamment :

- 90 jours d'observation, 99,5 % de disponibilité temporelle et 95 % de jours
  dont les échantillons couvrent au moins 95 % du temps ;
- au moins 30 trades clôturés, 50 ordres terminaux et 5 par moteur requis ;
- aucun ordre non résolu ni incident ouvert ;
- au plus 5 % de rejets, 10 % de fills partiels et 20 bps de slippage p95 ;
- un drawdown intrajournalier supérieur à -45 %, des états moteurs frais
  (trend 10 min, carry 20 min) et aucun kill-switch.

La campagne standard qualifie uniquement `trend`, seul moteur autorisable en
testnet. Le carry devra être ajouté explicitement à `required_engines` lorsqu'il
disposera d'un chemin d'exécution qualifiable ; il ne peut plus bloquer ou
valider artificiellement la promotion trend.

Les seuils sont copiés dans SQLite au démarrage : une campagne conserve donc
ses règles même si une future version du protocole change. Les rapports
`PASS`/`FAIL` sont eux aussi historisés.

```powershell
btcquant-readiness start
btcquant-readiness status
btcquant-readiness finalize
```

### Campagne maker shadow mainnet

Le testnet ne reproduisant pas fidèlement la liquidité ni la file d'attente du
mainnet, `btcquant-shadow` observe le carnet public Hyperliquid mainnet sans clé
API et sans aucune primitive d'ordre. Chaque minute, il place virtuellement une
cotation post-only au meilleur bid et au meilleur ask, puis mesure pendant
30 secondes :

- le market-through, qui reste un proxy conservateur de fill et non une preuve
  de priorité dans la file ;
- le fallback taker, le coût tout compris et le markout ;
- la durée de la campagne et le nombre d'intentions observées.

Les données sont isolées du track record paper dans
`state/execution-shadow.db`. Le service VPS démarre automatiquement avec une
release compatible. Son état est disponible via `/api/execution-shadow`,
`/metrics/prometheus` ou :

```bash
btcquant-shadow --database state/execution-shadow.db status
```

Une éventuelle qualification n'est évaluée qu'après au moins 30 jours et
50 intentions. Même en cas de `passed: true`, elle valide seulement le proxy
d'exécution : elle ne transforme jamais un market-through en fill réel et
n'autorise pas le trading mainnet.

`finalize` refuse de terminer une campagne tant qu'un seul critère échoue.
`scripts/test_testnet.py` et le broker Hyperliquid exigent ensuite la preuve
d'une campagne paper `PASSED` récente, puis une confirmation explicite :

```powershell
$env:BTCQUANT_ENABLE_TESTNET = "I_ACCEPT_TESTNET_ORDERS"
python scripts/test_testnet.py
```

Le script utilise exclusivement `api.hyperliquid-testnet.xyz`, valide un ordre
IOC, un stop-market `reduceOnly`, le lookup par `cloid`, puis referme la position
dans un bloc de nettoyage. Il journalise les deux ordres terminaux dans
`state/btcquant-testnet.db`.

Sur le VPS, le portail démarre ensuite une campagne P1 distincte de 30 jours :

```bash
sudo bash /opt/btcquant/current/deploy/start-hyperliquid-testnet.sh \
  --i-accept-hyperliquid-testnet-orders
```

Le service est bloqué sans le fichier d'approbation créé par ce portail. Le
paper et le testnet sont incompatibles au niveau systemd, le watchdog testnet
s'exécute toutes les deux minutes, et l'arrêt d'urgence retire l'autorisation :

```bash
sudo bash /opt/btcquant/current/deploy/stop-hyperliquid-testnet.sh
```

Les secrets attendus sont `HYPERLIQUID_WALLET_ADDRESS` (adresse publique du
compte) et `HYPERLIQUID_PRIVATE_KEY` (clé privée d'un **API wallet testnet
dédié**, jamais celle du portefeuille principal). Les alertes Telegram sont
obligatoires pour le portail VPS. L'argent réel reste verrouillé
inconditionnellement.

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
  --no-header --no-emit-project -o requirements.txt
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
python scripts/run_backtest.py --config environments/paper/config.yaml
```

Lancer le paper runner trend actif :

```bash
btcquant-trend --config environments/paper/config.yaml
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
environments/
  dev/config.yaml            profil local de recherche à x1
  paper/config.yaml          profil actif paper, état btcquant.db
  testnet/config.yaml        profil P1 Hyperliquid, état btcquant-testnet.db
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
