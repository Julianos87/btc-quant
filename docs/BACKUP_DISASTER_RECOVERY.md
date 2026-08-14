# BTCQuant backup, restore and disaster recovery — Lot 7

This document describes the Lot 7 local protocol. It is a preparation and
test framework, not an instruction to deploy, migrate, restore or restart a
production service. No production path is used by the tests.

## Current state and scope

The repository already contains a daily `btcquant-backup.timer`,
`scripts/backup_state.sh`, `scripts/backup_database.py` and
`scripts/verify_backup.py`. Those legacy scripts are retained and audited,
but they do not by themselves provide an immutable, hash-addressed BackupSet
with a catalog, retention generations or recovery fencing. The Lot 7 layer in
`src/btcquant/backup.py` is the explicit protocol for future qualification.

The existing state database, shadow database and research governance database
are distinct assets. A governance database is never copied into the trading
database, and a trading database is never used as the research registry.

The default scheduled job has not been enabled, started or changed by Lot 7.
Off-host replication is not treated as configured or verified by this local
framework. Therefore the 3-2-1 objective and the requested off-host RPO remain
open operational work; this first pass does not claim disaster-recovery
qualification.

## BackupSet contract

`BackupSource` is an explicit allow-list entry. Its stable logical name,
classification, source type and restore requirement are recorded in
`backup-manifest.json`. Secret-like paths (`.env`, credentials, tokens,
wallets, seeds and private keys) are refused and are not placed in manifests.

SQLite sources are opened with `mode=ro`, `query_only=ON` and copied through
the SQLite Online Backup API. The source is not checkpointed, vacuumed,
rewritten or cleaned up. WAL and SHM files are not copied or deleted. The
source and destination are checked with `PRAGMA integrity_check` and
`PRAGMA foreign_key_check`.

A BackupSet is created in a hidden staging directory and published with one
same-filesystem rename only after all entries have been copied and hashed.
Its final directory name is an immutable backup identity derived from the
manifest. The manifest records the exact application Git SHA, application
schema, source identity, capture times, capture skew, entry sizes, SHA-256
hashes, classifications and whether external exchange state was included.
The manifest sidecar hashes the manifest itself. A partially created set is
never published as a valid set.

The allowed classifications are:

* `AUTHORITATIVE_TRADING_STATE`
* `AUTHORITATIVE_SHADOW_STATE`
* `AUTHORITATIVE_RESEARCH_GOVERNANCE`
* `AUDIT_EVIDENCE`
* `CONFIGURATION`
* `DERIVED_REBUILDABLE`

The trading and research restore markers explicitly record that exchange
state was not included. No restored state may be considered live-ready on the
basis of a local SQLite file alone.

## Explicit protocol

The local interface is `scripts/backup_dr.py`:

```text
create --destination-root ROOT --git-sha SHA --schema-version N \
  --source trading=/path/btcquant.db:AUTHORITATIVE_TRADING_STATE \
  --source-identity canonical-repository
verify --backup-directory ROOT/BACKUP_ID --schema-version N
list --destination-root ROOT
prune --destination-root ROOT --recent 24 --daily 7 --weekly 4 --monthly 12
restore-to-staging --backup-directory ROOT/BACKUP_ID \
  --destination /tmp/restore-staging --runtime-root /tmp/runtime \
  --schema-version N
```

`create`, `prune` and `restore-to-staging` share a non-blocking `flock`; a
second concurrent operation fails with `BACKUP_ALREADY_RUNNING`. The command
has no production defaults and never starts systemd, a runner or an exchange
client.

The safe sequence for a future production migration is:

```text
acquire single-flight lock
→ validate exact release and configuration
→ stop all DB writer services and timers
→ prove quiescence and inspect open handles where supported
→ SQLite checkpoint using SQLite (never rm WAL/SHM)
→ Online Backup API
→ verify hash, manifest, integrity and schema
→ explicit migration only if authorized
→ validate migrated schema
→ atomic release switch
→ start target services
→ bounded health/readiness checks
→ success, or fail closed with recovery procedure
```

The backup is made after writer quiescence. A hot backup is supported for
read-only sources and WAL visibility, but it is not a substitute for the
quiescence gate before an irreversible migration.

## Restore and recovery fencing

Restore is staging-only and refuses an existing destination. It verifies the
exact BackupSet identity, all hashes, SQLite integrity and schema compatibility
before the atomic staging publish. It never overwrites `current`, `previous`,
the production database or a service runtime.

Restoring `AUTHORITATIVE_TRADING_STATE` creates `recovery-state.json` with
`RECOVERY_REQUIRED`. A writer must not start until the following evidence is
recorded and the marker moves through the exact sequence:

```text
RECOVERY_REQUIRED
→ RECONCILIATION_VERIFIED
→ RECOVERY_CLEARED
```

The evidence must cover exchange reachability, local and external order
reconciliation, positions, stops, no unbalanced state, no ambiguity and a
compatible accounting checkpoint. Direct transitions, missing evidence and
automatic clearing are refused. A normal non-restored installation may have no marker. Once a restore
marker exists under the controlled runtime state root, every StateStore writer
uses that root and refuses startup until the exact sequence reaches
RECOVERY_CLEARED; diagnostic and dashboard readers remain allowed.

Restoring `AUTHORITATIVE_RESEARCH_GOVERNANCE` creates a separate
`research-recovery-state.json`. It blocks real research until governance
lineage, trial ledger continuity and policy compatibility have been explicitly
verified. It cannot reset budgets or make a restored old registry appear new.

The exchange state is explicitly absent from every local BackupSet manifest in
this protocol. Therefore an external exchange reconciliation is always
required before any trading writer could be considered for restart.

## Retention and backup health

`RetentionPolicy` is the proposed operational policy, not a measured
production guarantee:

| generation | keep |
| --- | ---: |
| newest/recent | 24 |
| UTC daily | 7 |
| UTC weekly | 4 |
| UTC monthly | 12 |

Selection is by UTC bucket and always retains the newest verified valid set.
Invalid, corrupt, incomplete, unverified or unknown-version sets are never
deleted by the retention function. Pruning is serialized with creation and
restore. Free-space checks include a safety margin before backup creation.

Backup status is distinct from business data freshness:

FRESH_BACKUP -> STALE_BACKUP -> UNKNOWN

backup_age is based on created_at_utc; verification_age is based on the last
byte/integrity verification; and restore_drill_age is based on an explicit
successful drill record. Re-verifying a 30-day-old backup does not make the
backup itself fresh. Future timestamps produce UNKNOWN; a failed verification
never resets a previous successful verification timestamp. Capture skew is
recorded and a set exceeding its configured bound is DEGRADED, not trusted for
restore.

trusted means current-byte and integrity verification only. It does not mean
that a restore drill has run. A fresh or degraded BackupSet therefore reports
restore_verified = false until an external, append-only drill record binds the
backup ID, manifest SHA-256, successful integrity/application-open tests and
recovery-gate evidence.

An off-host export must pass through export_verified_backup_set. The exporter
callback is admitted only for a COMPLETED, independently byte-verified and
trusted BackupSet; a prior restore drill is not required merely to replicate a
new backup. It is responsible for authenticated encryption and remote
publication. CREATING, failed, degraded, corrupt or unverified sets never
reach that boundary. Lot 7 does not invoke a remote exporter.

## Failure and recovery matrix

| failure point | persisted state | automatic action | manual action / safety tradeoff |
| --- | --- | --- | --- |
| source unavailable | no published set | none | investigate source |
| low disk | no published set | none | free/quarantine space |
| copy/hash/integrity failure | staging only or invalid set | never select it | inspect and retry |
| crash before publish | hidden staging only | ignore staging | remove after audit |
| crash after publish before verification | completed manifest, no verification record | exclude from trusted catalog | re-verify |
| manifest/hash corruption | set invalid | skip; never prune as good | quarantine/manual review |
| migration required | old schema in staging | no automatic migration | explicit migration with verified backup |
| restore destination exists | destination untouched | refuse | choose a new staging path |
| trading restore | recovery marker required | writer start refused | exchange/order/position/stop reconciliation |
| research restore | research marker required | real search refused | validate lineage and trial continuity |
| health failure before any target writer | DB must remain fenced | no automatic production action | restore/rollback only under verified procedure |
| first target writer starts | point of no automatic DB restore | stop and fail closed | manual recovery; preserve DB and backup |
| exchange state absent | local state incomplete | no safe resume | external exchange reconciliation |
| off-host unavailable | local-only durability | no claim of DR | configure and verify independent target |

No automatic restore is permitted after a target writer has started. No
automatic old-code start is permitted against a newer database. A backup
restore is not a broker reconciliation and never authorizes exchange orders.

## Tests and limits

The Lot 7 tests use temporary directories and temporary SQLite databases only.
They cover WAL visibility, source non-mutation, backup API copying, hashes,
manifest tampering, path traversal/symlinks, unknown versions, partial
publication, free-space refusal, concurrent lock acquisition, retention
boundaries, exact restore, schema mismatch, recovery marker transitions,
trading/research gates and a restore drill with no service or exchange call.

The tests do not establish a production RPO/RTO, off-host durability, backup
encryption/key custody, cloud retention immutability, real VPS quiescence or
exchange reconciliation. Those remain explicit preconditions for any future
production recovery qualification.

## Writer classification and quiescence

The current code classification for the trading database is explicit:

| unit | database role | migration gate |
| --- | --- | --- |
| `btcquant-carry.service` | writer | inactive/dead required |
| `btcquant-trend.service` | writer | inactive/dead required |
| `btcquant-watchdog.service` | writer (incidents) | inactive/dead required |
| `btcquant-compact.service` | writer (compaction) | inactive/dead required |
| `btcquant-backup.service` | backup reader; still quiesced to avoid open handles during migration | inactive/dead required |
| `btcquant-rebalance.service` | writer when applying | inactive/dead required |
| `btcquant-rebalance-pending.service` | potential writer | inactive/dead required |
| `btcquant-dashboard.service` | read-only StateStore/ShadowStore in the current code | no business write evidence |
| `btcquant-shadow.service` | writer of the separate shadow database | separate shadow gate |

The five associated writer timers are also gated. `require_writer_quiescence`
requires a fresh state for every known service and timer; missing state, an
active unit or an open database/WAL/SHM handle is `MIGRATION_REFUSED`. It does
not stop units and is therefore safe to call from a future orchestrator after
an explicit stop operation. An `absent` state must be reported explicitly by
the caller; the library never infers it.

## Restore drill record

A drill can emit a small audit record outside the immutable BackupSet with the
backup identity, UTC start/end, result, integrity result, application-open
result and recovery-gate presence. It contains no database payload, secret or
machine credential.

## Lot 7.3 adversarial gates

A manifest SHA-256 and its sidecar provide corruption detection and
canonical-integrity checking; they do not provide authenticity or prove that
an untrusted writer did not replace the whole set. The protocol therefore
recomputes every entry hash, size, SQLite integrity result, foreign-key result,
allow-listed source identity, total byte count and capture-skew-derived state
at every verification. "COMPLETED" is exportable only when the verification
record is present and the complete byte-level verification passes. A later
payload or manifest change is rejected even when the filename is unchanged.

The restore order is strict: copy and re-verify each artifact, fsync the
staging files and directory, write and fsync the recovery marker, fsync its
parent, then perform the same-filesystem atomic rename that activates the
staging directory. If activation fails after a marker was written, the
marker remains a fail-closed recovery fence; no writer is started.

Every StateStore opened in writer mode checks the trading recovery marker at
construction. This covers Trend, Carry, watchdog incidents, compaction,
backup bookkeeping, rebalance and pending-rebalance writers, including
qualification/migration entrypoints. Diagnostic and dashboard reads use
explicit SQLite mode=ro and query_only=ON. Shadow writers use the separate
shadow marker. A valid RECOVERY_CLEARED marker is the only state that permits
a writer; malformed, truncated, unknown, future, permission failed or
symlinked markers fail closed.

Research recovery is separate. A real-search authorization must use the
explicit BTCQUANT_GOVERNANCE_DB path and explicit BTCQUANT_RECOVERY_ROOT,
and the durable frozen policy, committed freeze artifact and research marker
must agree. A restored governance marker blocks real search until its own
lineage evidence is reconciled; changing the working directory or selecting
a different empty registry cannot bypass this gate. A cleared recovery state
still authorizes neither live trading nor exchange execution.

For multiple authoritative databases, capture skew is recorded in the
manifest and is never represented as a false atomic snapshot. Optional
sources are represented as OPTIONAL_NOT_PRESENT; absence is not silently
converted into a captured empty database. Disk checks include source SQLite
sidecars where present, staging duplication and a safety margin. Retention
uses verified bytes only and always preserves the newest verified set; it
does not delete the last good recovery point.


## Current backup-ref distinction

The sanitation anchor is 8b9112e7f1bc21a1408fc6d6057b55d69839a825.
The legitimate scheduled commit 428f6c1c3fb62674b625dd34761aca07f9e01ece adds
state-20260814-0300.tar.gz.enc and is intentionally retained. That
pre-corrective archive proves only that the legacy encrypted scheduled
pipeline continued to run; it does not prove the new SQLite allow-list or WAL
handling is deployed.

## Public backup history incident and off-host policy
A forensic audit found 13 plaintext runtime archives on the public backups
branch, covering 2026-07-14 through 2026-07-26. The audited archives contained
operational/trading state such as equity history, strategy state, logs and trade
history. No .env, API credential, private key, seed, wallet secret, dashboard
token or exchange credential was detected in the 13 archives examined.

The sanitation anchor is
8b9112e7f1bc21a1408fc6d6057b55d69839a825. The current scheduled branch head at
the start of this corrective was
428f6c1c3fb62674b625dd34761aca07f9e01ece, a legitimate descendant retaining
37 encrypted archives. The retained encrypted generations remain byte-for-byte
intact and the current branch tree reaches no plaintext archive, database,
WAL/SHM file, log, CSV, JSON or secret. This does not retroactively erase
publicly exposed information: old commit and raw URLs may remain accessible
from GitHub's object/cache layer and require separate verification or support
action.

The independent legacy restore of state-20260813-0300.tar.gz.enc was qualified
on 2026-08-13 in temporary staging with SQLite integrity and foreign-key checks
passing. The restored trading database remained fenced because external
exchange state is not included and its application schema is v4 while the
current code targets v6. The scheduled state-20260814-0300.tar.gz.enc predates
the current multi-SQLite truthfulness corrective and is not evidence that the
corrective is deployed.

The new BackupSet v1 off-host operational deployment is NOT YET DEPLOYED /
NOT YET PRODUCTION-QUALIFIED. Full 3-2-1 qualification is NOT YET QUALIFIED.
The public repository is transitional only and is not the preferred final
target. Production should use a dedicated private repository or private object
storage, configured through BACKUP_OFFHOST_REMOTE and an explicit branch
configuration rather than a hard-coded provider. The current legacy encryption
format is OpenSSL AES-256-CBC with PBKDF2, 200000 iterations and salt. Missing,
empty or whitespace-only encryption keys fail closed before archive publication;
encryption success alone is not a validity proof. BackupSet manifest, entry
hashes and SQLite integrity checks remain mandatory after restore.
