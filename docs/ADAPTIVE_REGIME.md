# Gouverneur de régime adaptatif

Statut : **profil équilibré activé dans le paper le 2 août 2026 à la demande
explicite de l'opérateur**. Aucun ordre réel n'est autorisé par cette activation.

Le gouverneur ne cherche pas à prédire le prochain mouvement. À chaque clôture
4 h, il ajuste seulement la taille des nouvelles entrées et des renforcements à
partir d'informations déjà observées :

- efficacité directionnelle du prix sur 30 barres ;
- force de tendance ADX ;
- volatilité réalisée comparée à sa médiane historique récente ;
- références calculées sur les 540 barres strictement antérieures ;
- lissage sur 12 barres pour éviter les changements brusques.

Le multiplicateur est borné dans `[minimum, 1.0]`. Il ne peut donc jamais
augmenter le risque au-dessus du profil actuel. Une donnée de régime manquante
utilise le minimum configuré, tandis que les stops, sorties et coupe-circuits
restent inchangés.

## Résultat de recherche du 2 août 2026

La sélection 2019-2024 préfère encore le profil sans gouverneur. Les variantes
adaptatives montrent néanmoins un compromis monotone et cohérent :

| Profil | CAGR complet | Sharpe | Drawdown | CAGR 2025+ |
|---|---:|---:|---:|---:|
| Désactivé | +59,1 % | 1,12 | -56,4 % | -28,0 % |
| Léger | +52,3 % | 1,12 | -54,7 % | -27,6 % |
| Équilibré | +47,3 % | 1,14 | -50,9 % | -20,3 % |
| Défensif | +38,7 % | 1,13 | -45,9 % | -16,3 % |

Ces chiffres utilisent un warmup commun et ne doivent pas être comparés
directement à la baseline canonique démarrant plus tôt. Ils servent uniquement
à comparer les quatre profils entre eux.

## Suivi après activation

1. Conserver les paramètres gelés pendant au moins six mois.
2. Comparer chaque mois ses décisions au témoin sans gouverneur.
3. Ne jamais augmenter `adaptive_max_multiplier` au-dessus de `1.0`.
4. Revenir au témoin uniquement par une modification versionnée et validée.
5. Réexaminer le compromis rendement/drawdown après plusieurs régimes de marché.

Artefact reproductible : `audit/btc_adaptive_regime_research.json`.
