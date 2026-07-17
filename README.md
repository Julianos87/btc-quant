# TANDEM — portefeuille systématique BTC trend + carry

> Deux moteurs complémentaires : le **trend** cherche à capter les tendances du marché ; le **carry** vise à exploiter le funding avec une exposition delta-neutre.

[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org/)
![Mode](https://img.shields.io/badge/mode-paper%20par%20d%C3%A9faut-orange)
![Status](https://img.shields.io/badge/statut-exp%C3%A9rimental-blue)

## Présentation

TANDEM est un environnement de recherche et de simulation consacré aux stratégies systématiques sur Bitcoin.

Le projet comprend :

- le téléchargement et la mise en cache de données OHLCV et de funding ;
- des indicateurs et stratégies conçus sans look-ahead ;
- un moteur de backtest barre par barre ;
- une validation walk-forward ;
- des outils de gestion du risque ;
- des runners de paper trading ;
- un tableau de bord local.

Le portefeuille étudié associe deux approches complémentaires :

- **60 % trend following** : ensemble long/short sur plusieurs horizons ;
- **40 % cash-and-carry** : position spot longue et position perpétuelle courte.

Le mode d’exécution défini par défaut est `paper`.

## Architecture de la stratégie

### Trend following

Le moteur trend actif répartit son allocation entre trois canaux de Donchian :

| Sous-stratégie | Horizon | Allocation interne |
|---|---:|---:|
| `trend_ls_20` | 20 périodes | 33,33 % |
| `trend_ls_55` | 55 périodes | 33,33 % |
| `trend_ls_100` | 100 périodes | 33,34 % |

Cette diversification vise à réduire la dépendance à un horizon unique.

### Cash-and-carry

Le moteur carry associe une position spot longue à une position perpétuelle courte de taille équivalente. Les décisions reposent sur le niveau lissé du funding et sont appliquées avec un décalage afin d’éviter tout biais d’anticipation.

## Résultats historiques

### Ensemble trend long/short

Backtests 2019–juillet 2026 avec frais, slippage et funding inclus :

| Indicateur | Ensemble trend LS | Trend swing spot | Intraday breakout | Buy & hold |
|---|---:|---:|---:|---:|
| Sharpe | **1,28** | 1,25 | -0,11 | 0,96 |
| Drawdown maximal | **-7,3 %** | -5,7 % | -30,1 % | -77 % |
| Rendement total | **+73,6 %** | +56,4 % | -12,8 % | +1 795 % |

La stratégie intraday breakout s’est révélée négative hors échantillon et reste désactivée par défaut.

### Portefeuille trend + carry

Simulation sur 6,8 ans :

| Portefeuille | CAGR | Sharpe | Drawdown maximal |
|---|---:|---:|---:|
| 100 % trend, levier 4× | +51,8 % | 1,15 | -53,1 % |
| **60 % trend 4× + 40 % carry 3×** | **+51,6 %** | **1,65** | **-33,4 %** |

Dans cette simulation, la combinaison des deux moteurs améliore le rendement ajusté du risque. Ces résultats dépendent des hypothèses de coûts, de levier et d’exécution.

## État du projet

| Composant | État |
|---|---|
| Backtest trend | Implémenté |
| Validation walk-forward | Implémentée |
| Paper trading trend | Implémenté |
| Paper trading carry | Implémenté |
| Dashboard | Implémenté |
| Exécution réelle | Non validée |

## Installation

Prérequis : **Python 3.10 ou version ultérieure**.

```bash
git clone https://github.com/Julianos87/btc-quant.git
cd btc-quant

python -m venv .venv
```

Activation de l’environnement :

```bash
# Windows
.venv\Scripts\activate

# Linux/macOS
source .venv/bin/activate
```

Installation des dépendances :

```bash
pip install -r requirements.txt
```

## Utilisation

### Données historiques

```bash
python scripts/download_data.py
```

### Backtests

```bash
python scripts/run_backtest.py
```

Les résultats sont écrits dans `reports/`.

### Validation walk-forward

```bash
python scripts/run_walkforward.py trend_swing
```

### Paper trading trend

```bash
python scripts/run_live.py
```

### Paper trading carry

```bash
python scripts/run_carry.py
```

### Dashboard

```bash
python dashboard/app.py
```

## Configuration

Les principaux paramètres se trouvent dans [config.yaml](config.yaml) :

- marché et symbole ;
- coûts et slippage ;
- capital et risque par trade ;
- stratégies activées ;
- mode d’exécution.

Toute modification importante des paramètres nécessite une nouvelle validation historique et prospective.

## Structure du dépôt

```text
config.yaml
src/btcquant/
  data.py                   données OHLCV et cache
  indicators.py             indicateurs techniques
  risk.py                   dimensionnement et coupe-circuits
  carry.py                  logique cash-and-carry
  strategies/               stratégies systématiques
  backtest/                 moteur et walk-forward
  execution/                brokers et runners
scripts/                    commandes principales
dashboard/                  interface locale
tests/                      tests automatisés
```

## Limites

- Les performances historiques ne garantissent aucune performance future.
- Les résultats dépendent des frais, du slippage, du funding et des hypothèses de remplissage.
- La simulation du carry ne reproduit pas intégralement les risques de base et de marge.
- Les composants d’exécution réelle n’ont pas été validés en conditions de production.
- L’effet de levier multiplie les gains comme les pertes.

## Avertissement

Ce projet est expérimental et destiné à la recherche. Il ne constitue pas un conseil financier. Le trading de cryptoactifs et de produits dérivés peut entraîner une perte importante ou totale du capital.
