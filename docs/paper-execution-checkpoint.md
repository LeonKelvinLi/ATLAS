# Isolated paper execution work in progress

Paused September 15, 2026 at approximately 04:43 UTC when a separate ClauseCat
launch brief arrived. Project-routing clarification is pending. This is an
incomplete development checkpoint, not a working release or paper-readiness pass.

- Worktree: /Users/leon/Desktop/dev/ATLAS-paper-execution
- Branch: codex/atlas-paper-execution
- Base: e36ca7085d2b5925d4b62f765df6681d17b9e779
- Original checkout/main, cloud VM, collector configuration/state, credentials,
  validation evidence, and validation automation were not changed by this work.
- No authenticated broker calls, orders, funds, publishing, or deployment.

## Current files and verification

| Component | Current state |
| --- | --- |
| Architecture audit | docs/paper-execution-architecture.md completed before implementation. |
| Broker | atlas/paper/broker.py plus tests/test_paper_execution_broker.py. 68 focused tests passed before subsequent small hardening edits; rerun needed. Fixed PAPER/data origins, mutation gate, strict normalized snapshots, unknown-response handling, no mutation retries, rate limiting. Actual network writes remain blocked pending provider resolution/review. |
| Risk | atlas/paper/risk.py plus tests/test_paper_execution_risk.py. Latest 112 tests passed. Includes corrected buying-power reservation accounting and hard per-order cap on exits. |
| Qualification | atlas/paper/qualification.py, corresponding test file, docs/paper-qualification.md. Latest targeted group: 114 passed including existing multiasset/diagnostics tests. Independent review/full suite pending; authenticated qualification remains false. |
| Models/journal | atlas/paper/models.py and journal.py written; adversarial journal/model tests saved but not run. |
| Engine | atlas/paper/engine.py PARTIAL: constructor, initialization, reconciliation, risk-state and snapshot helpers. Missing authorization, submit, cancel, replacement, halt/status and final exception/recovery integration. It is not usable yet. |
| Synthetic broker | atlas/paper/simulation.py draft saved; unverified, tests not yet created. |
| CLI and integration | Not implemented. Full repository suite not run for this branch. |

Test group totals overlap; do not add them or substitute them for a full-suite result.

## Resume priorities

1. Run focused broker/risk/qualification/journal tests and syntax-check the new files.
2. Fix remaining models review finding: cumulative fill notional must not regress;
   unchanged filled quantity must retain average price, and incremental fill price
   must obey the intent limit. Exact order type correlation and missing-lock
   creation on journal reopen were already corrected, awaiting journal tests.
3. Fix initialization's account stability comparison: compare economic/identity
   fields rather than observed_at timestamps, which naturally differ across reads.
4. Complete the gateway's durable reserve-before-submit, immutable deterministic
   client ID, one-use mutation permit, and recheck after reservation. A fresh
   unsent claim may be excluded only within its original locked invocation; after
   restart even an ambiguous pre-send crash requires reconciliation and cannot
   resubmit automatically. Do not let the fresh-claim recheck accidentally block
   its own intent as an old unknown, or exempt old claims from reconciliation.
5. Complete exact cancellation and cancel/reconcile/new-child replacement;
   preserve partial fills and outstanding reservations through all ambiguous I/O.
6. Add end-to-end process-crash, restart, ambiguous accepted-then-timeout, duplicate,
   partial-fill, stale-data, API/rate-limit, position/fill mismatch, kill-switch,
   durable-storage-failure, and direct-adapter-bypass tests using synthetic state.
7. Add bounded CLI commands and operator documentation. Keep the exhausted older
   BTC pilot and its 403 evidence unchanged; do not call its journal or endpoints.
8. Perform independent review, required uv sync --locked --group dev and complete
   uv run --locked python -m pytest -q -ra. Report exact passed/failed/skipped.
9. Commit logical completed milestones; do not push/merge/deploy this development
   into the ongoing collector validation.

A complete paper experiment remains blocked on provider diagnosis and explicit
activation review. Local fixtures cannot prove accepted broker orders or strategy
profitability. Frozen research/evaluator/holdout files remain untouched.
