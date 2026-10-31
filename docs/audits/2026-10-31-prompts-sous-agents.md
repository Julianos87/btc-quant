# Prompts des sous-agents — corrections de l'audit Quant/Tandem

## Contexte commun

Corriger les dix constats de l'audit du 31 octobre 2026, à partir du dashboard déployé `bd73b1be1645e5901968f7866f38b80dacc5bd81`. Branche d'intégration : `fix/tandem-audit-20261031`. Répertoire partagé : `/home/ubuntu/worktrees/tandem-audit-fixes`. Rapport initial : `/home/ubuntu/audits/quant-tandem-2026-10-31/REPORT.md`.

Lire les instructions du projet, PRODUCT.md et DESIGN.md. Préserver l'identité visuelle, les contrats utiles des API et le caractère strictement consultatif du dashboard. Aucun résultat financier ni affirmation de comparabilité ne doit être inventé. Aucun accès en écriture aux données de production, aucune opération de trading, aucun déploiement par un sous-agent. Ne modifier que les fichiers attribués. Coordonner les contrats partagés avant de coder. Ajouter des tests de régression qui reproduisent les défauts et communiquer les résultats exacts, les limites et les changements requis ailleurs.

## Sous-agent 1 — Interface, état et interactions

Tu possèdes `dashboard/static/dashboard.js`, `dashboard/static/dashboard_ux.js` et `tests/test_dashboard_audit_frontend.py`.

Corrige :

- H1 : séparer les filtres demandés des filtres appliqués ; après une erreur, ne pas présenter les anciens trades comme répondant à la nouvelle date. Afficher une explication et une reprise claire. Garder l'export cohérent.
- M1 : conserver la profondeur de pagination lors des actualisations ; remettre à zéro seulement quand le contexte de consultation le nécessite. Gérer les requêtes concurrentes.
- M5 : protéger lecture, analyse et écriture des préférences locales ; valider leur schéma et restaurer des valeurs sûres si elles sont invalides.
- M6 : détecter une session expirée dans le client réseau commun et proposer une reconnexion explicite.
- M4 : présenter NOT_STARTED comme « Campagne non démarrée », avec l'étape attendue, sans lancer de campagne.
- H2 côté interface : afficher la provenance historique et les limites de comparaison. Ne jamais conclure « normal » ou « mêmes réglages » sans preuve. Coordonner les métadonnées avec le sous-agent 2.

Le coordinateur fournit les hooks HTML `tr-status`, `tr-retry`, `session-expired` et leur style. Prévoir un comportement accessible, français/anglais cohérent, récupération après erreur, résultats vides, réponses tardives et préférences absentes. Tester les transitions d'état, pas seulement la présence de chaînes dans les sources.

## Sous-agent 2 — API, comparaison historique et cache

Tu possèdes `dashboard/app.py`, les nouveaux modules serveur nécessaires dans `dashboard/` et `tests/test_dashboard_audit_backend.py`.

Corrige :

- H2 côté serveur : exposer un contrat explicite de comparabilité et de provenance. Séparer drawdown portefeuille et référence Trend ; ne pas présenter une fréquence à seuil de 5 % comme celle du seuil exact de 7,3 %. Préserver les champs existants utiles et ajouter les métadonnées nécessaires. En l'absence de preuve, considérer la comparabilité non établie.
- M2 : partager la validation des dates entre API trades et CSV, utiliser une borne supérieure exclusive, retourner 400 pour les entrées invalides. Tester minuit, dates ISO, fuseaux et intervalles invalides.
- M3 : éviter qu'un chargement distant bloque une lecture déjà en cache ; limiter les sections critiques, conserver la sécurité des clients réseau et les règles de fraîcheur. Tester la concurrence de manière déterministe, sans requête externe.

Convenir du schéma JSON avec le sous-agent 1 avant les modifications. Ne modifier ni JS/HTML/CSS, ni dépôt de reporting.

## Sous-agent 3 — Provenance et sources de reporting

Tu possèdes `src/btcquant/reporting/repository.py` et `tests/test_reporting_repository_audit.py`.

Corrige M7 : une base SQLite canonique présente mais vide ne doit pas faire réapparaître un ancien CSV. Distinguer absence, vide valide et erreur. Préserver une compatibilité historique explicitement définie et exposer la provenance. Ne jamais masquer une erreur SQLite par un repli silencieux.

Inspecter les appels et migrations existants. Couvrir trades, equity, flows et états lorsque la même ambiguïté existe ; préserver leurs types et contrats. Tester base absente, base vide, base peuplée, CSV ancien, erreur et politique de repli désactivée. Signaler au coordinateur tout changement nécessaire dans un autre fichier.

## Coordinateur — Intégration et validation

Prendre en charge H3, HTML/CSS et accessibilité : contraste des onglets dans les deux thèmes, titre principal et repères de page. Intégrer les trois lots, vérifier les contrats, examiner les différences et exécuter les tests ciblés puis les contrôles requis.

Utiliser une copie locale isolée pour rejouer les défauts dans le navigateur : filtre en échec puis reprise, pagination après actualisation, préférences corrompues, expiration de session, campagne non démarrée, références historiques, trois vues, mobile/ordinateur et deux thèmes. Distinguer les pannes injectées des erreurs spontanées. Vérifier le CSV, les dates et la concurrence côté serveur.

Livrer une version cohérente, un bilan par constat, les commandes et résultats des vérifications, les limites et le statut précis de publication/déploiement. Ne pas déclarer corrigé un scénario qui n'a pas été vérifié.
