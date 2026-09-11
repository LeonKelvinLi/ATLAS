# Coinbase local paper simulation

This dedicated command observes Coinbase public BTC-USD books and simulates
orders in an external SQLite database. It has no API key, account, order-submit
or transfer interface. All cash, positions, orders, fills and P&L in its output
are synthetic. A successful local run does not establish broker acceptance or
strategy profitability.

## Fixed protocol

The reference `sma_reference_v1` starts with exactly $100 synthetic USD, a $10
all-in entry budget, one long BTC position at a time, and no top-ups. Parameters
are fixed before collecting a campaign and bound to the journal on restart.
Existing frozen IQ/FX research artifacts and evaluation rules are unrelated.

The example uses fast/slow averages of 5/20 observations, an entry divergence
threshold of 25 basis points, and at least 5 seconds between observations. These
are engineering reference parameters, with no claimed predictive advantage.
Signals use previous observations; execution uses a strictly later fresh book.
Large data gaps reset warmup. A five-minute run is an integration check and
cannot answer whether a strategy works over meaningful market conditions.

Only immediately executable simulated orders are modeled. Buy sizing reserves
the adverse price plus upward-rounded taker fees inside the cash/order caps.
Available displayed depth limits assumed fills, and modeled 5 bps adverse
slippage is additional to displayed-depth price impact. Actual future liquidity,
latency and fill probability remain unknown. Passive maker fills are not modeled.
Partial sales retain their proportional cost basis and unsold dust.

The example's total and UTC-day loss limits are each $5. These are triggers for
a persistent entry halt, not guarantees against a larger loss on a price gap.
The model can still reduce a position after an entry halt. A process stop stops
observation entirely and does not liquidate either simulated or real holdings.

## Fee evidence

Refresh the existing View-only Coinbase preflight before the campaign. The
runner requires its successful authenticated report, matching SPOT/CBE taker
fee, View-only permissions, and an age of at most one hour. It stops if that
age expires. The rate remains a dated scenario assumption; the tier is not
an actual order-fee quote and extra commission/tax effects remain unverified.

The repository example deliberately has a null taker fee and cannot run until
an actual report is selected and its rate supplied as basis points. The public
runner never opens the key file or refreshes private account data itself.
Run receipts include the preflight's content hash and observation time without
copying balances, portfolio identifiers or credentials.

## Commands

All configuration, database, receipts and backups must be outside the checkout.
Use owner-only storage and a new receipt filename for every invocation. For an
existing external configuration and fresh fee report:

```text
uv run --locked python scripts/coinbase_simulation.py \
  --config /absolute/private/simulation.json \
  --journal /absolute/private/paper.sqlite3 \
  run --samples 60 --fee-report /absolute/private/account-preflight.json \
  --output /absolute/private/receipts/campaign-001.json
```

Each invocation is finite, with 1–720 observations requested. It sleeps between
observations, performs only bounded public GETs, and does not retry failed
requests. Network/data failures persist an entry halt when storage is available.
A failed or unfinished receipt is not a completed campaign. The journal's
single writer lock prevents overlapping processes from spending the same
simulated cash. Status/backup commands run when the writer is idle.

```text
uv run --locked python scripts/coinbase_simulation.py \
  --config /absolute/private/simulation.json \
  --journal /absolute/private/paper.sqlite3 status

uv run --locked python scripts/coinbase_simulation.py \
  --config /absolute/private/simulation.json \
  --journal /absolute/private/paper.sqlite3 backup \
  --output /absolute/private/backups/paper-001.sqlite3

uv run --locked python scripts/coinbase_simulation.py \
  --config /absolute/private/simulation.json \
  --journal /absolute/private/paper.sqlite3 stop
```

While running, SIGINT/SIGTERM or a file named `STOP` alongside the journal
requests a stop. The runner checks again after a market read before processing
the snapshot. Removing the file does not clear the durable entry halt.
No command resets capital or clears a halt. Investigate state before choosing
a separately named simulation; that is a new experiment, not a continuation.

Use the SQLite backup command rather than copying a live database/WAL pair.
Test recovery by opening a separately named backup with the original config,
checking integrity, and comparing every balance, position and event counter.
Preserve the config, immutable run receipts and fee provenance along with the
database. Configure encrypted offsite backups and retention on the actual host.

## Evaluation and operations

`round_trip_smoke_v1` is a separate, explicitly labeled engineering scenario:
one simulated buy followed by a later simulated sale. Its purpose is to verify
cash, fee, quantity and cost-basis accounting. It must use a different journal
and must not be combined with reference-strategy performance results.

Reference reports expose observed sample count, fees, cash, holdings, realized
P&L, marked unrealized P&L, liquidation estimate, drawdown and entry-halt state.
The $100 cash baseline is explicit. An exposure-matched buy-and-hold comparison,
independent longer campaigns and statistical evidence are still required before
claiming an advantage. Do not tune against this run and call it fresh validation.

The [Linux template](../deploy/atlas-coinbase-simulation@.service.example) runs
one finite campaign, uses a unique instance name for the receipt, and has no
automatic restart or schedule. It permits outbound networking required for
public HTTPS/DNS; the Python client pins the routes and never accepts arbitrary
URLs. It does not expose a network listener or use the generic live factory.

Before installation, select the host and dedicated user, install locked
dependencies, provision trusted config/fresh fee evidence and a 0700 receipts
directory, and review host network controls, disk limits and logs. Retrying the
same instance/receipt is refused. On the selected host, verify real OS stop,
storage failure, reboot behavior, restore and monitoring. No host is currently
selected and no service or timer was installed by this change.

## September 11 engineering verification

The complete offline suite passed **1,867 tests, with two pre-existing optional
skips**, in 30.69 seconds. The new coverage comprises 58 accounting/journal
tests, 23 runner-control tests and four process/storage recovery tests. It
includes an actual child-process SIGKILL after commit, SQLite disk-full rollback,
an ambiguous commit result, missing-file races, competing writers, stale fees,
partial fills, retained dust, loss halts and exact backup restoration.

The production-public-data reference run completed **60 observations in
313.98 seconds**, with zero observation gaps, simulated orders, fills, fees or
loss halts. Its balance remained **$100**, with no BTC position. No entry met
the fixed reference rule during this window. The same-capital cash baseline
also remained $100; no strategy advantage is established. Reopening both the
original journal and its consistent backup reproduced the final state exactly.

The separate production-public-data accounting smoke completed two observations
and two simulated fills, closing one round trip. From $100 synthetic starting
cash and a $10 entry budget, ending cash was **$99.7498842694**, realized P&L was
**-$0.2501157306**, and modeled fees totaled **$0.24**. No BTC remained in that
simulation. These results exercise accounting; they do not test predictive
performance. No real purchase, sale or transfer occurred.

Its consistent backup reopened with exactly the original cash, position, fees,
event count and audit hash. A subsequent manual entry halt also survived reopening
the original journal. The authenticated fee refresh confirmed the 120bps taker
tier used in the simulation; extra commissions/tax and actual future order fees
remain unknown. All eight frozen research artifacts retained their hashes.

Private campaign inputs, SQLite journals, immutable receipts, recovery evidence
and test output are stored outside Git under
`/Users/leon/Desktop/dev/ATLAS-research-evidence/coinbase-simulation-20260911`.
The campaign plan fixes parameters, sample counts and source hashes before
collection. The public-data reference run and forced accounting smoke use
separate journals and must be evaluated separately.
