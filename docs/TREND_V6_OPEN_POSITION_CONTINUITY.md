# Continuité Trend paper à la frontière v6

Ce document fixe le contrat opérationnel pour un déploiement paper v6
lorsque des positions Trend héritées sont encore **OPEN**. Il ne modifie
ni `ReadinessPolicy`, ni `PROTOCOL_VERSION`, ni les seuils de qualification.

## Protection PAPER vs EXCHANGE

`PaperBroker.supports_stop_orders = False`. La protection d'une position
paper n'est pas un stop exchange : c'est le `position.stop_price` persisté,
évalué à chaque cycle par `_check_soft_stops` **avant** `_process_due_bars`.

Le checkpoint Trend porte désormais `stop_protection_mode` :

- `SOFTWARE` lorsque `broker.supports_stop_orders` est faux ;
- `EXCHANGE` lorsqu'il est vrai.

Le mode est dérivé du broker réel, jamais d'un texte de configuration.

Un checkpoint héritage **sans** ce champ reste chargeable. Une position
OPEN dont le mode est absent ou inconnu est **fail-closed**
(`PROTECTION_MODE_UNKNOWN`) jusqu'à ce que le runner v6 persiste son mode.
Un état FLAT sans mode historique ne fabrique pas d'incident.

`SOFTWARE` n'est protégé que si le stop logiciel est numérique, fini,
strictement positif, avec `qty > 0` et une direction ±1, sans saga de
stop exchange. `EXCHANGE` conserve le contrat Lot1–7 : `stop_order_id`
confirmé, ou `previous_stop_id` pendant un remplacement.

L'incident `execution:trend:unprotected_position` reste **CRITICAL**.
Il ne doit plus s'ouvrir pour un paper SOFTWARE dont le stop logiciel
est valide.

## Frontière de campagne

Le comptage de qualification utilise `exit_ts >= campaign.started_at`.
Une position entrée sous `bc6b902` et sortie après le début d'une
nouvelle campagne v6 contaminerait cette campagne.

Politique obligatoire à la bascule paper v6 :

1. Annuler la campagne courante (#2 aujourd'hui) au moment du
   déploiement contrôlé — pas dans cette tâche.
2. **Ne pas** démarrer immédiatement la campagne formelle v6 si des
   positions Trend héritées sont encore OPEN.
3. Laisser ces positions vivre sous la sémantique v6 (stops logiciels)
   jusqu'à ce que **tous** les slots Trend soient FLAT. Leur cycle de
   vie est une preuve opérationnelle, pas une preuve de qualification
   de la nouvelle campagne.
4. Interdit : clôturer de force, effacer, réécrire les `entry_time`,
   fabriquer des sorties, marquer des trades « post-v6 », éditer les
   ordres/trades historiques, reset d'équity, suppression de campagne.
5. Une fois tous les slots Trend FLAT :
   - frontière de maintenance (Trend arrêté, unresolved = 0, pas de
     `reconciliation_required`) ;
   - démarrer une **nouvelle** campagne paper v6 (ne pas coder en dur
     l'id) ;
   - reprendre Trend.
6. La campagne formelle v6 part donc d'une frontière stratégie FLAT
   propre.

Les seuils de `ReadinessPolicy` ne changent pas ici.
