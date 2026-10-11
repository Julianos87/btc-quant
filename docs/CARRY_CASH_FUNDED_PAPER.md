# Carry PAPER sans emprunt

Le moteur Carry utilise désormais `environments/paper/carry_cash.yaml`.
Le profil ×3 reste conservé dans `config.yaml` comme scénario de comparaison.
Trend continue d'utiliser ce dernier fichier : son budget, ses règles et ses
historiques ne changent pas. Les performances publiées de l'ancien portefeuille
ne qualifient pas le nouveau Carry.

Le dashboard calcule l'exposition depuis les notionnels des positions enregistrées,
sans multiplier l'équité Carry par le ×3 de la référence historique. Son agrégat
de risque compte la jambe perp une fois ; l'exposition brute compte les deux jambes.

## Financement simultané

Le capital réservé au Carry reste 4 000 sur les 10 000 du portefeuille.
Le spot est payé intégralement en cash : emprunt désactivé, plafond zéro.
Le moteur calcule la taille qui finance ensemble spot, marge du short et coûts
d'entrée, puis laisse 2 % de cash disponible. Il réserve 50 % du notionnel perp
en marge initiale et maintient le seuil de maintenance à 5 %. À prix spot/perp
égaux, le notionnel spot est environ 2 610 ; ce n'est pas 4 000 de spot auxquels
on ajouterait gratuitement une marge. Les quantités BTC des deux jambes sont égales.
Le budget baisse si l'équité disponible baisse. Un fill coûteux ne peut créer une
dette dépassant le plafond déclaré ; une transition ambiguë bloque les entrées.

Le taux d'intérêt du scénario reste visible à 10 %, mais une dette nulle ne
produit aucun intérêt. La marge renforcée évite de financer le short avec des
gains spot qui se trouveraient dans un autre compte.

## Économie et limites

Le contrôle de rendement net conserve frais, slippage, horizon 30 jours et
réserve d'incertitude 2 %. La réserve sert à décider d'une entrée ; ce n'est pas
un frais débité du bilan. Le moteur attend si le rendement net attendu est nul
ou négatif. Aucun paramètre de signal n'est optimisé pour provoquer des trades.

`scripts/diagnose_carry_cash.py --output rapport.json` compare deux scénarios
fixes sur les observations Hyperliquid spot UBTC, perp BTC et funding horaire
disponibles entre janvier et août 2026. Le rapport inclut provenance des fichiers,
six périodes consécutives sans recouvrement, funding connu à la décision,
décomposition hypothétique à 30 jours et violations de maintenance possibles.
Les clôtures horaires restent des proxies de prix d'exécution. Le scénario
d'emprunt n'est pas un historique de taux effectivement disponibles.

La fenêtre a déjà été analysée : ce diagnostic n'est pas un test aveugle, une
recherche de paramètres ni une qualification de rentabilité. Les variations
adverses du basis à 30 jours atteignent 1,03 % dans cet échantillon ; ses quantiles
ne couvrent ni erreur de prévision du funding, ni depeg, ni risques d'exécution.
Ils ne justifient donc pas à eux seuls de réduire la réserve de 2 %.

Avec les coûts et protections retenus, aucune des six décisions de début de
période n'autorise une entrée. Les résultats hypothétiques du ×3 sont négatifs
dans les six périodes. Le cash améliore les six résultats, mais reste négatif
dans quatre périodes. Une sortie anticipée peut changer ces montants : ce ne
sont pas des gains de trades effectivement exécutés.

## Déploiement et observation

Le changement se fait Carry à plat, sans dette ni ordre en attente, avec
sauvegarde chiffrée vérifiée. Le service et le lancement Carry par défaut utilisent
le fichier cash. Une nouvelle campagne PAPER est liée à la nouvelle release ;
la maturité et les trades historiques ne sont pas réattribués au nouveau profil.
L'observation doit fournir des opérations réelles en PAPER et un résultat net
complet avant toute affirmation de rentabilité. Aucune ouverture réelle n'est autorisée.
