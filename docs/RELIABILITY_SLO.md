# SLO et budgets d'erreur

Ces objectifs couvrent la campagne paper et le futur testnet. Ils ne valent
pas autorisation de trading réel.

| Indicateur | Objectif sur 30 jours | Budget d'erreur |
|---|---:|---:|
| Disponibilité de chaque moteur requis | 99,5 % | 3 h 36 min |
| Fraîcheur état trend | 99,5 % sous 10 min | 3 h 36 min cumulées |
| Fraîcheur état carry | 99,5 % sous 20 min | 3 h 36 min cumulées |
| Couverture journalière equity | ≥ 95 % | ≤ 5 % de jours incomplets |
| Ordres ambigus non résolus | 0 | aucun budget |
| Incidents `UNBALANCED` ouverts | 0 | aucun budget |
| Rejets d'ordres | ≤ 5 % | seuil readiness |
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
document fixe la politique humaine qui entoure ces seuils. Le protocole v2
mesure la disponibilité comme l'union des intervalles pendant lesquels le
dernier échantillon equity reste frais ; un simple point quotidien ne suffit
plus. La campagne standard exige `trend`. `carry` ne devient moteur requis
qu'après activation explicite d'une politique de qualification dédiée.

Le profil `testnet-p1` réutilise ces mêmes SLO sur 30 jours. Il exige deux
ordres terminaux journalisés par le smoke test Hyperliquid, mais aucun nombre
minimal de trades organiques : leur fréquence dépend du marché et ne doit pas
permettre de prolonger ou de raccourcir artificiellement la fenêtre
opérationnelle.


## Contrat d'observabilité Lot 6

Les endpoints ont deux contrats distincts :

- /healthz est une sonde PROCESS_LIVENESS. Elle ne lit ni SQLite, ni
  exchange, ni cache et ne signifie pas que le service est prêt à valoriser un
  portefeuille ;
- /readyz est la surface publique minimale de SERVICE_READINESS : elle ne
  retourne que ready/not_ready et ne revele ni portefeuille, ni positions, ni
  PnL, ni details strategiques. /api/operational-health, protege par
  authentification, expose les controles detailles. Les deux lisent les
  sources en mode SQLite read-only, appliquent le profil de composants
  configure et utilisent les codes explicites (FRESH, STALE, UNAVAILABLE,
  UNKNOWN) en interne.


Chaque observation réseau expose source, observed_at, received_at,
age_seconds et freshness. Le cache utilise une TTL monotone et une durée
maximale de fallback : une lecture périmée est STALE, puis devient
UNAVAILABLE ; elle ne peut jamais rester indéfiniment affichée comme live.
Les endpoints exposent également le skew temporel entre les sources et un
statut de valorisation MARK_TO_MARKET_ESTIMATE ou UNKNOWN. L'absence du prix
as-of rend la valorisation non confirmée.

Le dashboard et ses métriques ouvrent les bases trading et shadow en
lecture seule. Une erreur de lecture produit UNKNOWN/SOURCE_UNAVAILABLE et
ne résout jamais implicitement un incident. Le watchdog persiste un incident
watchdog_check_failed lorsqu'une vérification échoue ; il ne transforme pas
une erreur d'observation en état nominal.


### Semantique temporelle des sources

- le prix Hyperliquid utilise le timestamp natif de la bougie 1 minute CCXT ;
- les OHLCV 1h et 4h utilisent le timestamp natif d'ouverture de la bougie,
  jamais un champ nomme abusivement candle_close_timestamp ;
- le funding utilise le timestamp natif de l'evenement funding, et non l'heure
  de recuperation ;
- le taux FX Binance est display-only. Sans timestamp de marche fiable, sa
  valeur est UNAVAILABLE et ne peut pas devenir une observation fraiche.

Le source_skew_seconds est le maximum-minimum des timestamps UTC connus.
Il est FRESH jusqu'a 1 200 secondes, STALE au-dela et UNKNOWN si une source
est significativement dans le futur. Une horloge monotone gouverne
l'expiration TTL ; les timestamps operateur restent en UTC.

SERVICE_READINESS signifie que la DB locale est lisible, que les moteurs
requis sont frais et que leur EXECUTION_SAFETY_HEALTH est PASS. Un composant
optionnel reste visible dans le detail mais ne fait pas echouer la sonde.
CAMPAIGN_QUALIFICATION reste un protocole historique separe.
