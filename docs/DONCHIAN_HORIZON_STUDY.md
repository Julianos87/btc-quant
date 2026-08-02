# Étude des horizons Donchian D20 / D55 / D100

## Question

Les trois horizons du moteur Trend apportent-ils une vraie diversification, ou
répètent-ils le même pari ?

## Méthode

- sept variantes : chaque horizon seul, les trois paires et le trio actuel ;
- capital Trend total constant à 6 000, réparti entre les horizons présents ;
- mêmes frais, slippage, funding, filtres, pyramiding et régime adaptatif que le
  profil paper actuel ;
- classement effectué uniquement sur 2019–2024 ;
- période 2025+ consultée après le classement comme test scellé ;
- simulation de coûts dégradés pour le contrôle de résistance.

Le script reproductible est `scripts/research_btc_horizon_contribution.py` et
les résultats complets, avec empreintes des données et de la configuration,
sont dans `audit/btc_horizon_contribution_research.json`.

## Résultats principaux

| Variante | CAGR complet | Sharpe | Drawdown maximal | CAGR test 2025+ | Trades |
|---|---:|---:|---:|---:|---:|
| D20 | +55,6 % | 1,20 | -56,0 % | -22,2 % | 192 |
| D20 + D100 | +50,2 % | 1,18 | -51,7 % | -19,9 % | 319 |
| D20 + D55 | +49,1 % | 1,14 | -53,7 % | -22,0 % | 343 |
| D20 + D55 + D100 | +47,3 % | 1,14 | -50,9 % | -20,3 % | 470 |
| D100 | +43,2 % | 1,11 | -41,5 % | -15,3 % | 127 |
| D55 + D100 | +41,7 % | 1,07 | -44,6 % | -18,3 % | 278 |
| D55 | +40,0 % | 1,02 | -48,0 % | -21,4 % | 151 |

Ces chiffres sont des simulations, pas des rendements réalisés ni une promesse
de performance future.

## Redondance mesurée

Corrélation des rendements journaliers :

- D20 / D55 : 93,5 % ;
- D20 / D100 : 90,5 % ;
- D55 / D100 : 96,0 %.

Entrées exactement identiques, rapportées au composant ayant le moins
d'entrées :

- D20 / D55 : 74,2 % ;
- D20 / D100 : 70,1 % ;
- D55 / D100 : 85,8 %.

Le trio diversifie donc un peu le moment des entrées et le risque, mais beaucoup
moins que ne le suggèrent trois noms de stratégie distincts.

## Décision

1. **D20 seul est refusé** : son rendement historique supérieur est payé par un
   drawdown de -56,0 %, contre -50,9 % pour le trio, et son test 2025+ est pire.
2. **D20 + D100 est le challenger structurel** : cette paire a été la meilleure
   paire sur 2019–2024, puis a conservé un léger avantage sur le test 2025+.
   Son drawdown complet se dégrade de 0,8 point et reste à -53,3 % sous coûts de
   stress, sous la limite catastrophe de -60 %.
3. **Le trio paper reste inchangé** : le test scellé a maintenant été consulté.
   Utiliser immédiatement cette même période pour choisir puis proclamer la
   paire « validée » serait du sur-apprentissage.

La recommandation est donc `FORWARD_CHALLENGER_D20_D100` : observer D20 + D100
sur de nouvelles données, en parallèle et sans ordre supplémentaire. Une
modification des règles du moteur ou de la campagne paper n'est pas autorisée
par cette étude historique seule.
