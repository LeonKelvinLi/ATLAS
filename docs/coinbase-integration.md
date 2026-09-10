# Coinbase Advanced integration

Reviewed September 10, 2026. This extension targets **Coinbase Advanced spot
BTC-USD** for public market data, explicit cost analysis and local simulation.
Public connectivity does not establish Leon's account eligibility, balance,
fees, API permissions or order acceptance. No Coinbase order or transfer has
been requested by this integration, and the proposed $100 allocation remains a
model constraint rather than a reserved or funded Coinbase account.

## Local setup and observed fee inputs

The Coinbase Advanced interface was enabled in Leon's signed-in Chrome session.
At about 22:16 UTC on September 10, the Fee tier details panel displayed **Intro
1: 0.600% maker and 1.200% taker**, with a notice that the tier updates roughly
every five minutes. Those rates are dated UI observations, not a fee quote for
any future order. The authenticated fee endpoint has not yet been read.

The external configuration at
`/Users/leon/.config/atlas/coinbase/preview-config.json` uses `60` maker basis
points and `120` taker basis points from that observation, with a $100 simulated
allocation and $100 all-in scenario budget. A maker preview and a taker preview
are alternatives drawing on the same modeled capital. They do not allocate $100
each. The repository example deliberately leaves the fee inputs null; it cannot
run until explicit fee assumptions are supplied.

Run a new public-data preview from the checkout:

```text
uv run --locked python scripts/coinbase_preview.py \
  --config /Users/leon/.config/atlas/coinbase/preview-config.json \
  --output /Users/leon/Desktop/dev/ATLAS-research-evidence/coinbase-integration-20260910/preview-new.json
```

The output must be a new external file; existing reports are never overwritten.
The command makes two public GETs and exports a cost comparison. It cannot place
orders, access a Coinbase account or transfer funds. Every future use needs
appropriate fee inputs; the saved UI rates do not update automatically.

The signed-in API key page showed no existing API keys. A form named
`ATLASViewOnly` is prepared for the Primary portfolio, with ECDSA selected,
View enabled, and Trade, Transfer and Receive off. Key creation is pending
explicit approval. No private key has been created or saved. The selected
BTC-USD panel displayed $0 USD and about 0.09 available USDC; that panel is not
a complete inventory of the user's holdings and does not establish a $100
funded allocation. No Coinbase purchase, sale or transfer was made.

### Verified public preview

The production public-data command succeeded at **22:23:56 UTC** on September
10. It observed a $77,139.28 bid and $77,139.29 ask, then computed these
alternative $100 scenarios using the dated fee inputs:

| Modeled entry | BTC quantity | Entry fee reserve | Maximum entry cost including modeled fee |
|---|---:|---:|---:|
| Resting post-only limit | 0.00128857 | $0.60 | $99.9993620296 |
| Immediately executable limit | 0.00128092 | $1.19 | $99.9992593468 |

Neither row represents an order or fill. Resting-order fill probability remains
unknown. The preview uses the worst allowed limit price plus an upward-rounded
fee reserve to enforce the budget, and reports the displayed-depth VWAP cost
separately. Estimated exit break-even rises from the current bid were about
1.836% and 2.444%, respectively, assuming a taker exit and the documented
rounding cushion. These are cost thresholds, not return predictions; exit depth
is unverified.

Receipts are stored outside Git in
`/Users/leon/Desktop/dev/ATLAS-research-evidence/coinbase-integration-20260910`:
`coinbase-ui-observation.json`, `public-preview.json`, and
`public-preview-cli.log`. They report zero orders and disabled execution.

## Public contract

Use the pinned HTTPS origin `https://api.coinbase.com` and these GET paths:

| Purpose | Path | Relevant result |
|---|---|---|
| Product rules | `/api/v3/brokerage/market/products/BTC-USD` | Identity, increments, size bounds and market modes |
| Order book | `/api/v3/brokerage/market/product_book?product_id=BTC-USD&limit=20` | `pricebook.product_id`, ordered `bids`/`asks`, each with decimal-string `price` and `size`, plus `pricebook.time` |

The official REST overview says these public endpoints require no authentication
and normally have a one-second cache. Send `Cache-Control: no-cache`; still
validate the provider timestamp and local receipt time. The generated reference
pages display an authorization header even for public endpoints. Unauthenticated
requests succeeded in this review, consistent with the overview.
[REST overview](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/rest-api),
[public book](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-book).

`base_increment` governs base quantity, `quote_increment` quote-denominated
amounts, and `price_increment` limit prices. Parse all financial values as
decimals; do not infer a price step from display formatting. Check both base and
quote minimum/maximum sizes after rounding. Product identity and `product_type`
must match BTC, USD and SPOT. Treat missing safety flags, disabled trading,
cancel-only mode, auctions and unfamiliar modes as unavailable for this bounded
preview. A product-level `post_only` mode flag is distinct from an individual
order's post-only instruction.
[Product contract](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product).

Two public GETs returned HTTP 200 on September 10 at 22:14:13 UTC. Observed
BTC-USD rules were: `status=online`, `base_increment=0.00000001`,
`price_increment=0.01`, `quote_increment=0.01`, `base_min_size=0.00000001`,
`quote_min_size=1`, `base_max_size=3400`, `quote_max_size=150000000`.
`is_disabled`, `trading_disabled`, `cancel_only`, `limit_only`, `post_only`,
`auction_mode` and `view_only` were false. These values are a dated observation,
not constants to hard-code. The one-level book had provider time
`2026-09-10T22:14:12.794990Z`, bid `77075.04` and ask `77075.07`.

Public `view_only=false` cannot prove account tradability. The authenticated
product endpoint accepts `get_tradability_status=true` to populate that field
for SPOT products.
[Authenticated product](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/products/get-product).

## Costs and the $100 model

Do not hard-code a published headline tier as Leon's fee. Maker and taker rates
depend on the current account tier and may change. An immediately executable
limit order pays taker fees on that portion; a resting portion can pay maker
fees. Read the actual tier before presenting costs as account-specific.
[Advanced fees](https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/advanced-trade-fees).

Coinbase One's zero-fee benefit excludes Advanced trading. Some membership tiers
advertise an Advanced spot-fee rebate; that is not zero upfront cost and must not
be assumed without account evidence. No subscription is needed for this public
data integration.
[Coinbase One](https://help.coinbase.com/en/coinbase/other-topics/coinbase-one/account).

For the local model, cap initial synthetic capital at $100 without automatic
top-ups and reserve modeled entry fees inside each order budget. Report spread,
round-trip fees, rounding remainder and any additional slippage assumption
separately. With entry fee `f_in` and exit fee `f_out`, the price increase needed
to offset fees alone is `(1 + f_in) / (1 - f_out) - 1`. This is an accounting
threshold, not an expected return. A marketable model should use available book
depth and explicit taker costs; a passive quote must not be labeled a fill just
because its price was touched.

Post-only limits can constrain actual executions to maker treatment, but can
reject when they would cross and can remain unfilled. The current local
`PaperBroker` models immediate marketable fills and does not model queue priority,
future passive fills or hidden liquidity. Keep passive-order economics as a
preview rather than claiming a maker-fill simulation.
[Order types](https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/order-types).

## Optional account connection: read-only first

Use a dedicated CDP key scoped to the intended portfolio with **View only**.
Leave Trade and Transfer disabled. Use ECDSA/P-256 (JWT algorithm ES256) for this
bounded implementation. Coinbase's product-specific setup directs ECDSA; its
generic overview says direct Advanced API calls also support Ed25519. The
current official Python SDK source supports both, despite documentation still
describing an ECDSA-only SDK. ES256 is our selected format, not a claim that all
Coinbase interfaces reject Ed25519. JWTs have a two-minute lifetime and bind the
method, host and request path; the SDK sends query parameters separately from
that signed path. Never print private keys or JWTs.
[Product authentication](https://docs.cdp.coinbase.com/coinbase-app/authentication-authorization/api-key-authentication),
[algorithm compatibility](https://docs.cdp.coinbase.com/get-started/authentication/overview),
[SDK JWT source](https://github.com/coinbase/coinbase-advanced-py/blob/master/coinbase/jwt_generator.py),
[SDK request signing](https://github.com/coinbase/coinbase-advanced-py/blob/master/coinbase/rest/rest_base.py).

The implemented authenticated preflight uses only GET and rejects excess key
permissions before reading the account:

1. `/api/v3/brokerage/key_permissions`: require `can_view=true`,
   `can_trade=false`, `can_transfer=false`; bind `portfolio_uuid` to the intended
   portfolio. The returned permission fields describe capabilities, not a
   readiness approval.
2. `/api/v3/brokerage/accounts?limit=250`: follow `has_next`/`cursor` completely
   within a fixed page bound, refusing repeated cursors or partial data. Check
   `active`, `ready`, currency, `available_balance`, `hold`, and
   `retail_portfolio_id`. CDP keys select the permitted portfolio; the optional
   portfolio query is deprecated for these keys. Total assets are not available
   USD cash, and existing holdings must not be counted as a new $100 allocation.
3. `/api/v3/brokerage/transaction_summary?product_type=SPOT&product_venue=CBE`:
   read decimal-fraction `fee_tier.maker_fee_rate`, `taker_fee_rate` and
   `pricing_tier`; preserve any extra commission/tax uncertainty. This summary
   does not establish today's realized trading P&L.
4. `/api/v3/brokerage/products/BTC-USD?get_tradability_status=true`: verify
   account-visible product status in addition to the public market rules.

[Key permissions](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/data-api/get-api-key-permissions),
[accounts and pagination](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/accounts/list-accounts),
[fee summary](https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/fees/get-transaction-summary).

Keep endpoint and method allowlists independent of broker permissions. Store
secrets outside the repository in owner-only files, reject redirects, bound
response size/time/pages, sanitize errors, and export only the needed account
summary. A read-only connection should have no order, preview-POST, transfer or
portfolio-mutation method. This preflight must not supply invented zero fees or
daily P&L to the general ATLAS execution service.

After approved key creation, store the exported JSON outside Git with mode
`0600`. The accepted format contains exactly `name` and `privateKey`, using a
P-256 private key. The optional discovery command reads key scope only:

```text
uv run --locked python scripts/coinbase_preflight.py \
  --key-file /Users/leon/.config/atlas/coinbase/read-only-key.json \
  --permissions-only \
  --output /Users/leon/.config/atlas/coinbase/permissions.json
```

Its private output includes the portfolio UUID and fingerprint. Discovery does
not prove that it is the intended portfolio and does not read balances or fees.
Verify it against the selected portfolio before the full preflight. The full
command requires `--expected-portfolio` with that verified UUID instead of
`--permissions-only`, and a different new output file. Every returned account
must match the selected portfolio. Numeric HTTP failure codes are retained;
raw provider messages, keys and JWTs are never printed.

No authenticated command above has been run against Leon's account. Production
credentials are still absent while key-creation approval is pending. Offline
tests use temporary generated test keys and injected responses only.

## Sandbox and MCP choice

Coinbase Advanced's sandbox returns predefined mocked responses without
authentication. It is useful for schema examples but cannot validate strategy
performance, live balances, liquidity, fill probability or live order acceptance.
Use the existing local simulator for accounting tests and public data for market
observations; identify both evidence types explicitly.
[Sandbox](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/sandbox).

The official **Coinbase MCP** now connects to Advanced Trade, whereas **CDP
CLI/MCP** primarily serves developer wallets and onchain infrastructure. A CDP
wallet is not Leon's retail exchange account.
[Tool comparison](https://docs.cdp.coinbase.com/get-started/build-with-ai/comparing-agentic-tools).

The remote Coinbase MCP endpoint is `https://agents.coinbase.com/mcp` with OAuth.
The current official allowlist names ChatGPT, Claude and other clients, while
Codex is listed as coming soon. The local `@coinbase/coinbase-cli` is an official
alternative requiring Node 22 or later, but its documented setup enables Trade
and Transfer and exposes mutation commands. Neither route is required for this
public-data and read-only phase. Do not add broad permissions merely to obtain
quotes or fee data. Account isolation is supported through portfolio-scoped
access, but configuring it does not itself move or reserve the proposed funds.
[Coinbase MCP](https://docs.cdp.coinbase.com/ai-agents/coinbase-for-agents/coinbase-mcp),
[portfolio scope](https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/guides/portfolios).

## Engineering boundary

Keep the Coinbase public adapter separate from `BrokerAdapter` execution methods
and from the Alpaca pilot journal. Public connection success cannot clear the
Alpaca refusals or restore its consumed attempts. No change to the frozen IQ/FX
research is justified by adding a BTC data source. Coinbase support is an
engineering capability; no profitable strategy or unattended trading readiness
has been established.

## Verification receipt

`uv sync --locked --group dev` passed without package upgrades. The complete
offline suite passed **1,782 tests with two existing optional skips** in 35.14
seconds. The new Coinbase public/preview tests contributed 150 passing cases;
the view-only preflight contributed 123. Coverage includes fresh snapshots,
precision and minimums, limits under changing displayed depth, fee rounding,
restricted products, JWT binding, least-privilege checks, complete account
pagination, HTTP failures and secret-safe reports. All eight frozen research
artifacts retained their prior hashes.

The live public GET preview is separate evidence from those offline tests.
Authenticated Coinbase account connectivity remains untested pending approval
and creation of the view-only key. No workers, scheduled jobs, live orders or
transfers were started. The Alpaca pilot remains halted; its support request
was sent separately and Coinbase setup does not change that result.
