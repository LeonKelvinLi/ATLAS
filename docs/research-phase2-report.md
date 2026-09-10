# ATLAS research phase report

September 9, 2026. Scope: offline development in `/Users/leon/Desktop/dev/ATLAS`,
continuing from `db01ee0`. This report distinguishes implemented code, verified
engineering behavior and missing market evidence. No strategy is qualified for
funded trading.

## A. Executive judgment

ATLAS is now a more defensible research and simulation platform. It can reserve,
run, attack, inspect and reproduce a small experiment while keeping research
separate from execution authority. It still lacks the real data and prospective
record needed to establish edge. These are judgment scores, not measured audits
or guarantees, and intentionally remain low where evidence is missing.

| Dimension | Score /10 | Reason |
|---|---:|---|
| Architecture | 7 | Clear data/features/registry/replay/diagnostics/health boundaries and one service risk gate; legacy and new research still coexist. |
| Research integrity | 6 | Point-in-time contracts, complete planned attempts, immutable lineage, overlap seals and source guards; provider authenticity and untouched evaluation remain unestablished. |
| Backtesting realism | 5 | Partial fills, shared volume, costs, latency, reservations and explicit terminal inventory; completed-bar close proxy lacks real order-book/queue and corporate-action realism. |
| Strategy evidence | 1 | No eligible real-data experiment or independently validated alpha. |
| Risk controls | 7 | Deterministic order controls plus optional fresh portfolio/health vetoes and exact policy binding; production provider and calibrated limits remain absent. |
| Execution reliability | 6 | Durable intents, restart/uncertainty checks and replay recovery tested offline; no real server or broker conformance evidence. |
| Security | 6 | Read-only research MCP, bounded files/outputs, strict paths, corruption checks and no executable config; host hardening and authenticated production transport untested. |
| Observability | 6 | Saved experiment/fill/equity/health histories and bounded reports; no operating host, alert delivery or prospective drift record. |
| Production readiness | 3 | Inert deployment preparation and locked tests exist; host, storage recovery, access controls and feeds need a real rehearsal. |
| Funded-trading readiness | 1 | Live factory refuses; strategy, data, account integration and operator activation requirements are unresolved. |

## B. What the audit found

The [pre-change forensic audit](research-forensic-audit.md) records ranked findings
against the starting commit. The main weaknesses were lost publication/revision
metadata; inspectable holdout labels; unlimited/immediate legacy fills; fee
assumptions changing across paper restart; exit-strategy attribution over pooled
cost bases; unregistered benchmark attempts; incomplete source hashes; dropped
historical variant counts; and unbounded/nonfinite evidence projections.

Historical findings remain material: `research_pooled.py` filters future-label
NaNs before building a basket, frozen feature-v1 volume logic inspects a segment,
and `research_best.py` selects using held-suffix performance. These legacy results
cannot become untouched validation by renaming them. The frozen artifacts and
one-time evaluator were preserved rather than retrained or recertified.

Independent review during implementation also caught incorrect health input
shape, a failure-code mismatch that could leave attempts reserved, confusing
cost-axis semantics, same-clock feature duplication, asynchronous baseline entry
omissions, missing currency/volume units, incomplete source-import guards,
symlink normalization, and tamperable holdout key sets. These received concrete
fixes and regression coverage.

## C. What changed

| Area | Implementation |
|---|---|
| Canonical data and quality | `atlas/research/market.py`: explicit publication/acquisition/session/revision/currency/volume contracts, bounded strict JSON, provenance/quality reports and alias-resistant observation identity. |
| Causal features and splits | `point_in_time.py`, `splits.py`: eight versioned descriptors, per-value availability/input hashes, session/gap warmups, anchored/rolling timestamp ranges, label-resolution purge and embargo. |
| Immutable research | `store.py`: atomic attempts/results, failed/unfinished counts, hash-verified immutable exports and single-use operational holdout claims with overlap protection. |
| Simulation | `replay.py`: later-publication market/limit matching, explicit cost scenarios, partial fills, shared volume, reservations, cancellation/expiry, strategy sleeves, event/equity history and exact restart. |
| Research attacks | `diagnostics.py`, `lab.py`: fixed interpretable hypotheses, controls/ensemble, additional-cost sweep, delays/parameter/window perturbations, regime summaries, concentration checks, conditional block resampling, multiple-comparison diagnostics and conservative scorecards. |
| Health and risk | `health.py`, `atlas/risk.py`, `atlas/service.py`: descriptive drift, sticky disable history, optional fresh portfolio projections/permissions before each risk check, policy fingerprint binding and honest legacy P&L attribution. |
| AI and configuration | `interface.py`, `atlas/__main__.py`, `mcp_server.py`, `config.py`, `evidence.py`: run/reproduce/inspect/inventory CLI, 20 conditional read-only saved-report tools, bounded historical evidence and no config-loaded provider/code. |
| Existing paper compatibility | `atlas/brokers/paper.py`: pinned fee/model metadata and monotonic mutation clock; ambiguous older state refuses automatic migration. |
| Documentation and CI | New audit/inventory/workflow/report, updated README/server preparation, and a temporary synthetic reproduction step in CI. No remote Actions run is claimed. |

The detailed contracts and commands are in [research-workflow.md](research-workflow.md).
This phase adds no broker or forecast model. Fixed rules provide interpretable
controls; adding supervised/deep models before recovering credible history would
increase selection risk without establishing evidence.

## D. Research validity

Trusted within tested assumptions: causal feature prefix invariance, timestamp
contracts, exact accounting invariants, deterministic replay/restart, immutable
attempt/result integrity, overlap blocking and exact artifact reproduction.

Not established: authentic historical availability; survivorship/corporate-action
correctness; exchange queue or executable-close realism; independently profitable
strategies; calibrated forecasts; complete historical multiple-testing correction;
or production operational reliability. `SYNTHETIC` and `TEST` remain engineering
evidence. Declared real data remains `HISTORICAL_UNVERIFIED`; the current loader
cannot self-certify a provider.

The primary hypothesis is selected in the plan. Only it receives the full gauntlet.
Other candidates have explicit partial scorecards. Folds replay fixed rules from
fresh cash in chronological test ranges; purge indices are fitting/label
diagnostics, and no trained-model superiority is implied. Event-block bootstrap
fractions are conditional distributions, not future loss probabilities. Sparse
Sharpe/tail/holding-period metrics stay unavailable; realized sell fills are not
independent round trips. Conditional exposure/random controls are not exactly
matched realized-risk benchmarks. None of these outputs authorizes an order.

## E. Data inventory

The bounded [data inventory](data-inventory.md) recovered a 2,400-row generated
parquet example and two legacy EURUSD PRACTICE snapshots containing 720 overlapping
observations across 1m/5m/15m. There are conflicting duplicate snapshots and six
unfinished endpoints; publication/revision semantics are not authenticated. These
fragments also do not match the USD spot/equity replay contract. The full historical
market database and referenced run bundles were absent from the searched locations.

The registry contains 27 historical records declaring 154 variants; the separate
experiment ledger contains 24 records. Those counts are retained as historical
selection context, not added to or conflated with new-store trial counts. Three
frozen models, journal, protocol, findings and both historical ledgers are unchanged.
No bulk dataset was downloaded or broker data session started.

## F. Strategy evidence

**None demonstrate sufficient evidence.** No real-data winner was manufactured.
The saved synthetic example exercises four rule families at two lookbacks and a
primary gauntlet with cash/buy-and-hold/conditional controls, equal-weight ensemble,
cost scenarios, perturbations and fold tests. Its curve is generated, and its
status cannot exceed `EXPERIMENTAL` regardless of profitability. A complete
engineering bundle is retained outside the repository; exact identity and
verification results are recorded in the final verification section below.

## G. Remaining blockers

| Stage | Concrete blocker |
|---|---|
| Research | Recover/licence suitable history for one declared instrument/universe; authenticate publication/revisions and units; validate calendars/actions/universe; pre-register the comparison family and protect genuinely untouched later data. |
| Paper | Connect a reviewed feed through a trusted observation adapter, establish volume/quote/cost assumptions, assemble durable state and a complete portfolio/health provider, then accumulate prospective observations. Retrospective replay is insufficient. |
| Shadow | Capture contemporaneous signals and executable quote opportunities without broker writes; freeze strategy/model/data identities, measure stale/rejected opportunities and implementation divergence through time. No prospective feed is running. |
| Deployment | Select and explicitly authorize a Linux host; rehearse actual process termination, disk-full, restore, authentication, monitoring and single-writer controls. Inert service files are not a deployment. |
| Funded | Sufficient independent net evidence plus all prior stages, official authenticated broker/account mapping, complete trusted account/market/risk data, operator-reviewed limits and concrete final activation. Live factory still refuses. |

## H. Tests and reproducibility

| Exact command/check | Result |
|---|---|
| `uv sync --locked --group dev` | Success; 78 packages resolved, 67 checked. |
| `uv run --locked python -m pytest -q -ra` | **1,179 passed, 2 skipped in 24.22 seconds** on the final implementation. |
| `uv run --locked python demo.py` | Five integrity checks passed on 2,400 generated candles; engineering output only. |
| `uv run --locked python -m atlas demo` | Success; in-memory synthetic paper example, `real_orders: 0`, `files_created: false`. |
| `uv run --locked python -m atlas research demo` | Success; 37 predeclared/evaluated trials, exact artifact reproduction true, temporary outputs removed, no submission. |
| `git diff --check` | Clean. |
| SHA256 comparison of eight frozen files against pre-change hashes | All eight match. |

The two skips are intentional: the optional `iqoptionapi.constants` dependency is
not installed, and no private `ATLAS_TEST_DATASET` was selected. The earlier full
run exposed one paper-fee fixture mutation; the fixture now supplies its fee
assumption at construction, preserving the pinned-model protection. The final
suite includes that regression and passes. Targeted undefined-name static checks
also passed; the full suite includes the repository's source lint guard.

Coverage includes future-value/revision invariance, malformed/gapped/session data,
missing currency/volume, overlap seals and tampering, concurrent reservation,
failed/unfinished counts, reproduction mismatch, partial fills and restart,
cash/inventory and per-strategy costs, authority/health/risk vetoes, fresh
account/quote binding, policy changes, stale data and source drift. Tests use
generated data and fake clients; they do not contact brokers or establish market
profitability. Retained example identity is appended after the local commit.

## I. Security findings

Research configuration cannot load code, provider callbacks or AI-authored risk
snapshots. Missing trusted portfolio data blocks configured limits. The optional
20 research tools only read saved, bounded reports; they cannot execute experiments,
unlock a holdout, change policy/health, create approval or promote a strategy.
Legacy disposable benchmarks are disabled when a research store is configured.

Registry integrity checks include manifests/results, protected-key-set digests,
full holdout records and foreign keys. Aliases, revised prices, subsets, added
rows and venue relabeling cannot bypass protected asset/symbol/time keys. This
deliberately overblocks cross-venue data. A trusted instrument registry is still
needed to reason about arbitrary symbol remapping. An administrator rewriting
code and state remains outside the hash-corruption threat model.

Owned external stores, immutable exports, bounded/nonfinite JSON validation,
nonblocking file checks and symlink refusal reduce accidental corruption and
path misuse. Production authentication, host ownership, encrypted coordinated
backups, signer custody and actual network transport remain separate work.
No secrets were added, no approval capability was given to MCP, and no real account
was accessed in this phase.

## J. Git state

Work is on `codex/atlas-offline-onboarding`; implementation and final verification
are committed locally. Exact commits and ahead/behind state are recorded in the
verification section. Nothing is pushed. The eight frozen files are byte-verified
against the pre-change baseline.

## K. Ranked next work

| Rank | Improvement | Expected value | Risk reduction | Research importance | Effort |
|---:|---|---|---|---|---|
| 1 | Recover a small licensed, auditable dataset for one defined instrument/universe | Very high | High | Essential | Medium |
| 2 | Verify timestamps, revisions, units, actions and historical universe against provider records | Very high | Very high | Essential | High |
| 3 | Pre-register the complete search family, acceptance criteria and a new untouched later period | High | Very high | Essential | Medium |
| 4 | Run matched simple-rule/baseline studies on real history and reject failed hypotheses | Very high | High | Essential | Medium |
| 5 | Calibrate execution assumptions from observed quote/fill microstructure and capacity | High | Very high | High | High |
| 6 | Build the trusted portfolio/health provider with exact account/quote reconciliation | High | Very high | High | High |
| 7 | Accumulate prospective paper/shadow events under frozen strategy identities | Very high | Very high | Essential | Medium engineering; substantial elapsed time |
| 8 | Validate failure/restart/restore on one authorized Linux host with strict single-writer ownership | High | Very high | Medium | Medium |
| 9 | Compare prospective outcomes with predeclared expectations; calibrate drift alarms and rejection rules | High | High | High | Medium |
| 10 | Review official authenticated broker schemas and transport only after evidence and operational gates survive | Conditional | High | Low until evidence exists | High |

Avoid new model families, optimization layers or dashboards until these steps
resolve the evidence gaps they would otherwise hide.
