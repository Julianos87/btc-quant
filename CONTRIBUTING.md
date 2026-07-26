# Contribuer

## Règles obligatoires

- travailler sur une branche et passer par une pull request ;
- ne jamais committer de secret, d'état runtime, de données privées ou de
  sauvegarde ;
- conserver `pyproject.toml`, `uv.lock` et `requirements.txt` synchronisés ;
- exécuter `pytest`, Ruff, le formatage, Mypy, l'audit des dépendances et la
  vérification du SBOM avant fusion ;
- fournir un test de régression pour toute correction de comportement ;
- documenter toute migration SQLite et garantir sa compatibilité ascendante.

Les changements de stratégie, sizing, levier, frais, funding, kill switch,
réconciliation, ordre, stop, configuration live ou déploiement exigent une
revue humaine explicite du propriétaire. Une génération ou revue uniquement
par IA ne constitue jamais une approbation de risque.

## Définition de terminé

Le code est typé, testé, documenté à hauteur de son risque, observable,
réversible et compatible avec la Safety Baseline. « Le script fonctionne sur
mon poste » n'est pas une preuve suffisante pour le testnet ou la production.
