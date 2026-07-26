# SLO et budgets d'erreur

Ces objectifs couvrent la campagne paper et le futur testnet. Ils ne valent
pas autorisation de trading réel.

| Indicateur | Objectif sur 30 jours | Budget d'erreur |
|---|---:|---:|
| Disponibilité des deux moteurs | 99,5 % | 3 h 36 min |
| Fraîcheur état trend | 99,5 % sous 10 min | 3 h 36 min cumulées |
| Fraîcheur état carry | 99,5 % sous 20 min | 3 h 36 min cumulées |
| Couverture journalière equity | ≥ 95 % | ≤ 5 % de jours incomplets |
| Ordres ambigus non résolus | 0 | aucun budget |
| Incidents `UNBALANCED` ouverts | 0 | aucun budget |
| Rejets d'ordres | < 5 % | seuil readiness |
| Slippage p95 | ≤ 20 bps | seuil readiness |
| Perte de données SQLite validées | 0 | aucun budget |

## Politique

- un ordre ambigu, une position déséquilibrée, une corruption SQLite ou un
  désaccord de réconciliation consomme immédiatement tout le budget et bloque
  la promotion ;
- à 50 % du budget mensuel consommé, seules les corrections de fiabilité et de
  sécurité sont autorisées ;
- à 100 %, la campagne est invalidée, une cause racine est exigée et la fenêtre
  d'observation repart après correction ;
- les maintenances planifiées comptent dans le budget, sauf exercice de reprise
  explicitement documenté avant son début ;
- le propriétaire examine chaque semaine les incidents, la fraîcheur, les
  rejets, le slippage et les restaurations de sauvegarde.

La readiness SQLite constitue la mesure automatisée de qualification. Ce
document fixe la politique humaine qui entoure ces seuils.
