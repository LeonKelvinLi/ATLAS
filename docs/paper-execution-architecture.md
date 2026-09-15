# Paper execution architecture audit

Development baseline: e36ca70. This work is isolated on codex/atlas-paper-execution;
no collector VM, configuration, credentials, validation evidence, or automation
is changed.

| Area | Existing implementation | Gap / decision |
| --- | --- | --- |
| Simulation | Persistent PaperBroker, Coinbase simulator, MultiassetReplay | Keep simulation provenance; simulated fills do not establish broker readiness. |
| Equity paper contract | Strict equity_paper fixture mapping, reviewed terms, monotone fills | In-memory attempts and fixture-only transport; add a separate ongoing paper package. |
| Alpaca pilot | Fixed paper host, TLS, private keys, one durable original and correction attempt, exact-ID recovery | BTC-only, both attempts rejected, exhausted and halted. Preserve all pilot code/state. |
| Execution gateway | AtlasService and Ledger commit before POST, serialize claims, enforce risk and handle ambiguous acceptance | Narrow status/accounting model lacks ongoing equity reconciliation; reuse primitives/patterns without altering existing clients. |
| Orders and fills | Strict cumulative order correlation and partial-fill handling | Need durable normalized broker observations, fill identity, inventory reconciliation and cancel/replace recovery. |
| Risk | Order, portfolio and replay rules exist | The factory lacks authoritative portfolio context; add a complete paper snapshot and durable daily/peak state. Current-equity <=100 rejects gains; bound allocated principal instead. Entry-only limits must not prevent validated reductions. |
| Recovery | Persisted intent/client UUID and missing-lookup guard | Add deterministic signal deduplication, durable reservations, one account writer, startup reconciliation, unknown and unmanaged-order halt. |
| Operations | Research/collector hosting and separate generic service shell | Add bounded paper CLI, kill switch and redacted event trail; no new workers, deployments or collector integration. |
| Qualification | Frozen research rules, immutable archives/holdout and multiasset scorecards | Add evidence-gate reporting without evaluating or tuning frozen strategies or consuming holdout. |

## Implemented boundary

The new atlas/paper package is an explicit equity PAPER engine, separate from the deployed
collector, existing research/simulation code and exhausted crypto connectivity
pilot. Reuse atlas.domain decimal/financial_context and atlas.ledger canonical
JSON/digest. Strategy callers provide reviewed, hash-bound decisions only;
mutation transport requires a one-use engine authorization after durable risk
reservation. All real transport URLs are fixed to Alpaca PAPER/data origins.
There is no LIVE configuration or live execution route in the new package.

Use long-only U.S. equities, regular-session DAY limit orders, allocated principal
at most $100 and smaller explicit policy limits. Broker state is authoritative;
an unresolved discrepancy blocks new execution. Commit intent and reservation
before submission. An ambiguous POST or missing client-ID lookup cannot cause
resubmission. Cancellation acknowledgement is not a final cancellation. Use
cancel/reconcile/new-order replacement with a new deterministic child identity;
never assume a native replacement eliminates fill races.

Authenticated broker qualification remains unestablished. The historical BTC
pilot's 403 has not been resolved here; it is not evidence that a different equity
request will fail. This development uses synthetic broker responses and isolated
local state. The owned API's paper_execution_enabled flag defaults false; an
explicit true still requires fixed PAPER endpoints, a dedicated account, durable
ownership, and engine-issued authorization. No broker session was activated.
