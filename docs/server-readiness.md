# Server readiness

ATLAS will run on a server. The Desktop checkout is for development and
offline verification. No application service, startup job, broker session,
or host migration has been configured by this change.

## Current implementation

The repository includes a Python supervisor, collector, live runner,
reconciliation tools, DuckDB candle storage, SQLite journal, JSONL logs,
and desktop-oriented health and backup scripts. Three historical model
pickles and a small legacy journal are tracked, even though `.gitignore`
matches them. The full market database, live execution logs, broker
dependency, and credentials are absent. The included snapshots still need
provenance and compatibility checks before resuming a research run.
Historical notes do not establish whether another host is currently running.

The new `uv.lock` and offline checks provide a reproducible development
baseline. They are not certification of a production host or compatibility
with an old serialized model.

## Runtime paths

[`runtime_paths.py`](../runtime_paths.py) separates code from operational
state. Set these variables in the process environment **before starting
Python**. Path selectors inside `.env` are ignored so children cannot inherit
different paths from their parent. Inspect the resolved values with
`uv run --locked python runtime_paths.py`; this only prints paths and
creates nothing.

| Variable | Default | Purpose |
|---|---|---|
| `ATLAS_STATE_DIR` | Code checkout | Market database, journal, runtime logs, and feature cache |
| `ATLAS_MODEL_DIR` | State directory / `models` | Model bundles read by the runner, monitoring, and evaluator |
| `ATLAS_ENV_FILE` | State directory / `.env` | Optional credential/configuration file; process values take precedence |
| `ATLAS_BACKUP_DIR` | Unconfigured | Explicit backup destination; the backup CLI also accepts `--destination` |

Overrides must be absolute paths. Selecting an external state directory does
not move existing files or copy the repository's journal/models. Restore
approved state deliberately. Python operational tools use these paths;
historical shell wrappers and research ledgers/output conventions have not
been converted into a server deployment system.

## Start and stop behavior

- The supervisor requires `--mode paper`, `--mode practice`, or `--mode real`;
  the explicit `--paper` alias still works. Missing or conflicting mode
  arguments fail before the lock, logs, or child processes are created.
- Real mode requires `IQ_ALLOW_REAL=1` and an explicit finite positive
  `--amount` in the account's currency. Practice defaults to one
  account-currency unit; paper rejects an amount. The selected environment
  file is read before configuration validation, with process values taking
  precedence. The supervisor forwards the exact mode and stake to its child.
- SIGTERM/SIGINT stop new supervisor work and request termination of tracked
  runner and collector children. Cleanup has shared wait/kill deadlines.
  Collector processes that survive cleanup must not overlap a later cycle.
- The runner stops new decisions and orders when shutdown is requested.
  An already-started broker call cannot be cancelled by cancelling its
  Python wait. It allows a short acknowledgement grace, records an unknown
  submission when acknowledgement is unavailable, and preserves known
  unresolved IDs for later reconciliation.
- Ordinary completion retains the existing settlement wait, which can
  outlast `--minutes`. A shutdown request interrupts that wait and gives
  reconciliation a shared eight-second budget, followed by bounded
  disconnect. The watchdog stops before reconciliation. These are code
  deadlines; host scheduling and real broker behavior still need testing.
- Watchdog recovery and dashboard startup require separate `--recover` and
  `--ensure-dashboard` flags. Default checks log health without starting
  ATLAS applications. Task restart support remains Windows-specific and
  reports unavailable elsewhere.
  `--recover` invokes historical task definitions whose execution mode is
  not established by this change; it is not a REAL-mode launch interface.

## Real execution and recovery

Real execution is implemented as an explicit future server option. This
development pass keeps all applications and account sessions stopped: it
does not activate funded use, change credentials, connect to the broker,
or submit orders. The default example configuration remains PRACTICE with
real execution disabled. The unofficial
[`iqoptionapi`](https://github.com/iqoptionapi/iqoptionapi) maintainers caution
against real-account use; local fake-client checks cannot establish broker
reliability or a profitable strategy.

The runner selects the requested balance at startup and verifies it again
immediately before every order. Unknown or unexpected balance state prevents
submission. Amounts are in the account's currency, not a hardcoded US-dollar
denomination. Mode checks cannot coordinate multiple hosts or guarantee that
an account is not controlled elsewhere.

Real records are isolated beneath the selected state's `logs/real/`
directory, including `live_h2.jsonl`, `live_h2_heartbeat.jsonl`,
`cross_section.jsonl`, and `supervisor.log`. Historical paper/practice logs,
frozen model artifacts, and the registered evaluation paths remain separate.
Monitoring selects these records with `ATLAS_MONITOR_MODE=real` in the
process environment or `--mode real` on `status.py`/`dashboard.py`. Selecting
records for observation does not enable orders and needs no stake or opt-in.

Before submitting a real order, the runner persists
`logs/real/order_submission_pending.json`. If an acknowledgement is unknown
or the process crashes during submission, that marker remains. A runner
blocked by unresolved submission exits with status 3; the supervisor stops
instead of relaunching it. It also checks the marker after any other runner
exit and before each launch, so a hard kill cannot bypass this boundary.
This durability boundary requires a filesystem that supports opening and
syncing directories as well as files. Failed directory synchronization
blocks submission or preserves the pending marker; it is never silently
bypassed. Verify support on the selected server OS and storage before use.

Reconciliation is a manual operator task: locate the exact submission in
broker history, preserve its order ID and verified outcome (or establish
that it was never accepted), and reconcile the operational record before
clearing a remaining marker. An absent acknowledgement does not establish
that the order was cancelled. Never delete the marker merely to make a
restart succeed. Known unresolved order IDs still require outcome recovery;
the marker specifically tracks a submission whose acceptance was not
durably established.

`settle_missing.py --mode real` selects real records for broker-history
reconciliation and requires `IQ_ALLOW_REAL=1`; it requires no stake and never
places an order. Its `--dry-run` option inspects local records, but keeps the
same explicit real opt-in requirement. A pending marker is reported as an
unresolved condition and is never automatically cleared by this tool.

`selfcheck.py` accepts `--mode` (paper by default) and `--amount` to inspect
the intended configuration without activating it. Real checks require the
opt-in, explicit stake, absence of a pending submission marker, and the
real signal log in the selected backup. Its existing broader audit also
checks host services, data, model provenance, and git state. A configuration
check passing does not establish current broker balance or account identity.

## Backups

Backups require an explicit absolute destination and read from the selected
state directory. `.env` is excluded; credential storage/recovery must be
configured separately. SQLite journals use the backup API, including paths
with URI punctuation, so committed WAL data is retained. The historical
plain-copy fallback for non-readable/non-SQLite journal files remains.

This script copies the listed operational logs and SQLite journal. It does
not snapshot `market.duckdb` or archive model bundles; the host backup plan
must cover those separately. Broker retention is limited, so do not assume
the full accumulated market history can be downloaded again.

Real logs and order-reconciliation records preserve their `logs/real/` tree
inside the backup destination, including a pending submission marker when
present. Historical paper/practice backup files retain their flattened
names, such as `logs_live_h2.jsonl`. Real self-checks inspect
`logs/real/live_h2.jsonl` in the backup, keeping those evidence streams
distinct during backup and restore as well.

A copied local directory is not proof of offsite replication. Scheduling,
retention, encryption/access permissions, and a restore drill belong to the
eventual host configuration.

## Remaining before deployment

1. Select the server OS and provider, then implement and test its service
   manager, health integration, persistent volumes, and backup destination.
   No such services or startup jobs are configured by this change.
2. Pin and verify exact model hashes, cutoff/provenance, feature contracts,
   and dependency compatibility in a startup preflight. Model selection
   still uses newest matching filenames, and missing H3/H4 bundles remain
   optional. Restore approved artifacts; do not retrain frozen hypotheses
   to replace missing files.
3. Confirm a single active host for the account before migration. Local
   socket locks cannot coordinate two servers. Reconcile unresolved or
   unknown broker submissions during handover; tests cannot establish that
   an in-flight order was cancelled.
4. Verify the actual host with restored state, restart/shutdown tests,
   PRACTICE-account connectivity, and backup recovery before considering
   funded activation. The local synthetic checks establish code behavior,
   not hosted operation or the suitability of the unofficial adapter for
   funded use.

## Work completed in this development pass

- Added a generated-data demo with storage round-trip and chronological-purge
  checks; it neither starts workers nor accesses research datasets.
- Added a portable dependency lock and a GitHub Actions workflow for offline
  tests. The workflow has not been run on GitHub as part of this local change.
- Fixed calibration on a training fold containing one class.
- Rejected overlapping candles and invalid bar spacing before feature use.
- Made private-data smoke tests opt-in and read-only, isolated supervisor
  lock tests, and made failed lint-tool execution fail the tests.
- Rewrote the README to distinguish historical research reports from
  independently reproducible evidence and current operational status.
- Added explicit supervisor modes, graceful process cleanup, external
  runtime paths, selected backup destinations, and opt-in watchdog actions.
- Added fake-client/process lifecycle tests and external-state integration
  tests, including SQLite WAL restoration and environment precedence.

Latest local verification on September 9, 2026:

- `uv sync --locked --group dev` succeeded.
- `uv run --locked python -m pytest -q -ra`: **484 passed, 2 skipped**.
  The skips require the optional broker library and an explicitly selected
  dataset. Lifecycle tests use fake clients/processes and captured handlers.
- Final targeted execution, shutdown/durability, REAL records, MCP balance,
  and self-check regression run: **118 passed**. This also covers refinements
  made while the full suite was running; counts overlap and are not additive.
- `uv run --locked python demo.py`: **five integrity checks passed** on
  2,400 generated candles.
- Static undefined-name checks and `git diff --check` passed. Tracked model
  bundles, the legacy journal, and frozen research records are unchanged.

Actual OS signal delivery, broker behavior, server operation, and the GitHub
Actions run remain unverified.

The frozen protocol and research records remain the reference for any
future evaluation: [`FORWARD_TEST.md`](../FORWARD_TEST.md),
[`FINDINGS.md`](../FINDINGS.md), and
[`RESEARCH_QUEUE.md`](../RESEARCH_QUEUE.md).
