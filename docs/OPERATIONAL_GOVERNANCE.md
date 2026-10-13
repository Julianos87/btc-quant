# Operational governance and observability

`python -m btcquant.entrypoints.ops_status` is the read-only operator surface for the PAPER host. It
observes the active immutable release, the latest technical qualification, the
bound PAPER maturity campaign, the canonical PAPER database, service and
health/readiness state, backup evidence, disk capacity, and TESTNET safety.

## Operator command

Run from any working directory with the deployed root explicitly selected:

```text
BTCQUANT_ROOT=/opt/btcquant /opt/btcquant/current/venv/bin/python -m btcquant.entrypoints.ops_status
BTCQUANT_ROOT=/opt/btcquant /opt/btcquant/current/venv/bin/python -m btcquant.entrypoints.ops_status --json
```

The command performs no SQLite write, service restart, release switch, backup
deletion, qualification record, maturity mutation, exchange call, or TESTNET
activation. The JSON output is canonical (`sort_keys=true`) and includes
`read_only: true`.

`PASS` means the observed contract passed. `WATCH` means the system is safe to
observe but needs operator attention (for example maturity not started or
incomplete, or source/runtime drift). `FAIL` means a known safety or binding
condition failed. `UNKNOWN` means evidence could not be established; it is
never converted to `PASS` and exits non-zero.

## Identity and maturity interpretation

The report keeps these identities separate:

- source repository/main is informational and drift is `WATCH`;
- active PAPER is the validated immutable `current` release;
- technical qualification is the durable PAPER observation matching that
  active release;
- maturity is the bound v3 campaign and its current release/config identity.

The maturity section shows duration, terminal orders, required-engine orders,
and closed trades as bounded percentages. Historical counters are not
backfilled by this command.

Each encrypted daily archive has an adjacent
`<archive>.verification.json` record. The backup job writes it only after all
SQLite snapshots pass integrity checks, the encrypted bytes decrypt back to the
created archive, and the tar stream can be read. The status command checks the
record against the current archive size and SHA-256, so a new daily backup does
not depend on rewriting the release qualification record. This byte/integrity
evidence is not a clean-room restore drill; the latter remains a separate
qualification and disaster-recovery check.

## Incident response

1. Capture `--json` output and the timestamp.
2. If `FAIL`, preserve the evidence and compare the release, qualification,
   maturity binding, DB safety, service, and health domains.
3. If `UNKNOWN`, treat the evidence as unavailable and do not authorize a
   deployment or TESTNET action.
4. Do not repair SQLite, restart services, cancel maturity, or provision
   secrets from this status command. Use the relevant approved campaign and
   runbook for any mutation.

## Capacity and retention

The capacity domain reports filesystem headroom against the deployment hard
gate and an advisory warning band. It includes a retention *dry-run plan* with
zero deletions. The backup retention proposal is documented in
`docs/BACKUP_DISASTER_RECOVERY.md`: recent 24, daily 7, weekly 4, monthly 12.
Unknown, corrupt, or unverified artifacts are preserved and never eligible for
automatic deletion.

No destructive retention command is provided by this campaign. Any future
cleanup tool must first produce a deterministic plan, require explicit review,
preserve the latest verified backup and rollback release, and remain separate
from this status command.

## Isolated dated-carry PAPER status

The dated-future experiment is a separate allocation, database and reporting
surface. Its read-only dashboard endpoint is `/api/carry-dated`; the snapshot
path is `BTCQUANT_CARRY_DATED_STATUS_PATH`, defaulting to
`/opt/btcquant/state/carry-dated-experiment/carry_dated_status.json`. The endpoint
accepts only schema version 1 for `strategy=carry_btc_dated` and
`paper_only=true`; unsupported versions or missing contract keys return
`UNKNOWN`. A missing file is `NOT_DEPLOYED`, and a timestamp beyond the existing
20-minute Carry heartbeat bound is `STALE`. No absent value is inferred from
equity, and this experimental capital is excluded from Tandem totals.

`AVAILABLE` means only that the runner checkpoint file is recent and
well-formed. The endpoint separately reads
`/opt/btcquant/state/carry-dated-experiment/collector_status.json` and reports
the collector unit identity, heartbeat age, book-quality/errors, archive
scrub state, data coverage and storage quota. `HEALTHY` describes only a recent
successful public-data poll; it is not a systemd process probe. The endpoint
deliberately keeps overall readiness `UNKNOWN` until an operator independently
checks both unit states, runner/collector DB integrity, settlement backlog and
backup/restore state. The runner is
`btcquant-carry-dated-experimental.service`; the public collector is
`btcquant-carry-dated-collector.service`. Never map the legacy perp collector
to either identity.

The panel distinguishes reserved margin, stress cash and the economic reserve.
Reserved margin is not a booked loss; the reserve is a decision hurdle, not a
ledger debit. It reports official settlement values separately from entry
estimates and does not display perp funding for the dated instrument. Economic
qualification remains `INSUFFICIENT_EVIDENCE` until the registered forward
protocol passes; service availability does not alter that verdict.

## Scheduled read-only observation

The release contains btcquant-ops-status.service and
btcquant-ops-status.timer. Deployment installs these units but does not enable
or start them. After the final PAPER qualification and maturity start have been
reviewed, the operator may explicitly enable the six-hour read-only timer:

    sudo systemctl enable --now btcquant-ops-status.timer
    sudo systemctl start --wait btcquant-ops-status.service
    sudo journalctl -u btcquant-ops-status.service -n 1 --no-pager

The service runs as btcquant, loads no .env, has no write path, and exits
non-zero for FAIL or UNKNOWN. It only writes its JSON result to journald.

## Safety boundary

The status command is not a qualification producer and not a TESTNET
authorization. A healthy report with maturity `WATCH` means the campaign is
running normally; it does not waive the 90-day, order, trade, secret, or human
approval requirements.
