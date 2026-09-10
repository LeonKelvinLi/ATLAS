# Alpaca connection and paper-account preflight

Connection observed September 9, 2026 local time / September 10 UTC.

## What is connected

The owner confirmed installation of `alpaca@openai-curated-remote`. The installed
plugin exposes **24 market-data tools** covering assets, calendars, stock/crypto
and option data. It exposes **no account, order-submission, cancellation, funding
or paper-balance tools** in this task. The larger provider Trading MCP described
in Alpaca's public documentation is a distinct capability; installing this data
plugin does not make that trading interface available.

Two connector tool calls succeeded: `get_asset` for `BTC/USD`
and `get_crypto_latest_quote` for `BTC/USD`, feed `us`. No private account was
read. Whether the connector uses provider-managed data credentials or a particular
user paper account was not established and is not inferred from the data response.

| Observed field | Returned value |
|---|---|
| Provider quote timestamp | 2026-09-10 05:40:17.126244 UTC |
| Local request/receipt clock interval | 05:40:17–05:40:19 UTC, clocks resolved to seconds |
| Bid / ask | $78,385.08 / $78,410.259 |
| Ask quantity | 0.001006 BTC |
| Returned minimum order size | 0.000012813 BTC |
| Quantity / price increment | 0.000000001 / $0.000000001 |

These are preserved observations, not current executable prices or user account
permissions. Asset minimums can change. No trade recommendation or strategy
result was produced from this snapshot.

The original MCP responses and connection summary are outside Git:
`/Users/leon/Desktop/dev/ATLAS-research-evidence/alpaca-readiness-20260909`.
`provider-capture.json` SHA256:
`7f43c02fa75460d6b2044e2a10076bf27d34bc4ed940ae775d15b63a0b2ed896`.
This retains the connector's text and structured responses, not the underlying
broker's raw HTTP response bytes.

## Establish the dedicated paper account

The next owner action is signing in to Alpaca's paper dashboard. Creating an
account, managing authentication and generating private keys remain owner steps.
No real-money brokerage account or deposit is needed for this paper test.
[Official paper-trading guide](https://docs.alpaca.markets/docs/paper-trading).

1. Preserve existing paper accounts and their history. The newer dashboard guide
   documents the account-number menu, then **Open New Paper Account**.
2. If the current creation form permits a starting amount, choose **$100** for a
   new dedicated ATLAS paper account. The exact form and $100 acceptance have
   not been observed here. Public docs still describe a $100,000 default and
   contain both older reset instructions and newer create/delete instructions.
3. If the form forces a larger balance, stop before calling it the $100 pilot.
   A separately durable allocation ledger would be needed; returning
   `min(broker_cash, 100)` would repeatedly replenish the apparent budget after
   losses. The local simulator's capital commitment cannot constrain Alpaca.
4. Generate API credentials for that selected paper account only. Keep them in
   an owner-selected private file outside the repository. Do not paste them in
   the conversation or substitute keys for a live account.

There is no verified Trading API operation for changing the paper account's
starting cash. Do not delete/reset an existing account to bypass failed limits
or clear losses. No account creation, reset or deletion occurred in this work.

## Read-only preflight contract

The preflight verifies a connection to the fixed HTTPS paper API and inspects
the account's current state. It does not enable trading, create an account,
allocate money, alter account settings or submit/cancel orders.

Only the official `https://paper-api.alpaca.markets` origin is allowed. Requests
must use ordinary certificate verification, bounded timeouts/response sizes and
no redirects. Credentials must never follow a redirect or an untrusted URL.
The response has no explicit paper/live mode field; key prefixes, a $100 balance
or an account-number example cannot substitute for endpoint verification.
[Account reference](https://docs.alpaca.markets/us/reference/getaccount-1.md).

The useful initial state is active USD paper access, cash/equity/non-marginable
buying power consistent with the $100 pilot, and no positions or open orders.
Missing status/permission fields remain unknown. Buying power that includes
margin must not be treated as crypto cash. A clean current snapshot is not an
immutable funding history or proof that future deposits are prevented.

Daily realized P&L and final crypto fees remain unavailable from these initial
reads. Equity minus prior-day equity is not realized P&L. The result always keeps
execution disabled, even when its current-state checks pass.
[Crypto rules](https://docs.alpaca.markets/docs/crypto-trading),
[positions](https://docs.alpaca.markets/us/reference/getallopenpositions.md),
[orders](https://docs.alpaca.markets/us/reference/getallorders-1.md).

### Run after the owner has prepared the paper credentials

Use an external JSON file with exactly `api_key` and `api_secret`, containing
the selected paper account's values. Create it privately in an editor and make
it readable/writable only by its owner (`0600`). Neither value belongs in a
command argument, source file, log or chat message.

Template to save privately outside the checkout, replacing the placeholders
locally:

```json
{
  "api_key": "YOUR_PAPER_API_KEY",
  "api_secret": "YOUR_PAPER_API_SECRET"
}
```

From the ATLAS checkout, the operator can run:

```text
uv run --locked python scripts/alpaca_paper_preflight.py \
  --key-file /absolute/private/alpaca-paper-keys.json \
  --output /absolute/private/new-paper-preflight.json
```

The key file must be outside the repository and cannot be a symlink. The report
path must be a new external file. Account identity is represented by a fingerprint;
credentials and the account number are excluded. The command never changes the
local $100 smoke account or the remote paper account.

This command has not been run with real paper API credentials. Its credential
and response handling is verified using fake transports and generated fixtures.
All 55 targeted tests passed, covering wrong origins, redirects, oversized and
malformed responses, invalid financial fields, unknown permissions, oversized
paper balances, truncated order listings, private-file permissions, key-file
symlinks, error redaction and report overwrite refusal. Independent review found
no actionable endpoint, credential-handling or accounting-claim issue.

The required locked dependency check and full regression suite passed:
**1,327 passed, two skipped in 69.85 seconds**. The skips remain the optional
IQ library and explicitly selected private DuckDB data. All eight frozen
research/model/journal hashes are unchanged. Full output is retained in
`alpaca-readiness-20260909/pytest.log`; no one-time evaluator or retraining ran.

## What remains after connection

Broker paper execution still needs a reviewed adapter, complete durable order
reconciliation, an established $100 funding baseline or allocation ledger,
fees/realized-P&L accounting and a genuine prospective data/strategy protocol.
The ATLAS deployment factory remains unavailable for real broker operations.

Crypto paper fee deductions were not established from the public documentation.
Keep observed broker fees and modeled costs separate; later fee activities can
post after the fills. Minute bars can include quote midpoints and later revisions,
so do not infer that all bars are immutable trade-only history. Preserve the
chosen feed, provider timestamps and receipt times.
[Fees](https://docs.alpaca.markets/docs/crypto-fees),
[activities](https://docs.alpaca.markets/docs/account-activities),
[crypto streaming data](https://docs.alpaca.markets/docs/real-time-crypto-pricing-data).

No paper API credentials have been supplied to ATLAS, no broker paper account
has been verified, and no broker paper or real orders have been placed. The prior
local $100 synthetic smoke remains a separate, completed engineering test.
