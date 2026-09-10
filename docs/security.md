# ATLAS security and execution trust model

This is a source-level and offline-test review, not a penetration test, brokerage certification or deployed-host assessment. No brokerage credentials, consent session, server listener or real order was used.

## Authorities

| Actor/input | Permitted authority |
|---|---|
| AI/MCP caller | Inspect server evidence, request bounded analyses, prepare proposals, present a receipt, request explicitly enabled execution |
| External web/news/PDF/social text | Inert, attributed context only; never policy, probability, executable code or authorization |
| Server configuration and verified providers | Select datasets, account, mode, approved strategy contract and deterministic risk limits |
| Trusted operator | Review the complete immutable proposal and approve its exact hash through a separate CLI/key boundary |
| Broker responses | Evidence requiring strict account, client/broker ID, state, quantity and price validation |
| Durable journal | Records committed intent and acknowledged progress; corruption/uncertainty cannot authorize a retry |

The application assumes a trusted Python host and configuration owner. It does not protect against an administrator who changes source code, edits the database and recomputes its hashes, or steals a signing/verification key. A content digest detects accidental/uncoordinated changes; it is not an externally anchored, tamper-proof audit log.

## AI and prompt injection

No tool accepts executable Python, a model path, a configuration object, a risk override, a manufactured probability or an arbitrary upstream broker operation. Bar/quote inputs come from bounded server-selected datasets, not MCP arguments. An explicit scenario percentage is labeled hypothetical and cannot modify a quantitative signal. Backtests take explicit fee/slippage assumptions and identify simulated fills and missing realism.

External context is bounded to 20 HTTPS-attributed records with bounded text. Output marks it `untrusted_external_data`, includes a content hash, and leaves numeric adjustment unavailable. Extra instruction-shaped fields are ignored; strings do not become callbacks, policy or approval. This code makes no claim that a language model cannot be persuaded by hostile prose; instead, execution remains constrained even if its interpretation is wrong.

The default unconfigured research server does not invent an account, balance or positions. Missing model outputs and performance series are unavailable, not zero. Historical model inventory hashes bytes without unpickling. Historical evidence uses a bounded allowlisted output schema; nested configuration and raw account/credential fields are omitted. The original research files remain historical data, not instructions.

## Approval and mode gates

Research has no proposal/execution tools. Paper requires explicitly synthetic account/quote configuration. Live-assisted needs independently enabled live policy plus an expiring HMAC receipt bound to the full immutable proposal hash, ID and nonce. Live-auto requires its own independent flag and server rules. Neither live mode is assembled by the current CLI factory.

There is no MCP approval issuer. The operator CLI displays the complete stored proposal and exact hash and requires that hash, then writes an exclusive `0600` receipt outside the repository. A receipt cannot change quantity, price, side, strategy version, evidence, account fingerprint, policy or expiry. Execution checks freshness and risk again after committing intent. Account/quote currency changes and revoked or replaced evidence invalidate the approved terms.

HMAC uses a shared verification secret. The verifier could mint receipts if compromised; the scheme is not proof of hardware-backed human presence. The AI caller must have no shell, filesystem or secret access to the trusted operator/verifier context. The full-hash noninteractive CLI option is for that trusted context and does not itself authenticate a human. Production should consider an operator-held asymmetric signing key and independently authenticated approvals.

The official Robinhood MCP, if separately connected with write access, has authority outside ATLAS. ATLAS cannot constrain orders submitted through another application, another host, the broker UI or an independently connected agent. A deployment must decide which component owns account writes; local SQLite locking is not multi-host fencing.

## Order and recovery controls

The kill-switch file prevents new orders. An approved cancellation can still reduce exposure while that file exists; process shutdown stops new broker calls, including cancellations.

Before broker submission ATLAS commits a unique client UUID and durable intent. The database serializes claims across processes. Pending, accepted and unknown orders block new dependent orders conservatively at account scope. Duplicate requests return existing state. No uncertain mutation is retried automatically.

A post-submit timeout, partial/malformed acknowledgement, failed result write or shutdown does not imply rejection. The pending intent survives. Reconciliation validates client and broker correlation and monotonic cumulative fills, including the latest database state inside the write transaction. A history lookup returning nothing never clears the block. A cancel acknowledgement does not undo a fill or prove a queued request completed; cancellations have a separate durable proposal/approval and atomically resolve the original order only on validated terminal evidence.

The paper simulator refuses inconsistent existing state rather than reseeding cash. Both stores require directory synchronization for durability. An error after a possible commit poisons the affected instance until reopening/reconciliation. Failed writes and read-only health errors expose sanitized codes, not raw requests or credentials. Error responses mark execution outcome unknown where a side effect cannot be ruled out.

The current model registry contains research-only baselines. A metadata flag cannot promote a rule into a calibrated live strategy. A future trusted signal provider needs an approved artifact digest, compatible target/horizon/product, feature contract and independent validation evidence. Legacy REAL runner/supervisor entrypoints and old MCP broker mutation registrations are quarantined so they cannot bypass this path.

## Secrets, interface and transport

Secrets use external owner-only regular files, not committed values, browser cookies or reused login sessions. Secret symlinks and unsuitable ownership/modes are rejected. Configuration and datasets are bounded, explicitly selected, non-world-writable files outside the repository. Account IDs are kept private in object representations and replaced with broker-specific fingerprints in proposal/MCP output. These are pseudonymous correlations, not anonymity guarantees.

HTTP requires a constant-time bearer comparison, loopback binding and loopback peers; duplicate authorization headers fail. Every HTTP route, including health/readiness, uses that boundary. Bodies are bounded; access logs are disabled. Stdio assumes an already trusted local process/user. No public identity provider, remote TLS gateway, token rotation service or per-user RBAC is included. Health and readiness do not create broker sessions, load pickles or run shell commands.

Crypto REST code has an injected transport and signer only. It signs exact serialized request bytes, restricts endpoints and order types, bounds pagination, rejects cross-origin links, validates account/UUIDs and avoids automatic uncertain-write retries. It does not implement credentials, HTTP sessions or production retry policy. Agentic MCP is a public contract/discovery validator only; authenticated schemas and execution mappings remain unimplemented.

## Residual requirements

- Validate Linux filesystem locking/fsync, real signal delivery, shutdown and restoration on the selected host.
- Protect operator identity, signing material and process privileges; use private authenticated client ingress and host monitoring.
- Reconcile broker history before resuming from any backup that may predate a submission; establish one account writer and consistent backup snapshots.
- Verify production rate limits, auth refresh/revocation, idempotency retention, fill/fee timestamps and all missing risk inputs using the official product.
- Independently validate market data adjustments, calibration, costs and strategy performance. Engineering safety tests do not establish that a strategy should trade money.

See [the implementation report](platform-report.md) for tested regressions and [server readiness](server-readiness.md) for remaining deployment work.
