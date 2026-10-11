# Carry 3× autonome en PAPER

Le profil PAPER simule un crédit, sans compte Hyperliquid, adresse, clé ni ordre externe.
Ce scénario ne qualifie pas une capacité de financement réelle et ne modélise pas
le portfolio margin Hyperliquid. Il utilise deux comptes simulés avec une marge
perp distincte. Le choix a été confirmé le 10 octobre 2026 : PAPER uniquement.

## Hypothèses explicites

- Capital Carry : 4 000 ; notionnel cible : 12 000 (3×), identique au profil précédent.
- Crédit simulé plafonné à 12 000 ; seul le principal nécessaire est emprunté.
- Marge initiale du short : 10 % ; maintenance : 5 %, hypothèses du scénario.
- Intérêts : 10 % annualisés, inchangés, débités sur la dette effective.
- Frais, slippage, seuils de funding et réserve de 2 % inchangés.
- Aucune capacité réelle de plateforme n'est déduite de ces paramètres.

Le plan réserve simultanément la marge perp et les coûts. Pour 4 000 de capital,
12 000 de spot et des prix spot/perp égaux, il faut environ 9 224 de crédit
avec les coûts Carry actuels (0,05 % par jambe et 5 bps de slippage), et non 8 000.
Le plafond est une limite de crédit, pas du cash gratuit ajouté à l'équité.
Les fonds empruntés restent une dette à rembourser ; les intérêts sont comptabilisés.

Une observation peut légitimement produire `net_expected_non_positive` : le moteur
fonctionne mais attend une opportunité nette. Le financement simulé n'autorise
pas à supprimer ce contrôle pour obtenir des trades. Le détail économique et le
plan sont enregistrés avec chaque refus. Le journal de checkpoint reste borné à
128 entrées ; les événements historiques durables restent conservés.

## Séparation des environnements

`financing_mode: paper_simulated` exige `environment: paper`, `execution.mode: paper`
et une provenance commençant par `PAPER_SIMULATED_`. Le runner refuse tout broker
externe avec ce mode avant d'ouvrir la base. L'état persiste le mode et les sources.
Une qualification réelle exigerait un autre profil et des preuves de compte.

## Continuité

Ce changement ne réécrit pas la campagne PAPER n°6 ni son binding historique déjà
invalide. Les anciennes observations restent conservées. Une nouvelle campagne
formelle sera une opération distincte : aucune maturité historique n'est revendiquée.
Les positions Trend, ordres, funding et historiques ne doivent pas être réinitialisés.

## Validation et exploitation

Les tests couvrent l'interdiction du broker externe, le rejet du testnet, le financement
simultané, le refus net, l'entrée 3×, les intérêts, la reprise sans double funding et
la sortie. Les suites existantes couvrent les remplissages partiels et les incidents.

Les erreurs réseau restent reprises par les boucles existantes et les retries bornés.
Systemd redémarre les processus en cas de sortie. Un état systemd `active` ne prouve
pas à lui seul une décision récente : vérifier les timestamps SQLite et les incidents.

Le verrou ccxt imposait multidict 6.7.1 et urllib3 2.7.0 rejetés par l'audit CI.
Les substitutions ciblées dans `[tool.uv]` conservent ccxt et imposent leurs versions
corrigées ; Werkzeug est également corrigé. Lock, export et SBOM sont régénérés.
