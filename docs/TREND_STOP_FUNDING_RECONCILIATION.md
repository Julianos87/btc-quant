# Funding Trend après stop — correction PAPER

Le chemin `stop_exchange` fermait la position et persistait le trade, mais ne
persistait pas la réduction d'exposition dans `position_timeline`. Le funding
utilise cette timeline, donc continuait à débiter/créditer la quantité précédente.

Le checkpoint atomique du stop contient désormais la quantité restante, y compris
zéro pour une fermeture totale. Une écriture échouée arrête le moteur pour
réconciliation ; elle ne peut pas laisser une boucle sauvegarder un état partiel.
Les tests reproduisent l'ancien défaut et couvrent arrêt total, partiel, reprise,
funding de la seule quantité restante, absence de double paiement et panne disque.

## Historique déjà affecté

`scripts/reconcile_trend_stop_funding.py` est un outil PAPER explicite. Sans
`--apply`, il lit uniquement et calcule un plan. Il reconstruit les transitions
manquantes à partir des checkpoints de stops observés et des trades corrélés,
valide leurs hashes, quantités et générations, puis recalcule les montants de
funding avec les prix/taux déjà enregistrés. Il refuse une exposition ambiguë.

Avec `--apply`, le moteur doit être arrêté : le verrou d'instance Trend l'impose.
Tous les slots doivent être FLAT. Le hash de l'état empêche un plan périmé d'écraser
un checkpoint récent. Chaque ajustement a une identité distincte et un checkpoint
atomique dans le ledger de funding existant. Les paiements d'origine ne sont pas
modifiés. Après crash, les ajustements restants sont recalculables ; un ajustement
déjà appliqué n'est pas payé une deuxième fois.

```bash
python scripts/reconcile_trend_stop_funding.py \
  --database /path/to/paper-copy.db --config environments/paper/config.yaml
# Après sauvegarde et preuve de fermeture des slots : même commande avec --apply.
```

La correction réconcilie le cash courant et ajoute la contre-écriture au ledger.
Les snapshots et courbes historiques restent des observations de l'ancien code ;
ils ne deviennent pas rétroactivement des résultats qualifiés. Le high-water mark
et les paramètres de risque ne sont pas réécrits. La campagne PAPER n°6 conserve
son historique et son binding précédemment invalide.
