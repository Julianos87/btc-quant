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

Domain types live in `src/btcquant/domain/carry_dated.py`; the read-only venue
client lives in `src/btcquant/execution/carry_dated_market.py`. Research and
runner code import the shared domain types; do not define lookalike DTOs.

`DatedInstrument` : venue, instrument_id, kind (`spot`/`dated_future`), base,
quote, settlement_currency, contract_size, amount_step, min_amount, tick_size,
expiration_utc, active, fee schedule/provenance, metadata timestamp/hash.

`DatedBook` : instrument_id, bids/asks comme niveaux prix/quantité, market time,
received time, source, sequence/hash éventuels, mark/index, freshness and quality.
Un carnet vide, croisé, futur ou trop vieux n'est pas tradable.

`DatedSettlement` : venue, instrument_id, stable settlement_key, kind
(`daily`/`expiry`), period_start/end UTC, official index/TWAP, source/provenance,
received_at, correction/version. Ne pas confondre prix officiel avec fill.

`DatedCarryDecision` is shared by engine and research code: decision_id, action,
reason_code/message/as_of, instrument/expiry, common quantity, spot/future
estimated execution, capital/free cash, initial margin, independent stress cash,
fees, gross/net before and after reserve, policy version and both market times.
Money and quantities are Decimal. The pure economic function
`btcquant.domain.carry_dated_policy.evaluate_entry` accepts candidates,
capital, fee schedule, margin policy and current UTC time, then returns a
`DatedCarryDecision`; it performs no I/O.

`DatedCarryDecision` : decision id/time, selected expiry, executable common BTC
quantity, spot/future fill estimate/depth, capital, cash for spot, margin cash,
fees and exit-cost assumptions, entry premium, risk reserve, net before/after
reserve, freshness, policy/version, decision and reason codes.

Engine key `carry_dated` in the dedicated experimental DB. Status JSON uses
`strategy=carry_btc_dated`, `schema_version=1`, `paper_only=true`, and
FLAT/OPENING/OPEN/SETTLEMENT_PENDING/CLOSING/UNBALANCED/ACCOUNTING_UNCERTAIN.
It includes instrument/expiry, capital/free_cash/cash_total/reserved_margin,
spot_qty/future_qty and average entries, last settlement key/index/count,
realized/unrealized PnL, fees, decision/action/reason/as_of, spot/future market
timestamps, uncertainty/incident and pending saga IDs. Serialize money and
quantities as canonical decimal strings. A dashboard reader must reject unknown
versions or missing required keys; it must not infer a position from equity.

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

- Market/data agent owns `domain/carry_dated.py`, new Deribit public client,
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

For the derivative cash requirement, model the current public Deribit Segregated
Standard Margin (S:SM) schedule, not a hard-coded percentage. The source updated
2026-09-28 specifies IM=1/L(N), MM=2/3*IM; BTC dated futures are Tier 4. Read
the applicable max amount and schedule from qualified official contract metadata
and version/hash them. Independently reserve cash for a preregistered adverse
price move: spot equity is not derivative collateral in the simulator. The
stress amount, reserved margin, free cash and entry admission buffer stay as
distinct values and are tested in cash units.

The official routed-spot specification confirms BTC_USDC routing and fee rate,
but does not clearly state the fee debit currency for that path. Keep the venue
field unknown unless authoritative evidence resolves it. For PAPER decisions,
calculate both quote-currency and base-currency spot fee treatments and admit
only against the more adverse cash/hedge/result case. Persist the assumption and
show resulting hedge residual; never label the assumed fee asset as an observed
venue fill or claim account-specific fees. If the conservative case cannot be
funded or passes no decision threshold, WAIT with `FEE_CURRENCY_UNCERTAIN`.

V1 has no early economic exit and no roll: hold the short through official expiry
and sell the spot using an observed PAPER book. A protective early close is
allowed only when modeled collateral/margin plus the funded adverse-move buffer
exceeds actual free derivative cash; the short is covered then the spot is sold,
with partial fills represented as UNBALANCED. Stale/missing market data blocks
entries and starts a risk-review/pending state; it does not trigger a liquidation
at an invented price. Missing official settlement leaves the position
uncertain/pending; never infer the TWAP. Spot liquidation is priced from actual
observed bids/depth and includes fees/impact.

The public settlement lookup `get_last_settlements_by_instrument` rejects some
expired instruments. For reconciliation, the market adapter may use the
official public by-currency settlement history filtered by exact instrument,
and the official delivery-price history after confirming the response identity.
Pagination is bounded. Missing, ambiguous, or corrected-but-not-reconciled data
fails closed; a candle or sampled midpoint is not a substitute for official TWAP.

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
