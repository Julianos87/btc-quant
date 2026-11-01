# Carry PAPER sans emprunt

Le moteur Carry canonique utilise `environments/paper/carry_cash.yaml` : budget
PAPER initial de 4 000 USDC, levier maximal de 1×, emprunt désactivé et plafond de dette nul. Le profil
historique ×3 n'est pas repris comme preuve pour cette simulation. Trend garde
son budget PAPER de 6 000 et ses règles dans `config.yaml`; cette livraison ne
modifie pas ses stratégies ni ses données.

Le compte de trading n'est pas requis : les deux services Carry PAPER n'ont
aucune clé et ne soumettent aucun ordre à l'exchange. Le service expérimental
possède un état séparé et ses observations ne sont pas ajoutées au portefeuille
canonique ou à Trend.

## Instruments et financement simultané

Hyperliquid/CCXT expose `BTC/USDC` comme marché spot `@142`, mais le token de
base est **UBTC / Unit Bitcoin**, token d'index 197, non canonique. Son
`tokenId` Hyperliquid est `0x8f254b963e8468305d409b33aa137c67` ; son adresse de
contrat EVM distincte est `0x9fdbda0a5e284c32744d2f17ee5c74b284993463`. Ces deux
identifiants ne sont pas interchangeables. L'ancienne configuration avait
utilisé l'adresse du contrat EVM comme `tokenId`, ce qui faisait échouer le
contrôle d'identité du marché et bloquait les décisions Carry. L'adaptateur lit
maintenant `tokenId` depuis les métadonnées de devise Hyperliquid et contrôle
l'adresse du contrat séparément. La jambe courte est le perp linéaire
`BTC/USDC:USDC`, marché `0`, réglé en USDC. UBTC n'est donc pas renommé en BTC :
sa parité, son basis et son chemin de rachat restent un risque distinct.
L'adaptateur contrôle identité, précision, taille minimale et horodatages de
chaque jambe avant une entrée. Les métadonnées observées indiquent un pas de
quantité de `0,00001` et un minimum de 10 USDC par jambe. La précision de prix
est relue au runtime.

Le budget disponible finance ensemble l'achat spot, la marge initiale du short,
les commissions et les coûts d'entrée ; le spot n'est pas réutilisé comme
collatéral perp. La simulation réserve 50 % du notionnel perp en marge initiale,
5 % comme seuil de maintenance, et laisse 2 % du budget disponible en cash.
Le filtre de réserve économique fixe est désactivé (`uncertainty_reserve_rate: 0.0`, soit 0 %). Ce paramètre est distinct des 2 % de cash libre imposés par `cash_utilization: 0.98`.
Aux prix égaux,
4 000 USDC donnent donc environ 2 610 USDC de notionnel spot, avant arrondi au
pas de quantité. Ce calcul est une règle PAPER, pas une capacité de marge
confirmée auprès d'un compte réel. Une baisse de l'équité réduit la taille.

## Modèle de coûts et qualité de marché

Les taux configurés sont 0,10 % pour le spot et 0,05 % pour le perp. Ce sont des
hypothèses PAPER prudentes et non un tarif personnel vérifié. Le taux d'emprunt
de 10 % reste visible dans le scénario, mais la dette nulle ne produit pas
d'intérêt. Les coûts de financement et la marge d'incertitude ne sont pas
comptabilisés comme des frais réalisés.

Le simulateur marche les carnets disponibles pour obtenir les fills ; spread et
profondeur se reflètent dans les prix d'exécution. La marge fixe de slippage de
5 bps sert au filtre économique et n'est pas débitée une seconde fois du PnL.
Les commissions sont comptées séparément par jambe et le funding est crédité ou
débité uniquement pendant la détention simulée de la jambe courte. Aucune
convergence future du basis n'est ajoutée comme gain attendu. L’ancienne réserve économique de 2 % était un filtre d’entrée, non un frais ni une perte comptable. Elle est à 0 % dans le profil actif ; le buffer de cash libre reste à 2 %.

Une entrée est bloquée si un carnet est périmé de plus de 15 secondes,
désynchronisé de plus de 5 secondes, incomplet, ou si l'identité ou la taille
minimale d'un instrument ne correspond pas à la configuration. Un carnet reçu
mais périmé ne doit pas empêcher la réduction d'une position PAPER existante ;
l'observation dégradée reste signalée. Si les données nécessaires à la
réconciliation sont absentes ou incohérentes, le moteur bloque les nouvelles
entrées et signale l'incertitude.

Le collecteur public indépendant lit les carnets `@142` et `BTC`, dix niveaux
au maximum, ainsi que le champ natif de funding BTC. Il conserve les heures
source et de réception en UTC, les payloads bornés et leurs hashes, les échecs,
les trous, doublons, âges et écarts d'horloge. Rétention : 60 jours ou au plus
86 400 cycles ; base plafonnée à 256 Mo. Aucune donnée manquante n'est
remplacée par un prix ou un funding synthétique.

## Services et gouvernance de recherche

La commande versionnée du service canonique `btcquant-carry.service` est :

```bash
/opt/btcquant/current/venv/bin/btcquant-carry --config /opt/btcquant/current/environments/paper/carry_cash.yaml
```

Elle décrit le lancement systemd ; ne pas démarrer une seconde instance sur le
même état. Pour consulter son état sans modifier le service :

```bash
systemctl status btcquant-carry.service --no-pager
```


- `btcquant-carry.service` reste le Carry canonique sous
  `/opt/btcquant/state/btcquant.db`.
- `btcquant-trend.service` garde son état et son allocation existants.
- `btcquant-carry-experimental.service` utilise le profil cash PAPER dans un
  état isolé sous `/opt/btcquant/carry-experiment/state/`.
- `btcquant-carry-data.service` collecte les données publiques dans
  `/opt/btcquant/carry-experiment/data/` et écrit ses manifestes sous
  `/opt/btcquant/carry-experiment/reports/`.

Le protocole versionné est `audit/carry_rework_protocol.json`. Sa gouvernance
est stockée séparément des bases de trading, en développement dans
`audit/carry_rework_governance.sqlite3` et sur le VPS dans
`/opt/btcquant/carry-experiment/research/governance.sqlite3`. Cette livraison
enregistre le protocole sans lancer de sélection. Les observations historiques
ont déjà été consultées, ne contiennent pas de profondeur de carnet et ne sont
pas un holdout aveugle. Un futur choix de réserve ou d'horizon doit satisfaire
les portes de recherche préenregistrées et un holdout futur intact.

## Diagnostic historique et verdict économique

Les calculs ci-dessous utilisent l’ancienne réserve économique de 2 %. Ils
sont conservés comme historique et ne décrivent pas le paramètre actif à 0 %.

Le diagnostic antérieur, `scripts/diagnose_carry_cash.py`, repose sur des
observations Hyperliquid de janvier à août 2026 : prix spot UBTC et perp BTC,
funding horaire, six fenêtres consécutives sans recouvrement. Les clôtures
horaires sont des proxys et non des prix d'exécution. Le scénario emprunt n'est
pas un historique de taux réellement accessibles. La fenêtre avait déjà été
analysée : ce diagnostic n'est ni un test aveugle ni une recherche de paramètres.

Dans cet échantillon, aucune des six décisions de début de période ne passait
le filtre d'entrée après coûts et protections ; le scénario ×3 était négatif
dans les six fenêtres et le scénario cash restait négatif dans quatre. Les
variations adverses historiques du basis à 30 jours atteignaient 1,03 %, mais
elles ne capturent pas l'erreur de prévision du funding, les coûts d'exécution,
les gaps, la parité UBTC/BTC ni la marge. Ces résultats ne prouvent ni la
rentabilité ni une perte certaine de futures opérations.

La vérification du calcul de départ a trouvé un oubli précis : l'ancien
diagnostic réutilisait le frais perp de 0,05 % pour la jambe spot, alors que le
profil déclare 0,10 %. Le rapport antérieur affichait environ 5,22 USDC de coût
par sens, +5,69 avant réserve et −46,49 après réserve. Avec le même notionnel
approximatif de 2 609,34 USDC, les taux distincts et 5 bps de coût statique par
jambe donnent environ 6,52 USDC par sens, +3,09 avant réserve et −49,10 après
la réserve de 52,19. Le funding annualisé requis pour couvrir coûts et réserve
est ainsi proche de 30,44 %, contre 29,2 % dans le calcul antérieur. Ce sont des
estimations sur hypothèses, pas un PnL réalisé. Le rapport corrigé est
`audit/carry_cash_diagnostic_fee_corrected_20261011.json`; le fichier antérieur
reste conservé comme trace de l'estimation avant correction.

Dans les six fenêtres historiques recalculées avec les frais spot/perp
asymétriques, quatre résultats hypothétiques restent négatifs avant réserve et
deux sont légèrement positifs. Après la réserve de 2 %, les six sont négatifs ;
aucune fenêtre ne satisfait le critère d'entrée. Ces fenêtres ont été consultées
et les prix horaires restent des proxys : elles n'établissent ni rentabilité ni
holdout aveugle.

Le fonctionnement technique du simulateur, son accounting de deux jambes, son
redémarrage et sa collecte de données sont testés séparément. Ils ne démontrent
pas la rentabilité. Tant que les minimums du protocole et le holdout futur ne
sont pas satisfaits, le verdict économique demeure `INSUFFICIENT_EVIDENCE` ; la modification du filtre économique à 0 % ne transforme pas ces anciennes
fenêtres en validation de rentabilité. Les exigences de preuve restent distinctes
du choix de configuration actif.

Sources primaires : [métadonnées spot](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot),
[pas et précision](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size),
[commissions](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees),
[funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding).
