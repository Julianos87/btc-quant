# TANDEM — portefeuille systématique BTC trend + carry

> Deux moteurs complémentaires : le **trend** cherche à capter les tendances haussières et baissières ; le **carry** vise à encaisser le funding avec une position delta-neutre.

[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Mode](https://img.shields.io/badge/mode-paper%20par%20d%C3%A9faut-orange)](#état-opérationnel)
![Status](https://img.shields.io/badge/statut-exp%C3%A9rimental-blue)

## Vue d’ensemble

TANDEM est un environnement de recherche et d’exécution systématique pour BTC comprenant :

- téléchargement et mise en cache de données OHLCV et de funding ;
- indicateurs et stratégies sans look-ahead ;
- moteur de backtest barre par barre avec frais et slippage ;
- validation walk-forward ;
- allocation et gestion du risque ;
- paper trading et connecteurs d’exécution Binance via CCXT ;
- dashboard local de suivi.

Le portefeuille cible associe **60 % de trend following** et **40 % de cash-and-carry**. Le dépôt technique s’appelle `btc-quant` et le package Python `btcquant`.

> Le mode défini par défaut dans `config.yaml` est `paper`. Les composants live sont expérimentaux et doivent être validés sur testnet avant toute utilisation réelle.

## Architecture de la stratégie

### 1. Trend following — 60 %

Le moteur trend actif est un ensemble long/short sur perpétuels, réparti entre trois canaux de Donchian :

| Sous-stratégie | Horizon | Allocation interne |
|---|---:|---:|
| `trend_ls_20` | 20 périodes | 33,33 % |
| `trend_ls_55` | 55 périodes | 33,33 % |
| `trend_ls_100` | 100 périodes | 33,34 % |

Les trois horizons utilisent les mêmes principes : filtre ADX, contrôle du funding, dimensionnement par risque et exécution sur la bougie suivante.

### 2. Cash-and-carry — 40 %

Le moteur carry associe :

- une position spot longue ;
- une position perpétuelle courte de même taille ;
- une entrée lorsque le funding lissé dépasse le seuil défini ;
- une sortie lorsque le régime de funding devient défavorable.

La décision prise au paiement `t` est appliquée au paiement `t+1`, afin d’éviter tout look-ahead. La modélisation intègre quatre exécutions par cycle complet, mais ne simule pas entièrement le risque de marge intraposition.

## Résultats historiques

### Ensemble trend long/short

Backtests 2019–juillet 2026, avec frais, slippage et funding inclus :

| Indicateur | Ensemble LS perp 4 h | Trend swing spot 4 h | Intraday breakout 1 h | Buy & hold |
|---|---:|---:|---:|---:|
| Sharpe | **1,28** | 1,25 | -0,11 | 0,96 |
| Drawdown maximal | **-7,3 %** | -5,7 % | -30,1 % | -77 % |
| Rendement total | **+73,6 %** | +56,4 % | -12,8 % | +1 795 % |
| Profit factor | 1,7–2,0 selon l’horizon | 2,31 | 0,96 | — |

Sur 2021–2026, les trois horizons trend sont positifs sans optimisation propre au jeu de données. Les longs produisent l’essentiel du P&L ; les shorts sont proches de l’équilibre sur l’ensemble de la période, mais jouent un rôle défensif dans les régimes baissiers.

### Validation walk-forward

| Stratégie | Sharpe OOS | Efficacité OOS/IS | Décision |
|---|---:|---:|---|
| `trend_swing` | 1,08 | 0,33 | Edge conservé mais affaibli hors échantillon |
| `intraday_breakout` | -0,27 | -0,60 | Désactivée par défaut |

Le breakout intraday reste présent comme base de recherche, mais n’est pas destiné à être tradé dans son état actuel.

### Portefeuille trend + carry

Simulation sur 6,8 ans :

| Portefeuille | CAGR | Sharpe | Drawdown maximal |
|---|---:|---:|---:|
| 100 % trend, levier 4× | +51,8 % | 1,15 | -53,1 % |
| **60 % trend 4× + 40 % carry 3×** | **+51,6 %** | **1,65** | **-33,4 %** |

La corrélation historique mesurée entre les deux moteurs est de **+0,01**. Dans cette simulation, l’ajout du carry maintient un rendement voisin tout en réduisant le drawdown. Ces résultats dépendent fortement des hypothèses de coûts, de levier et d’exécution.

### Validation multi-actifs

Les mêmes règles trend long/short ont également été testées sur ETH et SOL :

| Actif | Sharpe | Drawdown maximal | Décision de recherche |
|---|---:|---:|---|
| BTC | **1,41** | -15,6 % | Cœur du système |
| ETH | 0,93 | -12,3 % | Candidat ultérieur |
| SOL | 0,49 | -19,2 % | Écarté |

BTC + ETH réduit historiquement le drawdown de -15,6 % à -10,6 %, sans améliorer le Sharpe. L’ajout d’ETH reste donc différé jusqu’à la validation opérationnelle du portefeuille BTC.

## État opérationnel

| Composant | État |
|---|---|
| Backtest trend | Implémenté |
| Walk-forward | Implémenté |
| Paper trading trend | Implémenté |
| Paper trading carry | Implémenté |
| Dashboard | Implémenté |
| Exécution trend testnet/live | Codée, à valider sur testnet |
| Exécution carry double-jambe | Codée, non encore éprouvée |
| Utilisation en argent réel | Non validée |

## Installation

Prérequis : **Python 3.10 ou version ultérieure**.

```bash
git clone https://github.com/Julianos87/btc-quant.git
cd btc-quant

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
# source .venv/bin/activate

pip install -r requirements.txt
```

Les fichiers de données, rapports et états runtime sont produits localement par les scripts.

## Utilisation

### Télécharger ou actualiser les données

```bash
python scripts/download_data.py
```

### Lancer les backtests

```bash
python scripts/run_backtest.py
```

Les rapports et listes de trades sont écrits dans `reports/`.

### Lancer le walk-forward

```bash
python scripts/run_walkforward.py trend_swing
```

### Démarrer le paper trading trend

```bash
python scripts/run_live.py
```

Avec `execution.mode: paper`, aucun ordre réel n’est envoyé.

### Démarrer le paper trading carry

```bash
python scripts/run_carry.py
```

Options disponibles :

```bash
python scripts/run_carry.py --capital 4000 --leverage 3
```

### Lancer le dashboard

```bash
python dashboard/app.py
```

Interface locale : [http://localhost:8666](http://localhost:8666)

Le dashboard suit l’equity du portefeuille, les positions, les stops, les coupe-circuits, le funding et les événements des runners.

## Configuration

Les paramètres principaux se trouvent dans [config.yaml](config.yaml) :

```yaml
risk:
  initial_capital: 10000
  risk_per_trade: 0.0075
  max_drawdown_halt: 0.30
  daily_loss_limit: 0.03
  max_leverage: 1.0

execution:
  mode: paper
  testnet: true
```

Les clés API ne doivent jamais être inscrites dans le dépôt. Elles sont lues depuis les variables d’environnement :

```bash
BINANCE_API_KEY
BINANCE_API_SECRET
```

Toute modification du symbole, de l’unité de temps, du risque ou du levier nécessite un nouveau cycle de validation.

## Structure du dépôt

```text
config.yaml
src/btcquant/
  data.py                   données OHLCV CCXT et cache incrémental
  indicators.py             EMA, ATR, RSI, Donchian et utilitaires
  risk.py                   sizing, vol targeting et coupe-circuits
  carry.py                  logique et backtest cash-and-carry
  strategies/               stratégies partageant un contrat commun
  backtest/
    engine.py               moteur barre par barre
    walkforward.py          validation glissante hors échantillon
  execution/
    broker.py               courtier simulé
    ccxt_broker.py          exécution Binance spot/perp
    carry_broker.py         exécution carry double-jambe
    runner.py               boucle continue et état persistant
scripts/                    données, backtests, walk-forward et runners
dashboard/                  interface locale
tests/                      tests automatisés
```

## Passage éventuel vers le live

La séquence minimale prévue est :

1. exécuter le système en paper trading pendant une durée suffisante ;
2. comparer les décisions et résultats observés aux attentes du backtest ;
3. valider l’exécution sur Binance testnet ;
4. auditer les protections : tailles, stops, idempotence, reprise après panne et réconciliation ;
5. seulement après décision explicite, envisager un capital réel très limité.

Les clés API doivent interdire les retraits et être protégées par une liste blanche d’adresses IP.

## Limites

- Les performances historiques ne garantissent aucune performance future.
- Les résultats sont sensibles aux frais, au slippage, au funding et aux hypothèses de remplissage.
- Le slippage est modélisé de manière constante ; il peut être nettement supérieur lors de fortes turbulences.
- La simulation du carry ne reproduit pas complètement le risque de base et de marge.
- Les composants live n’ont pas encore été validés dans toutes les conditions de marché et de panne.
- Le runner doit rester disponible ; les protections dynamiques ne sont plus actualisées lorsqu’il est arrêté.
- L’effet de levier multiplie les gains comme les pertes et peut entraîner une perte rapide du capital.

## Sources de recherche

- [A Decade of Evidence of Trend Following in Cryptocurrencies](https://arxiv.org/pdf/2009.12155)
- [Grayscale — Managing Bitcoin’s Volatility with Momentum Signals](https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals)
- [Shen et al. — Bitcoin Intraday Time Series Momentum](https://onlinelibrary.wiley.com/doi/10.1111/fire.12290)
- [Wen et al. — Intraday Return Predictability in Cryptocurrency Markets](https://www.sciencedirect.com/science/article/abs/pii/S1062940822000833)
- [Logical Invest — Walk-forward testing](https://logical-invest.com/walk-forward-testing-avoid-curve-fitting-backtesting/)

## Avertissement

Ce projet est expérimental et destiné à la recherche. Il ne constitue pas un conseil financier. Le trading de cryptoactifs, de produits dérivés et de stratégies à effet de levier peut entraîner une perte importante ou totale du capital.
