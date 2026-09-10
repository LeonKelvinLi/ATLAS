# Bounded Alpaca paper connectivity pilot

This is a one-time test of submission, broker acknowledgement, cancellation and
recovery against Alpaca's paper endpoint. It is not an automatic strategy runner
and does not weaken the existing strategy risk checks. Real-money endpoints,
market orders, sales, transfers, account resets and account-wide cancellation
are unavailable in this client.

The selected account starts with $100 simulated cash. The controller allows
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

Implementation verification and the first provider paper smoke are pending.
The earlier $100 account/API check is recorded in
[the connection report](alpaca-paper-connection.md).
