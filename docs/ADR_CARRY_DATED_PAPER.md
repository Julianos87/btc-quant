# ADR : Carry BTC à échéance en PAPER

- Statut : accepté pour développement et observation expérimentale
- Date : 2026-10-12
- Basé sur : main `2c22a36bccd868b2f43eb4aec8d2e28b41ffbe5b`
- Produit cible : BTC_USDC spot acheté + BTC_USDC future linéaire échéance short
- Référence officielle : documentation Deribit actuelle, à revérifier lors du build

## Décision

Ajouter une politique Carry `dated_future` pilotée par une configuration PAPER
expérimentale. Elle ne doit pas changer la politique `perpetual` existante ni
les paramètres/services/données de Trend. Réutiliser `StateStore`, la saga,
les checkpoints, les événements et les contrôles de reprise existants. Aucun
ordre d'exchange n'est implémenté ou autorisé.

Phase initiale : nouveau runner expérimental et DB dédiée sous le runtime
`carry-experiment`. Toute éventuelle migration vers le Carry canonique reste
ultérieure, après validation technique, et une décision distincte après preuves
économiques. Le protocole existant pour perp demeure inchangé et historisé.

## Contrats domaine

Les noms précis des classes sont libres si leur sens/validation est conservé.
Les champs financiers utilisent Decimal en domaine/calcul ; sérialisation JSON
précise et rétrocompatible dans le checkpoint. Les timestamps sont UTC.

`DatedInstrument` : venue, instrument_id, kind (`spot`/`dated_future`), base,
quote, settlement_currency, contract_size, amount_step, min_amount, tick_size,
expiration_utc, active, fee schedule/provenance, metadata timestamp/hash.

`DatedBook` : instrument_id, bids/asks comme niveaux prix/quantité, market time,
received time, source, sequence/hash éventuels, mark/index, freshness and quality.
Un carnet vide, croisé, futur ou trop vieux n'est pas tradable.

`DatedSettlement` : venue, instrument_id, stable settlement_key, kind
(`daily`/`expiry`), period_start/end UTC, official index/TWAP, source/provenance,
received_at, correction/version. Ne pas confondre prix officiel avec fill.

`DatedCarryDecision` : decision id/time, selected expiry, executable common BTC
quantity, spot/future fill estimate/depth, capital, cash for spot, margin cash,
fees and exit-cost assumptions, entry premium, risk reserve, net before/after
reserve, freshness, policy/version, decision and reason codes.

Checkpoint key `carry_dated` (or one explicitly agreed canonical ID inside a
dedicated DB): schema-versioned strategy state, status FLAT/OPENING/OPEN/
SETTLEMENT_PENDING/CLOSING/UNBALANCED/ACCOUNTING_UNCERTAIN, filled spot and
future quantities, lots/average entry, cash/equity, margin reservation, accrued
settlement cash, realized/unrealized components, selected contract/expiry,
last settlement key and high-water marks, pending saga/order IDs, risk state.

## Event and ledger semantics

Use existing `events` rows with event type `dated_future_settlement` and a stable
aggregate ID derived from venue + instrument ID + settlement key. Atomically
check/replay that event, apply its cash PnL exactly once, advance its cursor,
write checkpoint and append the event in one StateStore transaction. Same key
and same content is idempotent; same key/different price or provenance is a hard
identity collision and opens an accounting uncertainty/incident. No schema bump
is needed if the existing event table suffices; if the implementer demonstrates
that a migration is required, stop and propose its scope before adding tables.

No native funding accrues on a dated future. The daily settlement cash is
computed from the venue's exact rule and prior settlement reference, not booked
as both daily cash and a separate unrealized mark PnL. The final settlement uses
official TWAP. Spot remains until its separately recorded book-based liquidation;
it is never assumed sold at the TWAP.

## Ports and ownership

- Market/data agent owns new Deribit public client, domain market models,
  historical data/settlement adapter, dated collector, tests, and its service unit.
- Engine/accounting agent owns dated runner/ledger/decision application, minimal
  config/entrypoint integration, settlement atomicity method in StateStore,
  tests and restart/ledger behavior.
- Research/risk agent owns pre-registered dated protocol, score, replay and
  evaluation report. It consumes agreed domain models and does not rewrite
  runtime fills/ledger.
- Coordinator owns this ADR, baseline, shared config schema selection, final
  integration, migration/deployment, release and VPS verification.
- Dashboard/ops agent begins after the JSON status contract is frozen by
  coordinator/engine agent. Independent validation only audits integrated SHA.

Work is split into separate worktrees/branches. Do not edit Trend, shared
StateStore migrations, or dashboard contracts across parallel agents without
coordinator agreement. Additive changes and old checkpoint compatibility are
required.

## Entry, exit and risk boundary

V1 supports one BTC position/one expiry at a time. Discover expiry dynamically;
never hardcode contracts. Select deterministically among eligible expiries using
the pre-registered policy, not the largest historical premium. Compute purchase
from spot asks and short from future bids using actual depth/quantity steps. Fund
both legs, commissions and reserved margin from Carry capital. No borrowing or
cross-use of spot gains as derivative collateral unless the simulator has
explicitly modeled and documented that exact account rule.

Only open if both books and instrument metadata are fresh, coherent and deep
enough, the hedge after fills is within tolerance, available cash covers all
obligations and net projected outcome after an evidence-justified risk reserve
passes the frozen threshold. Otherwise WAIT with a precise code.

Exit rules must be explicit in config/protocol and exercised in replay: expiry,
protective margin/risk action, operational emergency and any early economics
rule. No implicit roll. Missing official settlement leaves the position
uncertain/pending; never infer the TWAP. Spot liquidation is priced from actual
observed bids/depth in PAPER at the configured moment and includes fee/slippage.

## Evidence boundary and deployment

Fixtures prove mechanics only. Live public market observations begin a new
prospective collection and are known development data. No economic selection
until a separate immutable protocol is registered and untouched evidence is
available. Paper results are not real fills or proof of personal venue fees.

First deployment is an isolated PAPER service and DB. Trend and current Carry
remain running under the deployment runbook. Never perform a schema migration or
canonical writer switch as part of the first experiment. A later cutover needs
Carry flat/reconciled, verified backup, idempotent transfer of actual remaining
cash, sole-writer lock and release-bound qualification. Economic adoption is a
further gate after the preregistered forward evidence passes.
