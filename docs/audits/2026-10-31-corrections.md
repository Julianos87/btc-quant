# Quant / Tandem — corrections de l'audit du 31 octobre 2026

## Livraison

Branche `fix/tandem-audit-20261031`, construite sur le dashboard audité `bd73b1be1645e5901968f7866f38b80dacc5bd81`. Corrections intégrées localement ; aucun déploiement, changement de configuration de trading, migration de données de production ou envoi de notification effectué.

Les prompts et périmètres des trois sous-agents sont dans [2026-10-31-prompts-sous-agents.md](2026-10-31-prompts-sous-agents.md).

## État des dix constats

| Constat | Correction | Vérification |
|---|---|---|
| H1 — filtre en échec trompeur | Critères appliqués conservés atomiquement, période précédente indiquée, reprise explicite ; CSV associé au dernier filtre réussi | Panne 503 injectée, reprise et résultat vide dans le navigateur ; test comportemental |
| H2 — références historiques non équivalentes | Contrat `comparison` explicite, comparaison non certifiée signalée, fréquence de drawdown non équivalente supprimée, profil/date de référence conservés ; retrait des leviers non vérifiés des légendes courantes | Tests API et rendu ; référence réelle capturée, navigateur |
| H3 — contraste des onglets | Texte utilisant la couleur accessible du contrôle, états actifs lisibles ; titre principal et repères de page corrigés | Axe et inspection visuelle ordinateur/mobile |
| M1 — pagination annulée | Profondeur chargée conservée lors de l'actualisation, remplacement atomique du tableau ; fenêtres dépassant 500 lignes récupérées par plusieurs requêtes | Navigateur : 16 lignes après polling ; test de fenêtre de 650 lignes |
| M2 — dates API/CSV | Parseur commun, dates UTC et instants normalisés, borne de fin exclusive, erreur 400 pour entrées invalides | Minuit, fuseaux, intervalle inversé, historique vide et CSV testés |
| M3 — verrou global du cache | Lecture chaude indépendante des récupérations réseau ; déduplication par clé, verrous des clients distincts et fraîcheur bornée | Tests concurrents déterministes, sans réseau |
| M4 — campagne non démarrée | NOT_STARTED expliqué explicitement avec action attendue et blocage visible | Test d'état et navigateur |
| M5 — préférences invalides | Lecture/écriture protégées, validation du schéma, valeurs de repli | JSON corrompu dans le navigateur ; stockage interdit et mauvais types dans les tests |
| M6 — session expirée | Détection de redirection/401, lien de reconnexion, arrêt des appels répétés | Redirection simulée vers une page de connexion dans le navigateur |
| M7 — ancien CSV réapparu | SQLite présent fait autorité, même vide ; legacy seulement si base absente et autorisé, source exposée ; erreurs non masquées | Base vide/absente/corrompue, migration explicite et cache testés |

## Ajustements connexes

- Bilan automatique : données moteur absentes donnent « bilan indisponible », sans transformer l'absence en valeur zéro ; vraie equity zéro conservée. Notifications simulées dans les tests uniquement.
- Tableau des contributions : région nommée accessible au clavier sur mobile.
- Thème : état initial explicitement appliqué et préférence invalide ignorée ; le premier clic correspond désormais à la palette affichée.
- Période sans trade : message spécifique à la période, sans affirmer que le portefeuille n'a jamais clôturé de position.

## Tests

Suite complète Python avec couverture de branches : **2 392 réussites, 3 tests ignorés, 1 échec d'installation de l'environnement temporaire** au premier passage. Le test d'installation a ensuite **réussi avec l'accès réseau**. Couverture globale : **80,93 %**, seuil requis 80 % atteint. La suite complète représente donc 2 393 cas réussis après cette reprise, sans suppression ni affaiblissement du test.

Après les derniers ajustements de textes, de thème et de clavier : **37 tests ciblés réussis**. Ils ne s'ajoutent pas tous au total précédent, car ils comprennent des répétitions de vérification.

Ruff, format et syntaxe JavaScript conformes. Mypy Python 3.12 : aucun problème dans 141 fichiers source. La vérification locale ciblant Python 3.11 rencontre une incompatibilité des stubs NumPy installés dans l'environnement 3.12 ; elle doit être exécutée dans l'environnement 3.11 isolé prévu par CI. Les dépendances n'ont pas été modifiées.

Les scénarios navigateur utilisent la copie locale du code corrigé et les réponses API autorisées capturées pendant l'audit. Les API trades/CSV et références historiques emploient leur code corrigé ; les autres vues utilisent les réponses capturées. La simulation initiale d'expiration a été corrigée pour renvoyer réellement une page de connexion ; le scénario final passe sans erreur JavaScript non interceptée.

Preuves locales : `/home/ubuntu/audits/quant-tandem-fixes-2026-10-31/` — `full-tests.txt`, `relocation-test.txt`, `final-targeted.txt`, `mypy312.txt`, `output/playwright/regression.txt`, captures et contrôle d'accessibilité.

Contrôle visuel final : **12 configurations** (trois vues × mobile/ordinateur × clair/sombre), **aucune violation axe détectée et aucun débordement horizontal global** après correction du contraste du badge Short clair. Ce contrôle automatisé ne remplace pas une validation exhaustive au lecteur d’écran. Synthèse : `output/playwright/visual-summary.json`. Vérification ciblée finale après le dernier ajustement CSS : **33 tests UI réussis**. Intégrité des références legacy et Hyperliquid et contrôle SBOM conformes.

## Limites conservées explicitement

La comparaison historique reste indicative : ce correctif ne fabrique pas un backtest équivalent au moteur courant. Une première lecture de cache à froid peut attendre le réseau ; la déduplication est limitée à chaque processus serveur. Les sources legacy restent disponibles pour les installations sans SQLite, avec option de désactivation. Les textes historiques existants restent en majorité français.

La validation du navigateur public authentifié et le déploiement ne font pas partie de cette livraison locale. Le contrôle Impeccable a signalé que ses parseurs optionnels sont absents : ses résultats heuristiques ne remplacent donc pas les mesures axe effectuées dans le navigateur. Le fichier de métadonnées visuelles `.impeccable/design.json` est ancien par rapport à DESIGN.md ; aucune régénération hors périmètre effectuée.
