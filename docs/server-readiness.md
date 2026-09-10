# Server readiness

ATLAS is prepared for further Linux integration work. **No service, broker session, listener, scheduled job or server deployment was started during this development pass.** The Mac checkout remains for development and offline verification.

## Current platform boundary

`atlas/` owns the new research/MCP/proposal/risk/execution architecture. The CLI factory supports an unconfigured research broker and an explicitly funded **synthetic** paper simulator. Both real broker selections refuse before creating state. The live-assisted and live-auto service paths are tested with fake adapters; that is not production readiness.

Research defaults to unavailable account data, not a generated portfolio. The default CLI prints help. The offline `python -m atlas demo` uses in-memory synthetic data. `config-check` validates an explicit external configuration without creating state or contacting a broker. Actual MCP startup is a separate intentional command and was not run here.

The [Alpaca paper connectivity pilot](alpaca-paper-pilot.md) is a separate explicit
CLI with pinned paper REST endpoints, one irreversible $2 test-order attempt,
and one explicitly recorded $2 correction after a proven refusal. Both attempts
were rejected; the pilot is halted and its broker lifecycle test remains incomplete.
It does not activate this service factory, a strategy, worker, listener or host
deployment. Its provider receipt is separate from the offline verification and
from general broker or live readiness.

The complete inert examples and configuration contract are in [`deploy/README.md`](../deploy/README.md), [`research-config.example.json`](../deploy/research-config.example.json), and [`atlas.service.example`](../deploy/atlas.service.example). These are source files, not installed host configuration.

## Configuration, state and secrets

The [offline research workflow](research-workflow.md) now has a separate external
`research_store/research.db` and immutable exported experiment bundles. Replay
checkpoints and sticky strategy-health databases also belong outside the checkout.
None are a live execution journal. Optional `portfolio_risk` configuration adds
server-owned risk gates but cannot supply a provider or snapshot; a trusted
provider must be assembled separately, and its absence blocks readiness/orders.
The legacy paper schema now pins fee/model assumptions and the last mutation
clock. Older simulator state refuses automatic migration rather than inventing
its historical fee contract.

The new platform uses an explicit external JSON configuration; it does not autoload `.env`. Configuration includes mode, broker selection, persistent state directory, dataset/quote fixture paths, deterministic risk settings and optional HTTP settings. Secret inputs are external owner-only files selected by environment variable; no account number or key is included in examples.

| Selection | Purpose |
|---|---|
| `state_dir/research/execution.db` | Research scope |
| `state_dir/paper/execution.db` | Paper proposals, attempts and audit events |
| `state_dir/paper/simulator.db` | Persistent synthetic cash, positions and broker orders |
| `state_dir/live/execution.db` | Shared live-assisted/live-auto scope, preventing mode changes from escaping unknown orders |
| `state_dir/KILL_SWITCH` | Presence blocks new orders; an unreadable path also blocks |
| `ATLAS_HTTP_BEARER_FILE` | Owner-only HTTP bearer secret, at least 32 bytes |
| `ATLAS_EXPECTED_ACCOUNT_FILE` | Expected account identity for future live assembly |
| `ATLAS_APPROVAL_KEY_FILE` | Reserved verifier-key path for future live assembly |
| `ATLAS_LIVE_TRADING_ENABLED=true` | Independent future live opt-in |
| `ATLAS_LIVE_AUTO_ENABLED=true` | Additional automatic-mode opt-in |

Paths must be absolute and outside the checkout. Research and paper cannot open a live-scoped journal. Existing empty, malformed or inconsistent journals/simulator databases refuse rather than recreate money or discard pending orders. SQLite WAL/FULL synchronization and directory fsync are part of the submission boundary; unsupported or failing durable storage must block operations.

## Startup, shutdown and observability

The HTTP wrapper requires a bearer credential on every route, binds only loopback, rejects non-loopback peers, bounds bodies and disables access logs. The example systemd unit uses a dedicated user, restricted writes and networking, restrictive umask and bounded shutdown. A remote authenticated gateway, TLS, operator identity, resource limits and host enforcement remain separate work.

`/healthz` checks journal integrity. `/readyz` reports mode, kill/pending state and known blockers without probing a broker or loading model pickles. Research can be healthy while not execution-ready. Readiness never certifies live connectivity; live preparation remains blocked. Operational health and broker readiness are separate evidence.

SIGINT/SIGTERM set the service stop event before server cleanup. A broker call already started in a thread cannot be cancelled by stopping the waiting code. Timeout/shutdown leaves the intent unknown until exact broker history is reconciled. Tests use fake clients and captured signal handlers; real Linux signal/process/storage behavior remains untested.

SQLite audit events retain proposal IDs, timestamps, caller labels, immutable strategy/evidence/risk, client UUIDs and observed outcomes. The eventual host should monitor unknown/pending duration, rejected attempts, storage failure, readiness changes and feed freshness. Those alerts are not installed. Journald rotation/retention must be configured on the selected host; no local rotation job was created.

## Recovery and backups

Submission sequence: verify proposal and approval, commit intent/client UUID, refresh account/quote/risk and approval, submit once, validate correlation/fills, commit observed result. Repeated requests return the existing intent. Unknown acceptance blocks the account. A missing history result is insufficient evidence to clear it. Known accepted and partial orders also remain pending until terminal reconciliation.

Cancellation uses a separate proposal/approval and durable child intent. An accepted cancellation request is not proof of cancellation. Fills can arrive while cancellation is pending. Target and child completion update atomically; cumulative acknowledged fills must not decrease, including across concurrent reconcilers.

`Ledger.backup()` uses SQLite's online backup API to an explicit external destination and checks the copy; it never falls back to copying damaged bytes. Paper simulator cash/order state needs a consistent SQLite snapshot too. Cross-database backup coordination, encryption, offsite retention, model/data provenance and a restore drill remain host work. No backup schedule was configured.

A ledger restored to a time before a broker submission can no longer establish uniqueness by itself. Before any resumed broker write, reconcile the restored state with authoritative broker history and establish one active host/account writer. Do not delete unknown intents or seed a fresh database to make startup succeed.

## Legacy compatibility and quarantine

The historical FX-binary collectors, journals and evaluation rules remain in the root modules. Legacy `supervisor.py --mode real` and `live_h2_runner.py --mode real` now refuse before reading configuration, loading models or creating processes. `IQ_ALLOW_REAL=1` does not bypass that quarantine. Legacy order-placement/cancellation Python helpers are retained for compatibility tests but are not advertised or callable through the old `server.py` MCP registry. Legacy paper/practice operations still require a separately authorized broker session; none ran here.

Root operational tools retain their existing external selectors:

| Variable | Legacy role |
|---|---|
| `ATLAS_STATE_DIR` | Market database, journal, logs and cache |
| `ATLAS_MODEL_DIR` | Historical model directory |
| `ATLAS_ENV_FILE` | Selected optional environment file |
| `ATLAS_BACKUP_DIR` | Explicit backup destination |

`python runtime_paths.py` only reports paths. These selectors are distinct from the new platform JSON configuration. Monitoring/reconciliation can still select historical `logs/real/`; observation does not enable trading. The old backup utility has different coverage and legacy fallbacks; do not use it as proof of new-platform recovery. Old `selfcheck.py` inspects host/data/model state and is not the new lightweight health endpoint. Historical Windows wrappers and `CLAUDE.md` describe earlier hosts, not present deployment instructions.

## Before a research/paper server deployment

1. Select Linux host/storage and a dedicated user; review the service unit and install locked dependencies there.
2. Provide explicit external configuration, synthetic or verified dataset provenance, paper assumptions and owner-only authentication secrets.
3. Configure private client access, monitoring, retention and consistent encrypted backup/restore.
4. Test real OS termination, disk-full behavior, persistence/restart and authentication on that host, with paper/fake execution only.

## Additional blockers before funded trading

1. Owner-controlled official broker consent and authenticated schema review; verify the exact account and supported product.
2. Implement the actual narrowly mapped network transport and complete account/market provider. Crypto REST currently lacks required verified cash/equity/daily P&L/liquidity/session inputs; Agentic schemas are not yet mapped.
3. Bind a validated signal provider to approved artifact hashes, feature version, asset/target/horizon, as-of data and calibration evidence. The supplied baseline registry cannot self-promote to live.
4. Produce sufficient fresh, cost-aware, independent evidence against simple baselines. The current model does not have that evidence.
5. Replace or harden the shared-key approval trust arrangement for production, validate broker idempotency/history semantics, and establish a single account writer with reconciliation and rollback drills.
6. Have the owner approve the concrete account, limits, funding, host and activation after those requirements are met.

See [`platform-report.md`](platform-report.md) for exact local test results and the final implementation inventory. Local tests do not establish server operation, broker connectivity, investment profitability or a GitHub Actions run.
