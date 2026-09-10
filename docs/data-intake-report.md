# Real-data intake continuation

September 9, 2026. Continues the completed phase 2 at `f029630` in the same
development checkout. No application worker or broker session is needed.

## Intake audit before implementation

- **P1: No provider-to-canonical import.** A hand-authored source URL/hash could
  be supplied to the canonical loader without preserving the actual export or
  explaining field conversions. Add an offline raw-byte archive and mapping receipt.
- **P1: Candle close is not publication evidence.** A current historical response
  does not establish original publication or revision history. Default to unknown
  historical availability. An explicit close-plus-delay assumption may support
  retrospective diagnostics, but must retain unverified evidence status.
- **P1: Volume units and executability are unverified.** The candle endpoint
  describes activity volume and last-trade prices, not contemporaneous bid/ask or
  executable liquidity. Keep units unknown unless explicitly declared; preserve
  missing quote/cost evidence in the result limitations.
- **P2: Provider ordering and window semantics differ.** Coinbase's response
  buckets use start time, whereas ATLAS uses close time. Responses can include
  earlier buckets and omit periods without ticks. Record deliberate reversal and
  requested-window exclusions; reject duplicates, mixed ordering and unfinished
  retained bars. Do not fill gaps.
- **P2: Short-sample feature coverage.** Existing features reset each UTC day,
  including for crypto. Hourly observations permit warmups; daily observations
  cannot support the present ten-bar within-session features. Twelve days remains
  an intake diagnostic, not a credible alpha study.

The next implementation is a finite offline importer and verification command,
followed by one small public response. The study selection and comparison plan
are recorded outside the repository before requesting any candle prices.

Provider contract: [Coinbase Exchange candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles),
read in Chrome on September 9, 2026. It documents six response values per bucket,
the bucket-start timestamp, supported intervals, a 300-candle request limit and
possible missing or earlier-than-requested observations. No authenticated
historical publication/revision or redistribution entitlement was established.

## Implementation

`atlas/research/imports.py` converts one bounded six-column Coinbase response
without a network client. The CLI adds `research import-coinbase` and
`research verify-import`; these commands do not import the experiment runner.
The archive stores `raw.json`, `dataset.json`, `quality.json` and `receipt.json`
in a new external directory. Verification repeats the transformation from the
raw bytes and compares the exact dataset, quality report and receipt.

The importer validates USD product syntax, request URL identity, interval and
window, candle order, numeric bounds, complete bars and exact decimals. It records
conversion of bucket-open timestamps to closes, explicit reversal of descending
input, excluded rows and requested-window completeness. It does not interpolate
missing observations. Reads are bounded and refuse special files and symlinks;
archives cannot overwrite an existing directory.

Default availability uses the acquisition clock with an `unknown` classification,
and volume units remain unknown. A distinct optional archive can declare
close-plus-lag availability and base-asset volume; these are always assumptions.
Both remain unqualified for alpha validation, with latest-only revision history.
No market contract, strategy, model, execution rule or promotion threshold changed.

## Retained public sample and fixed study

External evidence root:
`/Users/leon/Desktop/dev/ATLAS-research-evidence/data-intake-20260909`.

The study-selection record was saved before requesting prices. It fixes BTC-USD,
hourly bars, the twelve completed UTC days from **2026-08-29 00:00 through
2026-09-10 00:00**, and one public GET with a one-MiB response cap. The actual
response was HTTP 200, **17,394 bytes / 289 rows**. The extra candle opening at
the end boundary is outside the study. No brokerage credentials were supplied.

The exact request and HTTP capture record are retained in `acquisition.json`;
the response is in `public-response.json`. Its SHA256 is
`e78784e055c845d35bf0e2f23e67ffc8fa4f81e4cb800736b32c83a0edeba270`.
The pre-data study record SHA256 is
`93f7bc79c9403569843150cb6faf54ad0a27cc4f7824498eda4f6f16c88de58c`.
These local receipts are not an independently timestamped registration service.
No redistribution license was established; the data remains outside Git.

The optional diagnostic fixes momentum at lookback ten as the primary hypothesis,
four existing families, position weight 0.2, threshold 0.002, five-bar rebalance,
seed 42, block size five and 200 bootstrap draws. Annualization is disabled.
It does not choose the best result and run it again. All observations are inspected
development history; no untouched holdout is designated or consumed here.

## How to read the diagnostic

- The prices are from a public historical snapshot. Original publication times,
  earlier revisions, historical venue/pair selection and data rights were not
  authenticated. An explicit assumed publication delay does not recover them.
- Existing features reset each UTC day even for crypto, leaving a short set of
  usable decisions after within-day warmup. The final evaluation uses only the
  last 40 percent of this already short sample.
- Fills use a subsequent hourly bar publication and modeled costs. The close is
  a last-trade price, not an observed midpoint or executable bid/ask. Hourly volume
  is a capacity proxy, not measured liquidity at the hypothetical order time.
- Standard subhour latency settings generally reach the same next hourly event;
  their comparison cannot validate real subhour execution timing.
- Normal costs use generic one-bp fees, five-bp spread and two-bp slippage; the
  fee sweep adds 0/1/2/5/10/20 bps above that model. None is calibrated to this
  account, venue tier or actual fill history.
- Any positive result can be at most `EXPERIMENTAL`; nonpositive results may be
  `REJECTED`. Neither outcome establishes future profitability or licenses an
  order. Bootstrap fractions describe this sample under model assumptions.

## Remaining work

The auditable import step is implemented. The credible alpha-validation dataset
milestone remains open: establish suitable licensed history, historical
availability/revisions, longer regime coverage, authentic costs/quotes and a
separately declared untouched period. Then perform the predefined comparisons and
collect prospective paper/shadow evidence under frozen identities. Further
broker integrations, model families or dashboards do not resolve these gaps.

Architecture, risk, execution and production readiness scores in the phase 2
report are not raised by this import. Strategy evidence and funded readiness
remain **1/10**; the public snapshot is not sufficient independent strategy
evidence. Server selection, actual recovery rehearsal, authentication and owner
activation remain separate unfinished stages.

Final verification and observed diagnostic results are appended below.
