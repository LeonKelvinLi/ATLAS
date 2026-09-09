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

## Priorities before configuring a host

| Priority | Finding in the current code | Required engineering work |
|---|---|---|
| 1. Explicit operation mode and artifacts | [`supervisor.py`](../supervisor.py) adds `--trade` unless `--paper` is supplied. [`live_h2_runner.py`](../live_h2_runner.py) selects the newest matching H2 filename and treats missing H3/H4 models as optional. | Require an explicit mode and validate exact model hashes, training cutoff, feature contract, dependencies, and required configuration before launching workers. Restore the approved artifacts; do not retrain frozen hypotheses to replace missing files. |
| 2. Predictable shutdown and host handover | The supervisor handles `KeyboardInterrupt` but has no `SIGTERM` handler. Collector subprocesses run through a background thread; the runner has no outer shutdown/reconciliation `finally`. Local socket locks cannot coordinate two hosts. | Add bounded signal handling and child-process cleanup, cover them with fake-process tests, and define a single active host for the account before migration. |
| 3. Durable data, health, and backups | Databases, models, and logs live under the code checkout. [`backup_state.py`](../backup_state.py) assumes a OneDrive desktop directory and includes `.env`. Health/recovery tools call Windows Task Scheduler, and several shell wrappers assume macOS. | Configure persistent state and backup locations independently of releases. Adapt health and recovery to the chosen server OS; define credential storage and offsite backup behavior explicitly. |

Choose the server OS and hosting provider before implementing their service
configuration. A local offline test cannot verify broker connectivity,
model restoration, process restart behavior, persistent storage, or backup
recovery on that host.

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

Local verification on September 9, 2026: `uv sync --locked --group dev`
succeeded; `uv run --locked python -m pytest -q -ra` reported **312 passed,
2 skipped**. The skips require the optional broker library and an explicitly
selected dataset, respectively. The generated-data demo passed all five
integrity checks. Static undefined-name checks and `git diff --check` also
passed. Server behavior and the GitHub Actions run remain unverified.

The frozen protocol and research records remain the reference for any
future evaluation: [`FORWARD_TEST.md`](../FORWARD_TEST.md),
[`FINDINGS.md`](../FINDINGS.md), and
[`RESEARCH_QUEUE.md`](../RESEARCH_QUEUE.md).
