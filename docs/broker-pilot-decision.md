# Broker decision and $100 test preparation

**Connection update:** the subsequent Alpaca installation exposed 24 market-data
tools, with successful BTC/USD asset and quote reads. This installed plugin has
no account or order tools. It is distinct from the broader provider-hosted/local
Trading MCP described below. See [paper connection status](alpaca-paper-connection.md).

September 9, 2026 (America/Los_Angeles). Leon confirmed existing Robinhood and
IQ Option accounts, with Robinhood registered in the United States. The proposed
funded budget is **$100 of starting capital, with no automatic top-ups**. No real
funds have been allocated or reserved by ATLAS.

## Decision

**Prefer the official Robinhood route over the existing IQ integration for a
possible future funded pilot. Use the local $100 simulator now; Alpaca is the
clearest documented option for the subsequent broker paper test.** Webull is a
credible alternative worth evaluating if opening another broker account: its
advertised native restrictions are useful, but their behavior and the connection
between its MCP and sandbox have not been authenticated or tested here.

This is a platform/engineering recommendation, not a recommendation to buy a
particular asset. The latest ATLAS historical diagnostic was rejected: the primary
returned approximately -0.244% under modeled costs, with unverified historical
availability and no alpha-validation eligibility. Passing software tests cannot
promote that strategy. See [the retained diagnostic](data-intake-report.md).

The older IQ models predict short-horizon FX binary outcomes. They cannot be
transferred to Robinhood equity or spot-crypto orders by changing the broker name.
The current modern factory connects only to the local paper simulator; both
Robinhood adapters remain unavailable for live deployment.

## Broker comparison

| Provider | Relevant verified public capability | Fit and remaining uncertainty |
|---|---|---|
| Robinhood | Official Agentic trading MCP and separate Crypto REST API. Agentic uses a dedicated trading account; broader account information can still be readable. | Best fit among Leon's existing accounts for further investigation. Actual Agentic eligibility, account isolation settings, exact costs/minimums and authenticated schemas need verification. No sandbox was established in this review. |
| Alpaca | Official MCP, separate paper credentials and endpoint, free paper environment with documented simulation assumptions. | Preferred broker paper route. Requires owner account setup; ATLAS does not yet have an Alpaca adapter or keys. Default paper balance is much larger than $100 and must be deliberately constrained. |
| Webull | Official MCP advertises optional read-only access, order previews, value/size/symbol restrictions. OpenAPI separately advertises sandbox execution. | Strong additional candidate. No authenticated evidence that MCP uses the sandbox or that per-order limits cap aggregate account exposure. |
| IBKR | Official MCP reads data/accounts and drafts instructions. Orders require the user to review and submit them in IBKR. | Useful if the desired final workflow is explicitly user-submitted trades; another account is unnecessary for the current local test. |
| Longbridge | Official hosted MCP, market data, portfolios/orders, and paper APIs; global endpoint documents U.S. account support. | Credible alternative, with more market/account configuration than this first experiment needs. Specific account entitlements remain unknown. |
| IQ Option | Public site advertises a demo account; ATLAS's legacy adapter uses the unofficial `iqoptionapi` library. | Not selected. Its public page in this session displayed a country-unavailable message, and the footer excludes U.S. residents from its intended audience. An existing login does not resolve eligibility. No official IQ MCP was verified. |

Sources: [Robinhood overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/),
[Robinhood tools](https://robinhood.com/us/en/support/articles/trading-with-your-agent/),
[Robinhood Crypto API](https://robinhood.com/us/en/support/articles/crypto-api/),
[Alpaca MCP](https://docs.alpaca.markets/docs/alpaca-mcp-server),
[Alpaca paper assumptions](https://docs.alpaca.markets/docs/paper-trading),
[Webull MCP](https://www.webull.com/agentic),
[Webull OpenAPI](https://www.webull.com/open-api),
[IBKR official integration](https://www.interactivebrokers.com/en/trading/ai-integrations.php),
[IBKR user submission guide](https://www.ibkrguides.com/clientportal/gen-ai-instructions.htm),
[Longbridge MCP](https://open.longbridge.com/docs/mcp),
[Longbridge pricing](https://open.longbridge.com/pricing),
[IQ Option public page](https://iqoption.com/en).

## MCP inventory and selection

At the start of this review, the callable-tool inventory contained **no broker account MCP**. Alpaca,
Webull, IBKR, Longbridge and the data/research plugins below appeared in the
user-supplied list of available but uninstalled plugins. That listing does not
prove usable permissions or account connectivity. Robinhood's official remote
MCP was verified independently of that list.

This is a bounded review of relevant candidates, not an exhaustive directory of
every community server. No integration was installed during the comparison itself;
the later Alpaca data-plugin installation is recorded in the update above.
Prefer provider-owned services to password/cookie-based community wrappers.

| MCP / plugin | Role | Selection for this experiment |
|---|---|---|
| Robinhood Agentic | Existing-account inspection and official trading interface | Future owner connection candidate. Public scope metadata only advertises `internal`; a narrow read-only OAuth grant is not established. |
| Alpaca Trading | Broker paper/live account, market data and orders | First choice for a broker paper integration. Explicitly choose paper keys, paper endpoint and `ALPACA_PAPER_TRADE=true`. |
| Webull Agentic | Broker data/orders with advertised restrictions | Evaluate native controls and sandbox compatibility before choosing it over Alpaca. |
| IBKR | Broker research and user-reviewable order instructions | Best verified structural separation between agent analysis and user submission. |
| Longbridge | Multi-market brokerage, data and paper testing | Alternative; no immediate need to add it. |
| CoinGecko | Read-only crypto data and research | Optional free addition. Official keyless MCP has shared limits; it does not execute trades or prove execution quality. |
| CoinMarketCap | Read-only crypto quotes, metrics and news | Overlaps CoinGecko; free-key option available. No need to connect both initially. |
| Financial Datasets | Fundamentals, filings, news, prices and screening | Useful for later equity research; current paid entry pricing is poor value for this $100 BTC test. |
| LONA Trading Assistant | Strategy authoring and historical backtests | Duplicates current research work. Provider says it does not offer brokerage/execution. |
| TradingCursor | Technical analysis and suggested levels | Optional analysis, not verified broker execution. Marketing performance claims are not strategy evidence. |
| Stocktwits | Sentiment and social research | New agent/plugin contract, access and pricing remain unverified. Legacy API-registration restrictions cannot establish the newer plugin's status. |
| Binance | Exchange/developer integration candidate | No current provider-owned MCP contract and U.S.-compatible account route verified here. The plugin name does not establish Binance.US support. |

Additional portfolio/news products in the supplied list, such as Parqet and
Bigdata.com, were not pursued: they do not resolve the current paper execution,
account isolation or strategy-evidence gaps. Their absence from the shortlist
is not a finding that they lack useful capabilities.

Data/research sources: [CoinGecko MCP](https://docs.coingecko.com/docs/mcp-server),
[CoinMarketCap MCP](https://coinmarketcap.com/api/mcp/),
[Financial Datasets MCP](https://docs.financialdatasets.ai/mcp-server),
[Financial Datasets pricing](https://www.financialdatasets.ai/pricing),
[LONA tools](https://docs.lona.agency/agents/mcp-tools-reference),
[LONA product](https://www.lona.agency/en),
[TradingCursor plugin](https://www.tradingcursor.com/chatgpt-plugin/),
[Stocktwits](https://stocktwits.com/),
[Binance developer portal](https://developers.binance.com/en).

The smallest useful eventual setup is **one official broker paper connection**,
with CoinGecko optional for research. In Alpaca's MCP, an initially restricted
data toolset can use `stock-data,crypto-data,assets,news`. Its `account` toolset
includes configuration mutation, so it must not be treated as universally
read-only. Paper submission tools need a separately verified paper configuration.

Connecting a broad upstream trading MCP directly can bypass ATLAS's $100 policy.
An ATLAS configuration limit cannot constrain independent tools, other broker
accounts, manually submitted orders, deposits or withdrawals. Account-level
funding isolation and a narrow integration path remain necessary for a funded
design; an order-value limit alone is insufficient.

## Costs and simulation assumptions

Keep subscriptions out of this first test. Alpaca's basic equity feed is IEX-only;
its $99/month enhanced feed is disproportionate to the proposed $100 capital.
Its lowest-volume crypto tier lists 0.15% maker and 0.25% taker fees per side,
approximately 0.50% for a taker buy/sell cycle before spread and slippage. Alpaca
documents fractional stock orders starting at $1 in paper and live environments.
[Data plans](https://docs.alpaca.markets/docs/about-market-data-api),
[crypto fees](https://docs.alpaca.markets/docs/crypto-fees),
[fractional trading](https://docs.alpaca.markets/docs/fractional-trading).

These are Alpaca facts, not Robinhood cost estimates. Exact Robinhood instrument
minimums, price increments, eligible account fee tier and executable spreads
remain unverified. The local pilot uses **60 bps per side as an illustrative
stress assumption**, not a quoted brokerage fee. Broker paper results also omit
some real effects: Alpaca documents exclusions including market impact, latency
slippage and order-queue position. No simulator fill is a real fill.

## Implemented local pilot

[Pilot configuration](../deploy/paper-100-config.example.json) and
[operating guide](../deploy/paper-100-pilot.md) define:

- $100 initial synthetic cash with an immutable initial-funding commitment.
- $10 maximum order value including estimated fees; $25 maximum position and
  25% concentration; at most four durable order attempts per UTC day.
- A $5 realized-loss gate, 30-second input freshness, and an operator kill switch.
- Dedicated account/state; MCP execution tools disabled in the template.

Restarting does not refill losses. The simulator refuses changed or removed
capital commitments and refuses to adopt an older uncapped account, even if its
current cash happens to equal $100. Bound accounts use state schema 3 so older
code cannot silently ignore the commitment. Existing uncapped accounts retain
their behavior. Profit can increase equity above the original funding limit.

The $5 gate is not a guaranteed stop-loss. It currently also blocks sell orders
once realized loss breaches the threshold. The kill switch blocks submissions;
neither mechanism automatically exits a position. These limitations preclude
describing this configuration as an unattended loss-limited live system.

### Observed smoke result

The bounded smoke ran once through `load_config`, `build_service` and the actual
`AtlasService` proposal/risk/execution path, with network access denied. All
**10 engineering checks passed**. One generated BTC-USD order had $9.90 notional
plus $0.0594 modeled fees. Remaining synthetic cash was **$90.0406**, with
**0.000099 simulated BTC**. This is an accounting fixture, not a market forecast.

Repeated submissions before and after restart added no orders. Changing the
funding commitment to $1,000 failed without changing the account. Oversized,
kill-switch and stale-input proposals each failed for their expected risk cause.
There was one durable submission and four proposals total. Final marked equity
was $99.940501 under the fixed synthetic bid/ask and fee assumptions; this is not
a strategy-performance observation.

Receipts and both databases are retained outside Git in
`/Users/leon/Desktop/dev/ATLAS-research-evidence/paper-100-20260909`.
`evidence.json` records artifact and script hashes. The kill switch is active,
and all transient service helper threads finished. The actual run began at
2026-09-10 05:22:10 UTC; fixture clocks are separately identified as synthetic.
The [runnable script](../scripts/paper_100_smoke.py) refuses an existing output
directory, so reproductions cannot overwrite the original account or receipts.

### Regression verification

`uv sync --locked --group dev` passed. The required full suite,
`uv run --locked python -m pytest -q -ra`, completed with **1,272 passed and two
skipped in 120.31 seconds**. The skips are the absent optional `iqoptionapi`
library and the explicitly opted-in private DuckDB dataset. The 20 new capital
cases are included in this count. Independent review found no actionable
accounting regression in the funding changes.

All eight frozen research/model/journal hashes remain equal to the baseline.
No one-time forward evaluator or retraining ran. The prior historical
experiment's source identity remains tied to its earlier pinned implementation;
these new source edits do not retroactively change that experiment's evidence.
Full test output is retained as `pytest.log` beside the smoke receipt.

## Next testing stage

The local smoke test establishes accounting and control behavior only. Genuine
prospective paper evidence requires a live observation feed with preserved
provider timestamps and acquisition receipts, broker-specific costs, a fixed
strategy identity and a declared evaluation window. The current factory reads
synthetic quote files; changing their timestamps or importing recent prices does
not turn this into a forward test.

For the next external stage, the owner must choose and establish an official
paper account. If inspecting Robinhood first, use its official desktop Agentic
onboarding and check whether a dedicated account is offered. Keep it unfunded
while actual permissions, account identity, schemas, fees and isolation are
reviewed. No authenticated `tools/list`, account read or order preview occurred
here. Do not provide passwords, OTPs or private keys in the conversation.

Funding and real-order submission remain owner actions. ATLAS currently cannot
reserve $100 at Robinhood, and this assistant has not transferred or traded any
funds. No long-running process, listener, scheduled task or live connector has
been started. The missing prospective strategy evidence remains an independent
blocker even after a broker connection is available.

## Verification provenance

Provider documentation was reviewed from official sources. Research agents used
read-only public HTTP document/API retrieval; Chrome was the only browser used.
Some initial Chrome content reads timed out; root later read the settled IQ page
successfully. No alternative browser, private session extraction, account
authentication or credential reuse was used.

Robinhood product/account-page details above come from the earlier September 9
official-source review retained in `robinhood-research.md`. This pass could open
those pages but could not re-read their contents because Chrome requests timed
out. Account availability and detailed permissions must therefore still be
checked during owner onboarding; the fresh public metadata below is narrower
evidence than an authenticated product test.

Robinhood's public OAuth discovery and resource metadata were rechecked and
returned HTTP 200, with the official MCP resource, authorization-code flow and
PKCE S256. This establishes discovery only. See the detailed distinction between
public contracts and unverified authenticated operations in
[Robinhood research](robinhood-research.md).
