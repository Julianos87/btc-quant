# Legacy synthetic Carry cutover

Commande de maintenance **v6** qui retire un checkpoint Carry paper synthétique
(`in_position=true`, `OPEN`, quantités nulles, sans économie d'entrée) et le
remplace par un état `FLAT` sans inventer de fill, de trade, de PnL ou
d'historique funding.

Elle n'est **pas** :

- une migration de schéma ;
- un comportement de `CarryRunner` au démarrage ;
- un chemin automatique de déploiement ;
- une réécriture SQL manuelle.

## Preconditions

1. Backup vérifié de la base (Online Backup API + SHA-256 + `integrity_check`).
2. Schéma applicatif **exactement 6**. Si la base est encore en 4, d'abord
   `python -m btcquant.entrypoints.migrate --confirm-migration` sur la copie
   de maintenance. Cette commande **refuse** d'auto-migrer.
3. `btcquant-carry` **arrêté**. Le runbook de déploiement exige l'arrêt du
   writer Carry avant toute mutation de maintenance.
4. Configuration **paper** uniquement (`environment: paper`, `execution.mode: paper`).
5. Motif legacy exact (voir ci-dessous).
6. Hash compare-and-swap de l'état **courant**.
7. Marker de recovery Lot7 absent ou `RECOVERY_CLEARED`.
8. Aucun ordre Carry, aucun `funding_ledger`, aucun incident CRITICAL Carry OPEN.

## Diagnostic (aucune écriture)

```bash
python -m btcquant.entrypoints.carry_cutover \
  --database /path/to/maintenance-copy.db \
  --config environments/paper/config.yaml \
  --print-expected-state-sha256
```

Sortie attendue sur le motif actuel de production :

```
carry_state_sha256=<64 hex>
pattern=LEGACY_SYNTHETIC_OPEN_QTY0
```

Le hash est canonique : JSON `sort_keys=True`, séparateurs `,` / `:`, UTF-8.
Toute mutation du checkpoint entre le diagnostic et l'application **refuse**
le cutover.

## Application

Carry doit rester arrêté. Ne jamais viser `/opt/btcquant/state/btcquant.db`
depuis un déploiement automatique.

```bash
python -m btcquant.entrypoints.carry_cutover \
  --database /path/to/maintenance-copy.db \
  --config environments/paper/config.yaml \
  --expected-state-sha256 <hash diagnostiqué> \
  --git-sha <sha du release v6> \
  --confirm-legacy-synthetic-cutover
```

Succès :

```
CUTOVER_APPLIED
old_state_sha256=...
new_state_sha256=...
equity=<inchangé>
cutover_timestamp_utc=...
```

Seconde invocation **sur l'état résultant exact**, avec le hash **post-cutover** :

```
NO_OP_ALREADY_CUT_OVER
```

Un Carry déjà `FLAT` **sans** l'événement d'audit `legacy_synthetic_carry_cutover`
n'est **pas** un no-op : la commande refuse.

## Motif legacy exigé

- `in_position == true`
- `execution_state == "OPEN"`
- `qty == spot_qty == perp_qty == 0`
- `entry_equity`, `entry_timestamp`, `entry_price`, `position_generation`,
  `funding_notional_price` absents ou nuls
- `spot_notional`, `perp_notional`, `borrow_principal` absents ou 0
- aucun ordre Carry
- `funding_ledger` vide
- ligne `positions` carry `OPEN` qty 0, cash = equity
- pas d'`accounting_uncertain`

## Refus typiques

| Condition | Effet |
|---|---|
| schéma ≠ 6 | refuse, pas d'auto-migration |
| config testnet / live / non-paper | refuse |
| hash attendu ≠ hash lu | refuse |
| qty / spot_qty / perp_qty > 0 | refuse |
| `entry_price` présent | refuse |
| `perp_notional` > 0 | refuse |
| `funding_ledger` non vide | refuse |
| ordre Carry (y compris non résolu) | refuse |
| `OPENING` / `CLOSING` / `UNBALANCED` | refuse |
| incident CRITICAL Carry OPEN | refuse |
| recovery marker actif | refuse |
| verrou d'instance Carry tenu | refuse |
| confirmation absente | refuse, aucune écriture |

## Frontière funding

`last_funding_ts` devient l'horodatage UTC du cutover.

Le ledger v6 reste vide. Un futur `OPEN` v6 authentique fixe sa propre
génération, ses notionnels et son checkpoint d'entrée. Aucun PnL
rétroactif n'est fabriqué sur l'intervalle synthétique.

## Vérification post-cutover

- `engine_state` carry : `FLAT`, `in_position=false`, quantités 0
- `positions` carry : `FLAT`, qty 0, cash = equity **identique**
- un seul événement `legacy_synthetic_carry_cutover`
- `orders`, `trades`, `flows`, `funding_ledger`, Trend : inchangés
- `CarryRunner` paper démarre **sans** `accounting_uncertain`

Redémarrer Carry **seulement** après cette vérification.

## Séquence de déploiement recommandée

1. backup v4 vérifié
2. quiescence des writers
3. migration officielle 4→6
4. bascule de release v6
5. Trend (et dashboard) seulement ; **Carry reste down**
6. diagnostic + cutover explicite
7. vérification
8. redémarrage Carry
