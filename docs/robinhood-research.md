# Robinhood integration research

Verified September 9, 2026, from public material. This review used documentation
and unauthenticated GET requests for public OAuth metadata. No brokerage login,
OAuth registration, token request, MCP session, account read, or order occurred.
Retrieval date is recorded because these product pages do not expose a reliable
publication date.

## Decision

Robinhood now offers an official trading MCP service covering equities, options,
and crypto. The separate official Crypto Trading REST API is also documented.
Therefore, a claim that Robinhood only offers unofficial integrations, or only
offers crypto integration, would be incorrect as of this review.
[Official Agentic product](https://robinhood.com/us/en/agentic-trading/),
[Crypto API help](https://robinhood.com/us/en/support/articles/crypto-api/).

ATLAS should keep these as two distinct adapters: `RobinhoodAgentic` for the
official remote MCP contract, and `RobinhoodCrypto` for the signed REST contract.
This is an engineering recommendation, not a statement that either adapter has
been authenticated, authorized for this user, or validated with a live account.

## Official Agentic MCP

Endpoint: `https://agent.robinhood.com/mcp/trading`, using Streamable HTTP.
Connection and account onboarding require the account owner on a desktop.
A primary individual account in good standing is a prerequisite. Trading is
restricted to a dedicated Agentic account, although read access includes other
accounts, balances, positions, transactions, watchlists, and scans. Crypto needs
a corresponding account and updated agreement. Agentic crypto has geographic
restrictions, including New York, and cannot transfer, stake, or lend assets.
[Agentic overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/).

Publicly documented tool names include:

| Capability | Tools |
|---|---|
| Account and cash | `get_accounts`, `get_portfolio` |
| Holdings | `get_equity_positions`, `get_option_positions`, `get_crypto_positions` |
| Quotes | `get_equity_quotes`, `get_option_quotes`, `get_crypto_quotes` |
| Order history/status | `get_equity_orders`, `get_option_orders`, `get_crypto_orders` |
| Preview | `review_equity_order`, `review_option_order`, `preview_crypto_order` |
| Submit | `place_equity_order`, `place_option_order`, `place_crypto_order` |
| Cancel | `cancel_equity_order`, `cancel_option_order`, `cancel_crypto_order` |
| Realized results | `get_realized_pnl`, `get_pnl_trade_history` |

The service supports long equities, options, and crypto. Limited margin supports
unsettled proceeds, but margin borrowing is not enabled. Exact input schemas,
per-tool order types, idempotency semantics, rate limits, and sandbox availability
were not verified from authenticated discovery. Names are insufficient to build
correct order payloads.
[Trading tool catalog](https://robinhood.com/us/en/support/articles/trading-with-your-agent/).

### OAuth metadata verified without authentication

Public discovery returned HTTP 200 with these values:

| Field | Value |
|---|---|
| Issuer | `https://agent.robinhood.com/mcp/trading` |
| Authorization endpoint | `https://robinhood.com/oauth` |
| Registration endpoint | `https://agent.robinhood.com/oauth/trading/register` |
| Token endpoint | `https://api.robinhood.com/oauth2/token/` |
| Grants | `authorization_code`, `refresh_token` |
| Response type | `code` |
| PKCE | `S256` |
| Token endpoint authentication | `none` |
| Advertised scope | `internal` |

[Authorization-server metadata](https://agent.robinhood.com/.well-known/oauth-authorization-server).

The protected-resource document identifies the same MCP URL as its resource,
the same issuer as authorization server, `header` bearer transport, and the
`internal` scope.
[Protected-resource metadata](https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading).

This establishes an official OAuth path for this MCP product. It does not
establish a general OAuth permission to trade every retail account through
undocumented REST routes. Do not infer narrower read/write scopes from the
single advertised scope. Client registration, consent, refresh behavior, token
lifetime, and the user's entitlement remain untested.

## Official Crypto REST contract

The API serves US Robinhood Crypto customers. Credentials select permitted API
actions. V2 supports eligible fee-tier trading; V1 remains supported without a
published retirement date. Both expose account, holding, product, quote, and
order reads. This is a live-account API; no documented sandbox was located.
[Crypto API help](https://robinhood.com/us/en/support/articles/crypto-api/).

Base: `https://trading.robinhood.com`. V2 contract:

| Method | Path | Required query |
|---|---|---|
| GET | `/api/v2/crypto/trading/accounts/` | — |
| GET | `/api/v2/crypto/trading/holdings/` | `account_number` |
| GET | `/api/v2/crypto/trading/trading_pairs/` | — |
| GET | `/api/v2/crypto/marketdata/best_bid_ask/` | `symbol` |
| GET | `/api/v2/crypto/trading/estimated_price/` | `symbol,side,quantity` |
| GET/POST | `/api/v2/crypto/trading/orders/` | `account_number` |
| POST | `/api/v2/crypto/trading/orders/{id}/cancel/` | — |

Sign UTF-8 `api_key + unix_seconds + path_with_query + uppercase_method + body`
with Ed25519; Base64 signature. Headers: `x-api-key`, `x-timestamp`, `x-signature`.
Timestamp validity: 30 seconds. Rates: 100/minute/user, burst 300; endpoint
configuration can differ. Collections paginate through `next`/`previous`.

Order fields: `symbol,client_order_id(UUID),side,type`; matching
`market_order_config`, `limit_order_config`, `stop_loss_order_config`, or
`stop_limit_order_config`. Market: `asset_quantity`. Limit: `asset_quantity` XOR
`quote_amount`, `limit_price`, `time_in_force`. Stop variants add `stop_price`.
Time-in-force: `gtc|gfd|gfw|gfm`. Symbols must be API-tradable USD pairs.

V2 best-price fields: `symbol,bid,ask`; no timestamp. Estimated prices include
timestamp and fees. Account fields include `is_api_tradable`; order responses
include `account_number,client_order_id,state,executions,fee_charged`.
[Official reference](https://docs.robinhood.com/crypto/trading/).

### Implementation implications and unresolved contract points

The public schema is sufficient for a restricted offline REST adapter with
injected HTTP transport and signer. It is not evidence of successful execution.
ATLAS should implement the following constraints:

- Sign exactly the path/query and body bytes actually transmitted. Serialize
  once. Never print private keys, headers, tokens, or account numbers.
- Match the configured account and verify tradability before enabling a write.
- Treat every partial fill independently; cancellation acknowledgement is not
  a fill reversal. Unknown states must block dependent execution.
- Preserve the durable client UUID before submission. The reference calls it
  an idempotency identifier but does not establish its retention window or
  guarantee that blind timeout retries are harmless.
- Reconcile through paginated order history by broker ID/client UUID. A public
  Python example mentions a V2 single-order GET, but that operation is absent
  from the current OpenAPI path table; do not silently depend on it.
- Do not label local response receipt as exchange quote time. Use timestamped
  estimated-price data where a freshness check requires a source timestamp.
- Reject cross-origin pagination URLs; never send signed headers to a returned
  arbitrary host. Bound pagination and retry reads with backoff. Do not
  automatically retry uncertain writes.
- Test numeric precision, wrong accounts, HTTP 401/403/429/5xx, unexpected
  response bodies, partial fills, and absent fields with synthetic fixtures.

The reference was inspected from its public documentation HTML and embedded
OpenAPI JSON, parsed as data without running downloaded JavaScript. The schema
contains minor inconsistencies: V2 order-filter states include `pending`, while
the inherited response enum includes `partially_filled`; the adapter must not
equate an unrecognized state with completion. This observation supports strict
response handling, not an inferred undocumented contract.

## Retail REST, community projects, and terms

No separate public, general retail equities/options REST specification was
verified. That is an evidence limit, not proof that none exists. The official
Agentic MCP is the verified route for those asset classes.

Robinhood's general third-party policy requires written authorization for
linked trading APIs and prohibits third-party control of its app. Its official
Agentic and Crypto products describe their supported access paths. This review
does not resolve legal eligibility for a commercial multi-user ATLAS service;
use the applicable official onboarding and agreements rather than treating an
undocumented endpoint as authorization.
[Third-party policy](https://robinhood.com/us/en/support/articles/third-party-connections/).

Community repositories with Robinhood in their names are not the official
hosted service. For example, the
[verygoodplugins project](https://github.com/verygoodplugins/robinhood-mcp)
describes itself as a read-only wrapper around an unofficial API. It is not a
source for Robinhood's official contract and is not a dependency recommendation.
No community login, cookie reuse, MFA-seed collection, or reverse-engineered
retail endpoint is needed for the proposed ATLAS integration.

## Development boundary

Implement typed, allowlisted adapter operations behind ATLAS policy. The
Agentic adapter should remain unavailable for execution until official
authenticated schemas have been reviewed and mapped explicitly. Never expose
a generic upstream tool proxy to the language model. The official MCP can
otherwise bypass ATLAS controls if independently connected with write access;
the deployment design must account for that separate authority.

Keep public metadata discovery separate from owner authentication and live
broker use. All implementation work in this checkout uses fake transports and
generated fixtures. No broker integration in this document is a claim of
current account connectivity or live readiness.

## Implemented offline adapter boundary

`atlas/brokers/robinhood_crypto.py` now implements the restricted V2 contract
through mandatory injected transport, signer, and clock functions. It includes
no HTTP client, broker session, credential loading, or retry loop. It sends
limit orders only and validates account identity and tradability at mutation
time. The caller must supply an Ed25519 signer; adapter tests verify the exact
signed payload using synthetic signatures, not a real key.

Available portfolio fields are reported without manufacturing missing values:
cash, total equity, and daily realized P&L remain unavailable. Individual
positions can be marked from timestamped bid estimates; failed valuations stay
unavailable and the account timestamp reflects the oldest successful valuation.
Total held quantity can include inventory unavailable for sale. Quote market
open status and available liquidity remain unavailable. ATLAS's deterministic
live risk checks must reject execution while required inputs are missing.

`atlas/brokers/robinhood_agentic.py` records verified endpoint/OAuth metadata and
checks a supplied discovery response against reviewed schema fingerprints.
It does not authenticate, fetch schemas, proxy tools, or implement order
submission. Schema validation alone never enables execution.
