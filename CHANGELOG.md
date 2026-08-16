# Changelog

Toutes les modifications notables sont consignées ici. Le format suit
Keep a Changelog et les versions suivent Semantic Versioning.

## [Unreleased]

### Added

- journal SQLite transactionnel, migrations, reprise après crash et readiness ;
- services de funding, risque, ordres, stops et comptabilité de positions ;
- simulateur d'exécution partagé backtest/paper ;
- authentification dashboard signée, repository et analytics partagés ;
- déploiement atomique, rollback, préflight et sauvegardes chiffrées ;
- politiques de sécurité, contribution, ownership, Dependabot et SBOM SPDX.

### Fixed

- carry paper : le dashboard affiche un notionnel synthétique au lieu d'une
  position BTC à quantité nulle ;
- `inspect_state` : « aucun ordre journalisé » à la place de « données insuffisantes » ;
- filtre funding : une venue muette bloque les nouvelles entrées, plus fail-open ;
- gunicorn : `HOME` writable pour supprimer l'erreur `/home/btcquant` ;
- carte Testnet du dashboard : verdict Santé / Qualification / Exécution,
  blockers en tête, fraîcheur en secondes ou minutes (plus `0.0 h < 0 h`) ;
- `Venue.last_price` : une bougie Hyperliquid 1m vide lève une erreur métier
  retriable au lieu d'un `IndexError` (incidents des 1, 8 et 15 août 2026) ;
- le carry paper journalise désormais chaque bascule ON/OFF dans `orders` ;
- compaction equity : 90 jours à pleine résolution, puis 1 point / 5 min
  (l'ancien point horaire faisait échouer l'uptime de qualification).

### Changed

- réconciliation et erreurs SQLite rendues fail-closed ;
- carry à deux jambes géré comme une saga persistante ;
- frontend dashboard séparé en HTML, CSS et JavaScript ;
- outils expérimentaux déplacés sous `btcquant.research`.

### Security

- brokers externes verrouillés par défaut ;
- services systemd durcis et exécution non privilégiée ;
- jeton dashboard retiré des URLs et remplacé par une session signée.
