# Alpaca connection and paper-account preflight

Market-data connection observed September 9, 2026 local time / September 10 UTC.
Dedicated paper-account setup verified in Chrome on September 10, 2026 local time;
authenticated paper REST preflight completed at 08:13:48 PDT / 15:13:48 UTC.

## Current paper account

The owner completed signup and verification. A new dedicated simulated account
named **ATLAS $100 Paper** was created and selected in the official Alpaca
dashboard. Chrome inspection verified the following displayed state:

| Field | Verified state |
|---|---|
| Account | ATLAS $100 Paper, simulated paper account |
| Account / crypto status | ACTIVE / ACTIVE in the paper API |
| Cash / equity | $100 / $100 in Chrome and the paper API |
| Non-marginable buying power | $100 in Chrome and the paper API |
| Open positions / market value | None / $0 in Chrome; empty positions API result |
| Orders | None displayed in Chrome; complete empty open-orders API result |
| Trading/account blocks and user suspension | All false in the paper API |
| Execution enabled | False |

The existing preflight CLI ran once with the selected paper account's credentials
and verified the pinned `https://paper-api.alpaca.markets` endpoint over TLS.
Its three read-only requests completed within approximately 0.812 seconds.
Account, positions and orders were separate observations, not an atomic snapshot.
`paper_account_read_verified` and `current_state_matches_100` are true;
`immutable_starting_capital_verified` remains false.

The preflight reported zero orders submitted and zero account mutations. Creating
the paper account and generating its keys were earlier, separate dashboard
mutations. At this connection checkpoint, no real funds were deposited or
allocated and no broker paper or real trade had been placed. Later bounded paper
execution is tracked in [the pilot report](alpaca-paper-pilot.md). The full account
number and secret values are excluded here.

The private directory `/Users/leon/.config/atlas/alpaca-paper` has mode `0700`.
The selected paper credentials are saved in `paper-keys.json`; the redacted
read-only result is `preflight-account.json` in the same directory. Both files
were verified as regular files, mode `0600`, owned by the current user.
The report, rather than the credentials, was read for this documentation update.

`preflight-account.json` SHA256:
`fd7031dddedf4b111970c15217db5fd18793908328939c12eeb605cfc1b65d02`.

## What is connected

The owner confirmed installation of `alpaca@openai-curated-remote`. The installed
plugin exposes **24 market-data tools** covering assets, calendars, stock/crypto
and option data. It exposes **no account, order-submission, cancellation, funding
or paper-balance tools** in this task. The larger provider Trading MCP described
in Alpaca's public documentation is a distinct capability; installing this data
plugin does not make that trading interface available.

Two connector tool calls succeeded: `get_asset` for `BTC/USD`
and `get_crypto_latest_quote` for `BTC/USD`, feed `us`. These market-data calls
did not read a private account. Whether the connector uses provider-managed data
credentials or a particular user paper account was not established and is not
inferred from the data response. The later authenticated paper-account read
described above used the standalone REST preflight, not an account MCP tool.

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

## Completed account connection

The dedicated account has been created successfully with the requested $100
simulated starting amount. No real-money brokerage account or deposit is needed
for this paper test.
[Official paper-trading guide](https://docs.alpaca.markets/docs/paper-trading).

The connection workflow selected **ATLAS $100 Paper**, generated its paper API
credentials, saved them privately outside the repository, and completed the
read-only preflight. Its account identity is retained as a fingerprint in the
private report. The current UI and API state agree on the $100 flat account.

A durable funding baseline and complete account-activity reconciliation remain
prerequisites for broker paper execution. The successful current-state check
does not enforce future funding or an immutable $100 allocation.

There is no verified Trading API operation for changing the paper account's
starting cash. Do not delete/reset an existing account to bypass failed limits
or clear losses. Returning `min(broker_cash, 100)` would repeatedly replenish an
apparent budget after losses; the local simulator's capital commitment cannot
constrain the broker account.

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

### Repeat the read-only preflight when needed

The prepared external JSON file contains exactly `api_key` and `api_secret`
for the selected paper account. It remains readable/writable only by its owner
(`0600`). Neither value belongs in a command argument, source file, log or chat
message. Any replacement file must retain this format and private permissions.

Template to save privately outside the checkout, replacing the placeholders
locally:

```json
{
  "api_key": "YOUR_PAPER_API_KEY",
  "api_secret": "YOUR_PAPER_API_SECRET"
}
```

For a later repeat, select a new report filename. The original
`preflight-account.json` is preserved and cannot be overwritten by the CLI:

```text
/Users/leon/Desktop/dev/ATLAS/.venv/bin/python -B \
  /Users/leon/Desktop/dev/ATLAS/scripts/alpaca_paper_preflight.py \
  --key-file /Users/leon/.config/atlas/alpaca-paper/paper-keys.json \
  --output /absolute/private/new-paper-preflight.json
```

The key file must be outside the repository and cannot be a symlink. The report
path must be a new external file. Account identity is represented by a fingerprint;
credentials and the account number are excluded. The command never changes the
local $100 smoke account or the remote paper account.

The first authenticated run completed successfully as recorded above. Credential
and response handling were also verified using fake transports and generated
fixtures. All 55 targeted tests passed, covering wrong origins, redirects, oversized and
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

Ongoing strategy execution still needs complete durable order and activity
reconciliation, an established $100 funding baseline or allocation ledger,
fees/realized-P&L accounting and a genuine prospective data/strategy protocol.
The separate [bounded paper pilot](alpaca-paper-pilot.md) implements a connectivity
order and cancellation with an irreversible attempt cap and one recorded
correction after proven rejection. Both broker attempts were refused, so its
lifecycle test remains incomplete. It does not
assemble the ongoing strategy service or invent missing P&L and fee measures.
The ATLAS deployment factory remains unavailable for real broker operations;
the read-only preflight is not an execution adapter and does not install a worker.

The authenticated report's four remaining blockers are
`execution_not_implemented`, `immutable_starting_capital_unverified`,
`daily_realized_pnl_unavailable`, and `order_fee_estimate_unavailable`.
Reported account accrued fees were $0; estimated order fees and daily realized
P&L remain null. An accrued-fee balance cannot supply those missing measures.

Crypto paper fee deductions were not established from the public documentation.
Keep observed broker fees and modeled costs separate; later fee activities can
post after the fills. Minute bars can include quote midpoints and later revisions,
so do not infer that all bars are immutable trade-only history. Preserve the
chosen feed, provider timestamps and receipt times.
[Fees](https://docs.alpaca.markets/docs/crypto-fees),
[activities](https://docs.alpaca.markets/docs/account-activities),
[crypto streaming data](https://docs.alpaca.markets/docs/real-time-crypto-pricing-data).

The dedicated $100 broker paper account is verified through both the Chrome
dashboard and authenticated paper API reads. Automatic strategy execution remains
disabled. The later one-time paper lifecycle test and its actual broker results
belong in [the pilot report](alpaca-paper-pilot.md). The prior local $100 synthetic
smoke remains a separate, completed engineering test.
