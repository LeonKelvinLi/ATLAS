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

- The supervisor requires `--mode paper` or `--mode practice`; the explicit
  `--paper` alias still works. Missing or conflicting mode arguments fail
  before the lock, logs, or child processes are created.
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
   broker connectivity and PRACTICE mode, and backup recovery. The local
   synthetic checks establish code behavior, not hosted operation.

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
- `uv run --locked python -m pytest -q -ra`: **380 passed, 2 skipped**.
  The skips require the optional broker library and an explicitly selected
  dataset. Lifecycle tests use fake clients/processes and captured handlers.
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
