# Data Contract V1 and prospective intake

`scripts/atlas_data.py` registers a fixed market-data generation, appends bounded
observations and reports coverage. It has no strategy evaluation, optimization,
order, transfer, allocation, worker or scheduler command. Existing strategy
thresholds, execution models, historical archives and frozen model artifacts
remain separate and unchanged.

## Frozen contract

`atlas/research/data_contract_v1.py` defines `ATLAS_DATA_CONTRACT_V1`:

- SPY, QQQ, GLD and USO on Alpaca IEX; BTC-USD on Coinbase Exchange.
- Five-minute unadjusted OHLCV; USD prices; declared volume and quote-size units.
- Bar close/event time, optional provider publication time, local availability,
  actual archive ingestion, source, raw-capture hash and linked local revisions.
- Independent bid/ask observations with provider timestamp text, sizes and units.
  Unknown publication, units, corporate actions, calendars and costs stay explicit.
- New York regular equity sessions, including DST and declared early closes;
  UTC 24/7 crypto. A missing bucket is not automatically a no-trade interval.
- Three immutable 90-calendar-day windows: research, validation and prospective
  holdout, separated by 300-second embargoes. Full bars must open within their
  window and close before its exclusive end. Bars straddling an embargo are excluded.

Event ≤ publication (when known) ≤ availability ≤ ingestion ≤ verification time.
Receipt clocks round **up** to UTC seconds; original provider timestamp text is
retained. This is not a tick-precision clock or a claim of original publication.
Backfills retain the actual historical capture receipt, with **new** local archive
ingestion. Imported calendar metadata likewise becomes visible only at ingestion.
As-of readers require both visibility clocks and select the latest visible revision.

Stale observations are retained and flagged: over 900 seconds for bars or 60
seconds for quotes at ingestion. Present-day freshness is reported separately;
reading an old archive does not retroactively change its original staleness.

## Append and holdout behavior

Create an explicit private directory outside the repository. SQLite transactions
append complete raw captures, observation versions and hash-linked batch receipts.
Every table rejects updates/deletes; verification checks schema, canonical bytes,
hashes, revision predecessors, receipt bindings and calendar contradictions.
An unchanged snapshot adds a capture receipt without inventing another version.
Source changes for an existing observation require a new generation. Historical
and test lineage cannot become prospective through a later correction.

Imports always remain historical. Injected test clocks or transports cannot
produce prospective evidence. Built-in GET intake labels only events occurring
after registration as prospectively observed; this label establishes neither
provider authentication nor strategy qualification. The same event's earlier
historical/test lineage remains sticky.

The ordinary bucket reader refuses holdout until the window has elapsed and the
caller presents the matching specification digest frozen **before** holdout began.
The specification may be frozen once; rule changes require a new generation.
Coverage can examine timestamps, hashes and data quality while the holdout is
sealed, but emits no prices, signals, returns or strategy scores.

Local hashes and SQL triggers detect internal inconsistencies, not a privileged
owner rewriting or replacing the whole archive. External timestamping, independent
provider attestation, clock synchronization, backups, Linux power-loss recovery
and filesystem enforcement remain host work. Missing, malformed or inaccessible
state refuses automatic recreation. A crash leaving a hot SQLite journal can
make read-only verification refuse until controlled writable recovery; that host
recovery procedure has not been exercised here.

## Commands

Run from the checkout using `uv run --locked python scripts/atlas_data.py`.
`--help` lists the full arguments. No command discovers credentials automatically.

```text
init --archive /absolute/private/new-archive --generation study-v1 --research-start YYYY-MM-DDTHH:MM:SSZ
contract --archive /absolute/private/new-archive
verify --archive /absolute/private/new-archive
coverage --archive /absolute/private/new-archive
import-backfill --archive /absolute/private/new-archive --input /absolute/private/bundle.json
freeze-strategy --archive /absolute/private/new-archive --specification /absolute/private/strategy-v1.md
collect-once --archive /absolute/private/new-archive --provider coinbase --start YYYY-MM-DDTHH:MM:SSZ --end YYYY-MM-DDTHH:MM:SSZ
collect-once --archive /absolute/private/new-archive --provider alpaca --key-file /absolute/private/alpaca-data-key.json --start YYYY-MM-DDTHH:MM:SSZ --end YYYY-MM-DDTHH:MM:SSZ
```

Choose research start so that research + 90 days + embargo + validation + 90 days
+ embargo places holdout in the future, leaving time to review and freeze the
strategy. Registration uses the current clock and cannot be backdated. The
freeze command hashes an already prepared specification; it neither generates
nor evaluates a strategy. The CLI does not export holdout prices.

An import bundle contains exactly `records`, `captures`, `calendar_coverage`.
Records use the complete V1 schema. Each capture has `role`, `request_url`,
`received_at`, and `body_base64` containing exact original response bytes.
Calendar descriptors have `start_date`, `end_date`, `sessions`, `source_id`,
`raw_capture_sha256` and `received_at`. A session has `session_date`, `open_time`
and `close_time`. An empty session list asserts closure only over that explicitly
queried date range. Imports cannot choose prospective status or archive ingestion.

Each collection is bounded to a completed, aligned period of at most one day.
Repeated invocations can append further days and observe revisions. A one-day
bound is a request limit, not a limit on total history. Limits are 20,000 records
and 32 MiB per batch; 1,000,000 observations and 10,000 batches per archive.
Verification scans the archive and fails explicitly if limits are exceeded.
Capacity/latency under sustained collection has not been benchmarked.

The built-in clients are pinned HTTPS GET paths, with bounded response sizes,
timeouts and pagination, and no redirects or retries. Current quotes are kept
separate from requested historical candles. Polling captures locally observed
versions; it cannot recover every intermediate exchange correction.
Alpaca collection includes current quotes separately from bars and calendar data;
Coinbase collection includes the current level-one book. If collection fails,
safely timestamped completed responses are retained in a batch with no admitted
observations. The receipt reports `collection_complete: false`, and the CLI exits
with status 1. A missing or untrustworthy receipt clock cannot be invented to
archive an unreceived response.
The Alpaca query pins its documented IEX feed and USD price currency.
[Latest quotes endpoint](https://docs.alpaca.markets/us/reference/stocklatestquotes-1).

Alpaca documents timestamped quotes and late bar updates; its IEX feed is not
consolidated SIP coverage. Quote-size conversion remains an explicit concern.
[Alpaca stock market-data streams](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data)
and [historical bars](https://docs.alpaca.markets/us/reference/stockbars).

Coinbase Exchange candles may omit intervals without ticks. V1 retains unknown
candle volume and book-size units when the verified endpoint contract does not
establish them. It refuses to infer historical quotes from a current book.
[Coinbase Exchange candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)
and [product book](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-book).

## Interpreting coverage

The JSON report separates raw event coverage, information available by window
end, prospective-only holdout coverage and present-day freshness. It reports
expected/observed buckets, whole missing sessions, unknown calendar dates,
revisions, quote visibility at bar close, unknown units, stale receipts and exact
blocker codes by window and instrument. Unknown calendar coverage produces null
totals, not zero expected sessions. It does not fill gaps or shift window dates.

`numerical_coverage_complete` is separate from `can_support_90_90_90`.
The latter requires timing and provenance gates too. The current V1 intake
preserves unverified publication, corporate-action, provider and cost evidence,
so those blockers cannot be removed merely by collecting more rows. Independent
evidence contracts require separate reviewed work; changing a JSON label cannot
certify them. `qualification_established`, `alpha_validation_eligible` and
`live_approved` remain false. A complete dataset still needs a frozen strategy
and the existing qualification process.

## September 13 offline checkpoint

The selected prior IEX and BTC archives were replay-checked and copied into a
new historical-only evidence archive: 6,203 stock bars plus 8,640 BTC bars.
All original source files remain unchanged. Research and holdout have no bars;
validation contains only 21 equity sessions and 30 crypto days. Known-session
gaps remain 5 for GLD and 344 for USO. There are no historical quotes, original
publication times remain unknown, and the 90/90/90 coverage report fails.
Locked dependency synchronization and the full repository suite passed: 2,861
tests, including 364 new data tests, with two expected skips. The skips cover
the unavailable legacy IQ Option vendor package and the opt-in private dataset.
All eight frozen artifacts and earlier research receipts were hash-checked.

The evidence generation is an engineering fixture over selected historical data.
No strategy specification was frozen, no qualification/optimization ran, and no
prospective collection job, broker session or funded activity was started.
Actual long history across market regimes and a future untouched holdout remain
data-acquisition work; this implementation does not manufacture them.
