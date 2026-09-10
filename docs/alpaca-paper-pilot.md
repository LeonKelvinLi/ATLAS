# Bounded Alpaca paper connectivity pilot

This is a one-time test of submission, broker acknowledgement, cancellation and
recovery against Alpaca's paper endpoint. It is not an automatic strategy runner
and does not weaken the existing strategy risk checks. Real-money endpoints,
market orders, sales, transfers, account resets and account-wide cancellation
are unavailable in this client.

The selected account starts with $100 simulated cash. The standard controller allows
**one BTC/USD limit-buy attempt with a $2 ceiling including a modeled 60 bps fee
reserve**. Its initial limit is 2% below the observed bid, rounded down using
current broker price and quantity increments. A moving market can still fill
it. Any partial or full fill remains as a reported paper position; the test
does not automatically sell to restore the starting balance.

## Prepared commands

The external selection file is
`/Users/leon/.config/atlas/alpaca-paper/pilot-config.json`.
It contains a key-file path and the verified account fingerprint, never inline
credentials. Both selection and key files must be owned by the current user,
regular files outside the repository, with mode `0600`.

Run from the checkout:

```text
uv run --locked python scripts/alpaca_paper_pilot.py setup \
  --config /Users/leon/.config/atlas/alpaca-paper/pilot-config.json

uv run --locked python scripts/alpaca_paper_pilot.py smoke \
  --config /Users/leon/.config/atlas/alpaca-paper/pilot-config.json

uv run --locked python scripts/alpaca_paper_pilot.py status \
  --config /Users/leon/.config/atlas/alpaca-paper/pilot-config.json
```

`setup` checks the exact account, $100 cash/equity/non-marginable buying power,
active crypto permissions, no positions and no order history. It captures
unfiltered account activities as the baseline. Existing state is validated and
preserved. `smoke` records one immutable order intent, refreshes account/history
checks, submits once, then requests cancellation of that exact order and polls
briefly for a terminal state. No continuous process or listener is started.

State is fixed to
`~/.config/atlas/alpaca-paper/pilot/pilot.sqlite3`; selecting another configuration
path cannot create another production pilot journal. The database binds the
paper origin, account fingerprint, limits and deterministic client UUID. A file
lock serializes processes; full SQLite commits preserve the intent before POST.
Audit records must agree with current state. Empty, corrupt or foreign state
refuses operation. Never delete the journal or restore an old copy to obtain
another attempt.

## Recovery and halt behavior

A timeout or crash after recording the submission consumes the single attempt.
Another `smoke` invocation only looks up that same client ID and attempts its
recorded cancellation; it cannot place a replacement order. A missing lookup
is unknown, never permission to submit again. A cancellation acknowledgement
is not a final canceled state. Accepted, partially filled, pending-cancel,
replaced and other nonterminal orders remain unresolved.

The test leaves `HALTED` present. It blocks new exposure while allowing reads
and cancellation of the exact owned order. For an incomplete operation:

```text
uv run --locked python scripts/alpaca_paper_pilot.py cancel \
  --config /Users/leon/.config/atlas/alpaca-paper/pilot-config.json
```

Cancellation is recorded before DELETE and is not blindly retried after an
ambiguous response. An unresolved cancellation requires inspection using the
same order ID; the controller cannot claim that the account is flat.

## Explicit correction after a confirmed rejection

The first test was refused by the broker. A separate correction command is
permitted only when the complete original audit proves a known-rejection handler
was reached, with no submission uncertainty, acknowledgement, broker ID or fill.
Missing order lookups alone never qualify. The original intent, binding,
reservation, audit and `HALTED` flag are preserved.

An immutable correction grant in the same journal permits one additional $2
attempt with a distinct fixed client UUID and at most $4 combined modeled
reservations. Both old and new IDs must be absent, the account must still be flat
at $100, and complete activities/history must still match the baseline. The
correction intent is committed before POST. A crash or uncertain correction
result allows lookup/cancellation only; there is no third grant or attempt.

The revised price formatter uses a cent-compatible grid for prices above $1,
while respecting the broker asset increment. This is conservative compatibility
with the general order guide, not proof of the original rejection cause.
Future rejections retain the observed HTTP status, bounded numeric provider code
and a fixed attributed reason category; raw provider messages are not retained.
[General limit-price rules](https://docs.alpaca.markets/us/docs/orders-at-alpaca).

```text
uv run --locked python scripts/alpaca_paper_pilot.py corrected-smoke \
  --config /Users/leon/.config/atlas/alpaca-paper/pilot-config.json

uv run --locked python scripts/alpaca_paper_pilot.py corrected-status \
  --config /Users/leon/.config/atlas/alpaca-paper/pilot-config.json
```

`corrected-cancel` reconciles cancellation for that exact correction ID. These
commands do not reset the initial test or enable ongoing strategy trading.
The explicit `halt` command permanently revokes an unused correction grant.
After an intent exists, halt still permits its exact lookup/cancellation.

## Evidence and limits

Submission and cancellation results must match the immutable symbol, asset ID,
side, quantity, price and client ID. A broker ID cannot change, cumulative fills
cannot regress, and canceled/expired orders retain any fills. Status also reads
cash, equity, positions, complete bounded order history and activities.

The $2 reservation is never replenished, even for a canceled or rejected order.
The $100 snapshot and this one-time cap do not constrain manual deposits or
other clients. Before submission, changed activity or any existing order blocks
the clean pilot. Account reads are separate requests, not an atomic snapshot.
Orders at the 500-row limit and incomplete activity pagination are rejected.

Actual order fees and daily realized P&L remain unknown. The 60 bps allowance is
an explicit model; absence of immediate fee activities does not establish zero
fees. Later fee or correction records need reconciliation. The generic ATLAS
service remains unavailable for this broker because its full strategy/account
provider and ongoing accounting have not been assembled.

This paper lifecycle test cannot establish strategy profitability, live fill
quality or live readiness. No frozen model or one-time evaluator is used.

Provider contracts:
[crypto order rules](https://docs.alpaca.markets/us/docs/crypto-orders),
[create order](https://docs.alpaca.markets/us/reference/postorder),
[client-ID lookup](https://docs.alpaca.markets/us/reference/getorderbyclientorderid),
[cancel order](https://docs.alpaca.markets/us/reference/deleteorderbyorderid-1),
[activities](https://docs.alpaca.markets/us/reference/getaccountactivities-2),
[paper assumptions](https://docs.alpaca.markets/us/docs/paper-trading).

## Current execution receipt

**Setup is complete for the bounded connectivity test, but the broker lifecycle
test has not passed. Both attempts were rejected, neither produced an accepted
order, and the pilot is halted with no submission attempts remaining.** The
account still has $100 simulated cash and no positions. Automatic strategy
trading and real-money trading remain disabled.

The initial implementation passed 1,454 tests, with two existing optional skips,
in 167.97 seconds. All eight frozen research artifacts were unchanged.

The first broker test on September 10 at 16:15:51 UTC recorded a limit buy for
0.000026365 BTC at $75,403.2874, with a $1.9999357183348060 modeled reservation.
The broker rejected it. The old client discarded the error body and exact HTTP
status, so the specific cause is unavailable. A price-precision mismatch is only
a hypothesis. Subsequent paper reads returned $100 cash/equity, no positions,
no orders and no activities. No cancellation was sent because no broker order
was found. The attempt remains recorded and halted.

Receipts and test output are outside Git in
`/Users/leon/Desktop/dev/ATLAS-research-evidence/alpaca-paper-pilot-20260910`.

The revised implementation at `7ffad70` passed **1,509 tests with two existing
optional skips** in 25.91 seconds. On September 10 at 16:33:31 UTC, its one
corrective request attempted 0.000026393 BTC at $75,324.76, reserving
$1.99997466902408 including modeled fees. Alpaca returned **HTTP 403, provider
code 40310000**. The retained diagnostic category is `other`; the raw provider
message was not retained, so the specific refusal reason is still unknown.
Price precision was not established as the cause of either rejection.

Reads at 16:33:48 UTC showed account and crypto status ACTIVE, account/trading
blocks false, cash/equity/non-marginable buying power $100, and empty positions,
order history and activities. No broker order ID, accepted order, fill or
cancellation was observed. Both request IDs, their rejection audit records and
the combined $3.999910387358886 modeled reservation remain preserved. Subsequent
missing lookups leave the active intent's recovery state `unknown`; they do not
erase the recorded HTTP refusal or authorize another submission.

A read-only Chrome check of the same paper account's Configure page at about
16:37 UTC showed Trades Suspended off and Fractional Trading on. No account
settings were changed. The public [status page](https://status.alpaca.markets/)
reported the paper Order API operational at 16:39 UTC. These observations do not
explain the refusal. The [account configuration contract](https://docs.alpaca.markets/us/reference/getaccountconfig-1)
documents suspension and fractional-trading settings; the dashboard agrees with
the earlier active account snapshot.

The next step is provider diagnosis. The
[support request](alpaca-paper-support-draft.md) was sent to Alpaca on September
10 after Leon authorized continuation; Gmail confirmed `SENT`. Support
acknowledgement and resolution remain unverified. Do not reset the journal,
replenish the attempt budget, raise the paper
balance or submit another order to recover an error message. The earlier $100
account/API check is recorded in [the connection report](alpaca-paper-connection.md).
