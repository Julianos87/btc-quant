# Post-final PAPER hardening

This document describes the off-production capacity, retention, and alert
contracts added after the final PAPER candidate. It does not authorize a
deployment or change the active PAPER maturity campaign.

## Capacity observation

`btcquant-ops-status` reports filesystem headroom and bounded local footprints
for releases, encrypted backups, state, SQLite WAL/SHM, application logs, and
known temporary artifacts. Journal storage is intentionally reported as
`NOT_MEASURED` because it is outside the service root and is governed by the
host's journald policy.

The scan is bounded by an entry limit and never follows symlinks. An incomplete
measurement is explicit and is not converted to a safe estimate.

## Retention planner

The retention planner is `DRY_RUN_ONLY`. It reports path, type, age, size,
classification, protection reason, and cleanup eligibility. It performs zero
deletions.

The current release, previous release, release bound to active maturity, state,
data, and the latest verified encrypted backup are protected. Unknown,
unreadable, symlinked, outside-root, corrupt, and unverified artifacts are not
eligible for cleanup. A future deletion command would require a separate
authorization and independent tests for path confinement and race resistance.

## Alert model

The status snapshot contains a side-effect-free alert evaluation. Alert keys are
deterministic (`condition:subject`) and can be compared with a caller-owned
previous key set to classify `new`, `ongoing`, and `resolved` conditions. No
alert state is written to SQLite and no notification is sent by this module.

CRITICAL conditions include DB safety failure, unresolved/reconciliation state,
open critical incidents, health/readiness failure, inactive required services,
backup verification failure, capacity failure, and TESTNET safety failure.
Capacity WATCH and backup freshness WATCH are WARNING conditions, not emergency
failures. UNKNOWN remains non-PASS and is reported explicitly.

The existing Telegram notifier remains the only notification integration. This
campaign does not enable real notifications or expose credentials.

## Safety boundary

These changes are read-only at runtime. They do not start or stop services,
write the trading database, alter maturity, activate TESTNET, or contact an
exchange.
