# btc-quant — système de trading systématique BTC (swing + intraday)

Système complet : données → indicateurs → stratégies → backtest sans look-ahead →
validation walk-forward → gestion du risque → exécution paper/live (ccxt).
Conçu pour BTC/USDT, extensible à ETH/SOL en changeant `symbol` dans `config.yaml`.

## Fondements de recherche

Le design découle d'une revue des preuves empiriques (voir sources en bas) :

- **Le trend following bat le buy-and-hold sur BTC en risque ajusté** — croisements
  de moyennes ~10–30 jours, breakouts de canal ; Sharpe ~1.6–1.7 dans plusieurs
  études 2012–2025.
- **Le momentum intraday existe** (les sessions à fort volume prédisent la suite
  de la journée ; effet « Monday Asia Open ») mais est fragile après frais.
- **La mean reversion pure (« acheter le creux ») est perdante sur BTC** — les
  tendances crypto sont trop persistantes. Le système n'en contient donc pas.
- **Le sizing par volatilité (ATR + vol targeting) améliore le Sharpe** et écrase
  les drawdowns.
- **Un backtest ne vaut rien sans** : exécution à l'open de la barre suivante,
  frais + slippage réalistes, et validation walk-forward (ratio OOS/IS).

## Résultats (backtests 2019 → juillet 2026, frais + slippage + funding inclus)

Stratégie active : **ensemble trend long-short** sur perpétuels, trois horizons
Donchian 20/55/100 (paramètres standards de la littérature, non optimisés sur
nos données) à ⅓ du capital chacun.

| | Ensemble LS (perp 4h) | trend_swing (spot 4h) | intraday_breakout (1h) | Buy & hold |
|---|---|---|---|---|
| Sharpe | **1.28** | 1.25 | -0.11 | 0.96 |
| Max drawdown | **-7.3 %** | -5.7 % | -30.1 % | -77 % |
| Rendement total | **+73.6 %** | +56.4 % | -12.8 % | +1795 % |
| Profit factor | 1.7–2.0 selon l'horizon | 2.31 | 0.96 | — |

Enseignements clés (2021–2026, entrées jamais autorisées avant 2021) :
- les trois horizons sont **tous positifs sans optimisation** (Sharpe 1.05 à
  1.38) → l'edge ne dépend pas d'un paramètre chanceux ;
- les **longs font l'essentiel du PnL** (+2 962 USDT sur 227 trades) ; les
  shorts sont ~neutres net (-361 USDT sur 212 trades, win rate 28 %) mais
  jouent leur rôle d'assurance : en 2026 (marché baissier) ils gagnent +332
  et compensent presque exactement les -344 des longs, là où la version
  long-only perdait -396 ;
- le CAGR reste bas car le risque est volontairement minime
  (0.75 %/trade) — le monter scale rendement ET drawdown proportionnellement.

- Walk-forward `trend_swing` : Sharpe out-of-sample **1.08**, max DD -4.4 % sur
  5.5 ans OOS, positif sur 7 plis sur 10. Efficacité OOS/IS 0.33 → l'edge est
  réel mais plus faible que ce que l'in-sample suggère. C'est normal et c'est
  exactement ce que le walk-forward sert à mesurer.
- Walk-forward `intraday_breakout` : Sharpe out-of-sample **-0.27**, efficacité
  -0.60 → **perdant après frais, désactivé par défaut** dans `config.yaml`.
  Gardé dans le code comme base d'itération (filtre de saisonnalité, ordres
  maker pour réduire les frais), pas pour trader tel quel.
- Le CAGR est bas parce que le risque est volontairement minuscule
  (0.75 %/trade, vol cible 40 %). Monter `risk_per_trade` augmente rendement
  ET drawdown proportionnellement — le Sharpe, lui, ne bouge pas.

## Portefeuille 60/40 (trend + carry)

Second moteur validé : **cash-and-carry** (long spot + short perp, encaisse le
funding — [carry.py](src/btcquant/carry.py)). Corrélation mesurée avec le
trend : **+0.01** (décorrélation totale). Sur 6.8 ans :

| Portefeuille | CAGR | Sharpe | Max DD |
|---|---|---|---|
| 100 % trend 4x | +51.8 % | 1.15 | -53.1 % |
| **60 % trend 4x + 40 % carry 3x** | **+51.6 %** | **1.65** | **-33.4 %** |

Même rendement, un tiers de drawdown en moins. Robustesse carry : les 18
paramétrages testés sont tous rentables (+23 à +40 %/an à 3x).
Paper : `python scripts/run_carry.py` (funding réels, exécution simulée).
Live carry : exécution double-jambe non implémentée — jalon suivant.

## Architecture

```
config.yaml                 paramètres (stratégies, risque, coûts, exécution)
src/btcquant/
  data.py                   OHLCV ccxt paginé + cache CSV incrémental
  indicators.py             EMA, ATR Wilder, RSI, Donchian (décalé anti look-ahead)…
  risk.py                   sizing (risque fixe/trade + vol target) + kill-switches
  strategies/               TrendSwing (4h), IntradayBreakout (1h) — contrat commun
  backtest/engine.py        moteur barre par barre : décision à la clôture,
                            exécution à l'open suivant, stops intrabar, frais/slippage
  backtest/walkforward.py   optimisation glissante + test out-of-sample
  execution/broker.py       PaperBroker (simulation)
  execution/ccxt_broker.py  Binance réel : testnet, retries, idempotence,
                            stops STOP_LOSS_LIMIT côté exchange
  execution/runner.py       boucle live : état persisté JSON, stops suiveurs,
                            kill-switch drawdown + limite de perte journalière
scripts/                    download_data, run_backtest, run_walkforward, run_live
```

## Dashboard

Suivi en direct du portefeuille 60/40 (paper) :

```bash
python dashboard/app.py        # puis http://localhost:8666
```

Équity en temps réel (courbes portefeuille/trend/carry), positions et stops de
chaque sous-système, statut des coupe-circuits, funding en direct, compte à
rebours du prochain paiement, journal des événements. Thème clair/sombre,
actualisation 30 s. Ne lit que les états réels des runners — aucune donnée
simulée côté interface.

## Utilisation

```bash
pip install -r requirements.txt
python scripts/download_data.py           # télécharge/actualise l'historique
python scripts/run_backtest.py            # backtest + rapports dans reports/
python scripts/run_walkforward.py trend_swing
python scripts/run_live.py                # PAPER TRADING (défaut)
```

Passage en réel — dans cet ordre, sans sauter d'étape :
1. Paper trading plusieurs semaines (`mode: paper`) et comparer aux attentes.
2. `mode: live` + `testnet: true` (sandbox Binance) — vérifie l'exécution réelle.
3. `testnet: false` avec un capital minime. Clés API dans les variables
   d'environnement `BINANCE_API_KEY` / `BINANCE_API_SECRET`, retrait désactivé
   sur la clé, IP whitelistée.

## Limites connues et avertissements

- **Rien ne garantit la performance future.** Le walk-forward réduit le risque
  d'illusion, il ne l'élimine pas. Ne jamais engager d'argent qu'on ne peut pas
  perdre.
- L'exécution futures (shorts, stops STOP_MARKET reduceOnly, levier 1x) est
  codée mais n'a pas encore tourné contre le testnet Binance : valider en
  sandbox avant tout capital réel.
- Slippage modélisé constant (5 bps) ; en conditions extrêmes il est pire.
- Le runner live doit tourner en continu (machine allumée ou VPS) ; les stops
  côté exchange protègent la position si le bot tombe, mais le stop suiveur
  ne remonte plus tant qu'il n'est pas relancé.

## Multi-actifs ETH/SOL — validé, chiffré, en attente

L'ensemble trend long-short a été validé à l'identique sur ETH et SOL
(mêmes règles, filtres, coûts) :

| Actif | Sharpe | Max DD | Verdict |
|---|---|---|---|
| BTC | **1.41** | -15.6 % | cœur du système |
| ETH | 0.93 | -12.3 % | **à ajouter** (bon edge) |
| SOL | 0.49 | -19.2 % | écarté (edge trop faible) |

Corrélation des *stratégies* : 0.47 BTC-ETH, ~0.30 avec SOL. Le multi-actifs
**réduit le drawdown** (BTC seul -15.6 % → BTC+ETH -10.6 %) mais **n'améliore
pas le Sharpe** (ETH est de moindre qualité que BTC) : c'est un amortisseur de
risque, pas un booster de rendement. Décision : ajouter ETH *après* validation
live du 60/40 BTC ; ne pas multiplier l'exposition non testée maintenant.

## Carry en réel (exécuteur double-jambe)

[carry_broker.py](src/btcquant/execution/carry_broker.py) : ouverture/fermeture
simultanée spot long + perp short, gestion de l'échec d'une jambe (défait la
jambe orpheline), réconciliation spot↔short, notifications. Branché dans le
runner (`python scripts/run_carry.py --live --testnet`). **Codé, non encore
exercé** — à valider via `scripts/test_testnet.py` (qui couvre désormais le
cycle carry complet) avant tout usage réel.

## Extensions prévues

- ETH/SOL : changer `symbol`, re-valider par walk-forward (paramètres propres).
- Momentum cross-sectionnel BTC/ETH/SOL (rotation vers le plus fort).
- Perpétuels : shorts en régime baissier, prise en compte du funding.
- Filtre de saisonnalité intraday (Monday Asia Open, chop du dimanche matin US).

## Sources principales

- [A Decade of Evidence of Trend Following in Cryptocurrencies (arXiv)](https://arxiv.org/pdf/2009.12155)
- [Grayscale — Managing Bitcoin's Volatility with Momentum Signals](https://research.grayscale.com/reports/the-trend-is-your-friend-managing-bitcoins-volatility-with-momentum-signals)
- [Concretum — Seasonality in Bitcoin Intraday Trend Trading](https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/)
- [Shen et al. — Bitcoin Intraday Time Series Momentum (Financial Review)](https://onlinelibrary.wiley.com/doi/10.1111/fire.12290)
- [Wen et al. — Intraday Return Predictability in Crypto: Momentum, Reversal, or Both](https://www.sciencedirect.com/science/article/abs/pii/S1062940822000833)
- [QuantifiedStrategies — Trend Following and Momentum on Bitcoin](https://www.quantifiedstrategies.com/trend-following-and-momentum-strategies-on-bitcoin/)
- [QuantPedia — The Seasonality of Bitcoin](https://quantpedia.com/the-seasonality-of-bitcoin/)
- [Quant Signals — RSI mean reversion fails on BTC (2 397 trades)](https://quant-signals.com/rsi-trading-strategy/)
- [Logical Invest — Walk-forward testing to avoid curve-fitting](https://logical-invest.com/walk-forward-testing-avoid-curve-fitting-backtesting/)
- [Gainium — Common Backtesting Mistakes](https://gainium.io/blog/common-backtesting-problems)
