# Alpaca equities paper boundary — offline preparation

Prepared September 11, 2026. `atlas/research/equity_paper.py` maps reviewed
long-only equity orders to Alpaca paper REST request/response contracts using
**in-memory response fixtures only**. It has no HTTP transport, credential input,
CLI, background worker or broker session. No equity order was sent. The existing
BTC/USD paper pilot remains halted after its earlier refusals.

The adapter accepts only the concrete `FixtureTransport` class. Its fixed paths
use `https://paper-api.alpaca.markets` for account, positions, open orders, market
clock, asset metadata, fake order submission and exact client-ID lookup. The
quote fixture uses `https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest?feed=iex`.
These fixture checks establish local contract behavior; they do not establish
current account entitlement, data subscription or acceptance by Alpaca.

## Caller contract

`normalize_intent(intent)` validates an already reviewed intent without choosing
its symbol, side, quantity or allocation. The object has exactly these fields:

```text
schema: 1
client_order_id: canonical UUID reserved by the caller
account_fingerprint: SHA-256 of the selected paper account UUID
symbol: uppercase equity ticker
side: buy or sell
quantity, limit_price: explicit decimal strings
time_in_force: day
extended_hours: false
reservation_usd, fee_buffer_usd: explicit decimal strings
risk_reservation_id: canonical UUID reserved by the caller
reviewed_at, expires_at: integer UTC seconds, at most 300 seconds apart
intent_sha256: intent_hash(intent), bound before execution
```

Buy notional plus the explicit fee buffer must fit the cash reservation, with a
$100 maximum. A sell requires owned long inventory and zero cash reservation.
Quantities respect nine decimal places and asset `fractionable` metadata. Limit
prices respect cents at $1 or more, and four decimal places below $1. The caller
must derive any fee buffer and deterministic risk reservation separately.

Preflight requires the exact account fingerprint, ACTIVE/unblocked status,
multiplier one, no short market value, at most $100 cash/equity, cash-only buying
power, no unmanaged holdings or open orders, a tradable U.S. equity, and a fresh
open regular-session clock and quote. Freshness is capped at ten seconds. This
deliberately narrow fixture contract may refuse an otherwise valid broker
account; no production eligibility is inferred.

For `submit`, the caller also supplies an exact matching reservation object:

```text
reservation_id, client_order_id, intent_sha256, reserved_cash_usd
state: committed
```

The caller-provided `attempts` map models a durable execution journal in tests.
The adapter records an unknown attempt before its sole fake POST. The same client
ID cannot submit twice; reuse with changed terms refuses. Timeout, malformed
acknowledgement or missing lookup remains unresolved. A 404 does not release the
reservation or authorize another submission. Exact order correlation, cumulative
fill monotonicity and terminal-state immutability are checked. A canceled order
with a partial fill retains that inventory evidence.

## Activation status

Every result reports `broker_activation_blocked: true`,
`actual_orders_submitted: 0` and `live_approved: false`. The in-memory attempt map
is **not a durable production journal**. A future paper connection needs a
separately implemented and reviewed persistent reservation/submission boundary,
an official authenticated paper transport, verification of the selected account
and data contract, and resolution of the outstanding broker-readiness issue.
The module cannot alter keys, move funds, reset the older pilot or call a live
broker endpoint.

`tests/test_multiasset_equity_paper.py` covers 50 offline cases, including account
and product restrictions, reservation/hash tampering, UTC freshness, no default
network path, ambiguous submission, missing lookup, repeat prevention, partial
fills, and immutable terminal outcomes. Passing those fixtures is engineering
evidence only.
