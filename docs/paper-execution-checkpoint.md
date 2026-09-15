# ATLAS paper execution checkpoint

Overall readiness: **PARTIAL**. The isolated engineering implementation and full
regression suite pass. No authenticated Alpaca order lifecycle or strategy
performance result was established by this work.

Worktree: `/Users/leon/Desktop/dev/ATLAS-paper-execution`.
Branch: `codex/atlas-paper-execution`. Base: `e36ca70`.

## Independent tracks

The collector VM, deployed release, cloud configuration, credentials, validation
evidence and hourly automation were not accessed or changed during this
paper-engine development. The original checkout, main branch, frozen models,
`FORWARD_TEST.md`, `FINDINGS.md` and sealed holdout were not modified. This branch
has not been pushed, merged or deployed.

The supplied collector window starts September 14 at 9:19:36 PM Pacific and
reaches 86,400 seconds September 15 at 9:19:36 PM Pacific. This is the supplied
checkpoint, not a new verification of cloud health. Infrastructure readiness
remains pending the separate clean-run evidence. The VM collects independently
of the Mac; local Codex follow-ups can inspect evidence when the app is available.

## Before and after

| Component | Before | Implemented here |
| --- | --- | --- |
| Equity execution | Fixture-only equity contract; separate exhausted BTC pilot | Fixed-origin Alpaca PAPER adapter and bounded execution engine |
| Intent/recovery | Reusable narrow gateway patterns | SQLite FULL commits before POST, deterministic signal identity, reservations, reconciliation |
| Ownership | Per-journal exclusion | Per-account local lock; permanent PAPER account-to-journal registration prevents a fresh-folder reset |
| Lifecycle | No ongoing equity account ledger | Partial fills, known/uncertain failures, exact cancellation, terminal-only replacement of remaining quantity |
| Risk | Separate research/simulation checks | Cash, inventory, exposure, losses, drawdown, frequency, fresh quotes/session checks and persistent halts |
| Operations | No dedicated equity command | Private config, initialize, offline status/halt, reconcile/submit/cancel/replace, synthetic demo |
| Strategy | Existing causal equity/commodity algorithms | Source/spec binding and offline bridge; no tuning or new strategy selection |
| Qualification | Existing metrics and frozen rules | Explicit evidence assessor reusing those metrics and thresholds |

Architecture rationale: [paper-execution-architecture.md](paper-execution-architecture.md).

## Defects caught

- Corrected cash reconciliation and cumulative/incremental fill accounting;
  unexplained cash changes cannot become strategy P&L or new risk anchors.
- Fixed double-subtraction of broker open-order holds from buying power.
  Unsent local reservations still consume capital.
- Bound starting principal, allowing gains above $100 without silently increasing
  exposure limits. The whole dedicated paper account must match its allocation.
- Preserved complete provider cooldowns, including delays over five minutes.
  Invalid restrictions fail closed; throttling does not consume the failure budget.
- Prevented successful reads or acknowledgements from clearing cumulative order
  failures. HTTP refusals and synchronous/asynchronous rejected orders count;
  repeated reconciliation does not count a rejection twice.
- Closed concurrent and sequential same-account fresh-directory bypasses.
- Kept quote, signal, account and session deadlines through final authorization,
  including the durable-write interval.
- Separated local request authorization from observed broker orders and actual
  delivery. Synthetic actual-order counts are zero; uncertain delivery is unknown.

## Verification

`uv sync --locked --group dev` completed. The required complete run,
`uv run --locked python -m pytest -q -ra`, finished with **3,590 passed,
3 skipped, 0 failures** in 184.56 seconds. The three intentional skips require
the optional IQ Option package, a disposable root Linux container, and explicit
private DuckDB dataset opt-in. None is a new paper-execution test failure.
Focused test group totals overlap and are not added to this result.

Coverage includes rejected orders, partial fills, cancel/fill races, duplicate
signal terms, stale quotes, closed sessions, insufficient funds, exposure limits,
timeout after acceptance, absent client-ID lookup, rate limits, disconnects,
unmanaged state, corrupt journals, kill-switch persistence, mode/account confusion,
adapter bypass attempts and competing account writers. Separate subprocesses die
after reservation, after arming and after broker acceptance before acknowledgement;
recovery does not resubmit or invent fills.

New tests use synthetic data and isolated state. No new broker authentication,
paper order, live order, funding or account change was performed.

## Readiness and limits

| Gate | Result | Outstanding evidence |
| --- | --- | --- |
| Local engineering | PASS | Full suite and independent review completed |
| Collector infrastructure | PENDING, unchanged | Independent uninterrupted 24-hour validation |
| Alpaca PAPER acceptance | PARTIAL | Dedicated-account preflight and observed accepted/fill/cancel lifecycle |
| Existing-strategy paper pilot | NOT RUN | Authenticated prospective input and reliable account execution |
| Strategy readiness | PARTIAL; qualification not established | Predeclared unseen observations, costs, benchmarks and adequate sample |
| Live-money execution | DISABLED | No LIVE route exists in this package |

The previous BTC pilot recorded two rejected attempts and remains halted. These
are prior observations, not proof that a fresh equity request would fail. Its
journal, credentials and exhausted attempt budget were not reset or reused. New
PAPER writes default off. An account with excess initial capital or multiplier
above one fails initialization; this code does not modify accounts to pass.

This is a bounded local engine, not an unattended trading deployment. The account
lock covers one OS user/host; another host, user or dashboard can still interfere.
A dedicated account and one authorized execution host remain necessary. Python
private attributes are an application convention, not a malicious-code sandbox.
Missing/corrupt PAPER account registration fails closed on reopen.

Cancel acknowledgement can still race a fill. Only terminal broker state plus
reconciled fills releases the reservation. Ambiguous submissions never retry.
Cancellation is attempted once per owned order; unresolved cancellation requires
reconciliation and operator review. There is no automatic unhalt, journal reset
or account rebind command.

History is bounded: an order response reaching 500 rows or activity history above
2,000 records stops execution. A long study needs a separately tested complete-
history extension before those limits. Fees remain unknown unless supplied.
IEX quotes do not establish consolidated liquidity or queue priority. Alpaca's
[paper-trading documentation](https://docs.alpaca.markets/docs/paper-trading)
describes simulator limits; its
[fractional-order contract](https://docs.alpaca.markets/docs/fractional-trading)
supports the DAY limit shape implemented here. Account acceptance still needs
observation.

## Next step

Keep cloud validation frozen. Prepare a separate dedicated Alpaca PAPER account
and private config, verify its permissions/eligibility, then obtain an accepted
paper lifecycle. Do not change or reset the collector account or exhausted pilot
to obtain that evidence. Bind a prospective input adapter to the unchanged chosen
strategy and run the predeclared study. Combine execution/strategy evidence with
the independent infrastructure result only when both exist.

See [paper-execution-operations.md](paper-execution-operations.md) for commands and
recovery, and [paper-qualification.md](paper-qualification.md) for measurement and
evidence requirements.
