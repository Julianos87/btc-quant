# Changelog

Toutes les modifications notables sont consignées ici. Le format suit
Keep a Changelog et les versions suivent Semantic Versioning.

## [Unreleased]

### Added

- `deploy/migrate.sh` exécute `btcquant.entrypoints.migrate` depuis le venv
  de la release cible (script-relative), pas depuis `/opt/btcquant/current` ;

- commande explicite `btcquant-carry-cutover` pour retirer un checkpoint Carry
  paper synthétique (`OPEN` qty=0) vers un état v6 `FLAT`, hors migration de
  schéma et hors démarrage du runner ;
- carte Testnet décisionnelle, regroupement Santé / Qualification / Exécution
  et libellés Carry paper synthétique sur l'architecture dashboard Lot6 ;
- journal SQLite transactionnel, migrations, reprise après crash et readiness ;
- services de funding, risque, ordres, stops et comptabilité de positions ;
- simulateur d'exécution partagé backtest/paper ;
- authentification dashboard signée, repository et analytics partagés ;
- déploiement atomique, rollback, préflight et sauvegardes chiffrées ;
- politiques de sécurité, contribution, ownership, Dependabot et SBOM SPDX.

### Changed

- réconciliation et erreurs SQLite rendues fail-closed ;
- carry à deux jambes géré comme une saga persistante ;
- frontend dashboard séparé en HTML, CSS et JavaScript ;
- outils expérimentaux déplacés sous `btcquant.research`.

### Security

- brokers externes verrouillés par défaut ;
- services systemd durcis et exécution non privilégiée ;
- jeton dashboard retiré des URLs et remplacé par une session signée.
