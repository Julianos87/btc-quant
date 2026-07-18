# TANDEM — portefeuille systématique BTC trend + carry

> Deux moteurs complémentaires : le **trend** cherche à capter les tendances du marché ; le **carry** étudie une exposition delta-neutre au funding.

[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org/)
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
python scripts/run_live.py --config config_4x.yaml
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
python scripts/run_carry.py --capital 4000 --leverage 3
```

Le modèle paper simule une position spot longue et une position perpétuelle courte. Il entre lorsque le funding annualisé lissé dépasse 3 % et sort lorsqu’il devient négatif.

Le levier x3 du carry est actuellement un **modèle synthétique de recherche**. Avec seulement 4 000 USDT de capital, l’achat comptant d’un notionnel de 12 000 USDT exige un financement ou une architecture de marge qui n’est pas reproduite par le paper runner. Les résultats du carry à x3 ne doivent donc pas être présentés comme directement exécutables en l’état.

## Résultats historiques de référence

Le fichier `dashboard/yearly_reference.json`, généré le 13 juillet 2026, rejoue le profil paper 60/40 sur la période du 10 septembre 2019 au 10 juillet 2026 avec funding historique :

| Année | Portefeuille 60/40 | Trend | Carry | BTC |
|---|---:|---:|---:|---:|
| 2019, partielle | +28,0 % | +34,3 % | +4,8 % | -28,7 % |
| 2020 | +160,9 % | +180,6 % | +68,4 % | +302,0 % |
| 2021 | +17,0 % | +0,3 % | +147,5 % | +59,8 % |
| 2022 | +2,3 % | +0,2 % | +8,9 % | -64,2 % |
| 2023 | +179,9 % | +232,6 % | +26,6 % | +155,6 % |
| 2024 | +55,0 % | +56,8 % | +41,0 % | +121,3 % |
| 2025 | -17,7 % | -21,3 % | +12,9 % | -6,3 % |
| 2026, partielle | -7,8 % | -9,5 % | +1,7 % | -26,5 % |

La référence du moteur trend à x4 indique également :

| Indicateur trend | Valeur |
|---|---:|
| Trades | 488 |
| Trades par an | 65,7 |
| Win rate | 37,3 % |
| Perte moyenne | -3,10 % |
| Gain moyen | +9,59 % |
| Drawdown maximal historique | -53,09 % |
| Plus longue série de pertes | 21 trades |

Ces chiffres sont des résultats de simulation. Ils ne constituent pas une garantie de performance future.

## Parité backtest / paper

Le moteur de backtest et le runner partagent :

- les mêmes classes de stratégie ;
- le même dimensionnement par risque et volatilité ;
- les mêmes frais et hypothèses de slippage ;
- le même principe de décision à la clôture d’une bougie ;
- une comptabilité du funding sur les positions perpétuelles ;
- des coupe-circuits de drawdown et de perte journalière.

### Écart connu à corriger

Le backtest stocke les paiements historiques dans la colonne `funding_rate`, utilisée pour le P&L. La stratégie `TrendLS` lit cependant une colonne distincte, `funding`, pour filtrer les entrées extrêmes. Le runner paper renseigne cette seconde colonne avec le funding courant, contrairement au backtest principal.

La parité du **filtre d’entrée funding** n’est donc pas démontrée dans l’état actuel du code. Les résultats historiques ne doivent pas être décrits comme utilisant exactement ce filtre tant que les deux chemins n’ont pas été harmonisés et retestés.

## État du projet

| Composant | État |
|---|---|
| Backtest trend | Implémenté |
| Paper trading trend x4 | Actif |
| Backtest carry synthétique | Implémenté |
| Paper trading carry x3 | Actif |
| Rééquilibrage 60/40 | Implémenté |
| Dashboard et suivi des apports | Implémentés |
| Exécution trend testnet/live | Codée, non validée pour le profil actif |
| Exécution carry double-jambe | Expérimentale, non validée |
| Utilisation en argent réel | Non validée |

## Installation

Prérequis : Python 3.10 ou version ultérieure.

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

### Avec pip

`requirements.txt` est **généré** depuis `uv.lock` et contient des versions
épinglées. C'est la voie utilisée par `deploy/install.sh` sur le VPS.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt
```

### Modifier une dépendance

Ne jamais éditer `requirements.txt` à la main : il serait écrasé et ferait
installer des versions non testées. Passer par `pyproject.toml` :

```bash
uv add <paquet>          # ou éditer [project.dependencies] puis : uv lock
uv export --no-dev --no-hashes --no-emit-project -o requirements.txt
```

## Qualité

Les tests et le lint tournent automatiquement à chaque push et pull request
(voir `.github/workflows/tests.yml`), sur Python 3.10, 3.11 et 3.12.

```bash
uv run pytest -q      # tests
uv run ruff check .   # lint
```

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
python scripts/run_live.py --config config_4x.yaml
```

Lancer le paper runner carry :

```bash
python scripts/run_carry.py --capital 4000 --leverage 3
```

Générer la référence annuelle du portefeuille :

```bash
python scripts/make_yearly_reference.py
```

Lancer le dashboard :

```bash
python dashboard/app.py
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
  strategies/                stratégies trend
  backtest/                  moteur et walk-forward
  execution/                 brokers et runners
scripts/                     commandes et maintenance
dashboard/                   suivi du portefeuille
deploy/                      services et timers VPS
tests/                       tests automatisés
.github/workflows/           CI (tests + lint)
pyproject.toml               dépendances et outillage (source de vérité)
uv.lock                      versions figées
requirements.txt             export figé de uv.lock (généré)
```

## Limites

- Le profil trend x4 a connu un drawdown simulé supérieur à 50 %.
- La poche carry x3 suppose un financement que le modèle ne chiffre pas.
- Le filtre d’entrée lié au funding n’est pas encore identique entre backtest et paper.
- Les coûts réels, les fills partiels et les risques de marge peuvent différer fortement de la simulation.
- Les composants live ne sont pas validés pour une utilisation en production.

## Avertissement

Ce projet est expérimental et destiné à la recherche. Il ne constitue pas un conseil financier. Le trading de cryptoactifs, de produits dérivés et de stratégies à effet de levier peut entraîner une perte importante ou totale du capital.
