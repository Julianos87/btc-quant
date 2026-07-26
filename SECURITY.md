# Politique de sécurité

## Versions supportées

Seule la branche `main` et la dernière release déployée sont supportées. Le
testnet et le live restent désactivés tant que la qualification décrite dans
`docs/PRODUCTION_RUNBOOK.md` n'est pas obtenue.

## Signaler une vulnérabilité

Ne pas ouvrir d'issue publique contenant un secret, une clé, une adresse
d'infrastructure ou une procédure d'exploitation. Utiliser en priorité un
GitHub Private Vulnerability Report. À défaut, contacter le propriétaire du
dépôt par un canal privé déjà vérifié.

Accusé de réception visé : 2 jours ouvrés. Qualification initiale : 5 jours.
Une vulnérabilité critique affectant l'exécution impose l'arrêt des moteurs
externes jusqu'à correction, rotation des secrets et vérification des journaux.

## Gouvernance des clés

- clés distinctes pour développement, testnet et production ;
- retraits interdits et permissions limitées au strict trading nécessaire ;
- liste blanche IP activée lorsque l'exchange le permet ;
- secrets stockés uniquement dans le fichier d'environnement protégé du VPS,
  jamais dans Git, les commandes, les URLs, les logs ou les sauvegardes ;
- rotation planifiée au minimum tous les 90 jours, et immédiatement après tout
  soupçon d'exposition, départ d'un intervenant ou compromission d'un poste ;
- après rotation : révocation de l'ancienne clé, redémarrage contrôlé,
  réconciliation fail-closed et vérification d'un appel en lecture seule ;
- toute activation de clé live requiert une revue humaine et la preuve de
  qualification testnet.

## Réponse à incident

1. désactiver les brokers externes et activer le kill switch si nécessaire ;
2. préserver SQLite, logs et release active sans les modifier ;
3. révoquer les clés concernées ;
4. rapprocher positions, ordres et soldes avec l'exchange ;
5. restaurer uniquement depuis une sauvegarde vérifiée ;
6. documenter la cause, l'impact, la chronologie et les actions préventives.
