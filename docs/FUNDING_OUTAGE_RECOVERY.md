# Funding : continuité, protection et reprise PAPER

Les cycles Carry two-leg ne confondent plus un signal temporairement incomplet
avec une comptabilité irréconciliable. Une série vide, en erreur ou périmée
suspend les nouvelles entrées, sauvegarde le checkpoint et sera retentée.
La fraîcheur requise est une période native plus cinq minutes de tolérance de
publication. Les prix sont toujours acquis pour les contrôles de marge,
kill switches et expiration d'une couverture incomplète.

Une lacune comptable n'est jamais remplacée par un paiement inventé : le curseur
s'arrête avant le premier créneau manquant. Les paiements suivants attendent le
paiement manquant, même si l'API les a déjà fournis. Les timestamps bruts restent
les identités des événements ; les créneaux natifs servent uniquement à vérifier
la continuité. Un lookback d'une période distingue un checkpoint d'événement
légèrement antérieur au créneau d'un checkpoint d'initialisation arbitraire.
Deux événements distincts sur le même créneau sont rejetés.

Trend continue ses protections pendant une erreur d'acquisition du funding et
suspend les nouvelles expositions jusqu'à la reprise. Les incidents temporaires
sont résolus après obtention des données. Une véritable erreur d'application
comptable conserve son traitement fail-closed.

Si Carry doit fermer une position pour risque alors que son funding n'est pas
entièrement vérifiable, l'exposition est fermée puis un état comptable incertain
est enregistré. Une réconciliation est nécessaire avant reprise : la fermeture
ne prétend pas avoir soldé des paiements inconnus. Une panne d'écriture du
checkpoint atomique arrête le processus sans sauvegarde finale d'une mutation
non validée.

L'ancien blocage `Signal carry two-leg incertain` peut être levé automatiquement
uniquement lorsque le bilan restauré est explicitement FLAT, sans quantité spot,
perp ni principal emprunté. Les autres incertitudes ne sont pas levées.

Le chemin two-leg produit désormais un échantillon d'equity à chaque cycle
terminé. Les échantillons historiques manquants ne sont pas reconstruits comme
s'ils avaient été observés.
