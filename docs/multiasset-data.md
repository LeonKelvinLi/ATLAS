# Historical stock data intake

`atlas/research/data_adapters.py` supplies a provider-neutral `HistoricalDataAdapter`
protocol and canonical historical bundles. The built-in Alpaca adapter uses only
bounded GET requests for stock bars, the US trading calendar, and current asset
metadata. It has no order, cancellation, transfer, account-reset, strategy, or
worker methods. It does not change the halted Alpaca paper pilot.

The CLI requires an explicitly selected external paper-key file containing exactly
`api_key` and `api_secret`, owned by the current user with mode `0600`. Key contents,
request headers, and provider error bodies are never printed. Credential and output
paths must be outside the repository and cannot use symlink paths or parents.

## Request and result contract

```python
from atlas.research.data_adapters import (
    AlpacaHistoricalAdapter, HistoricalBarsRequest,
    archive_historical_bundle, parse_utc, verify_historical_archive,
)

request = HistoricalBarsRequest(
    provider="alpaca",
    asset_class="equity",
    symbols=("SPY", "QQQ", "GLD", "USO"),
    start=parse_utc("2026-09-08T13:30:00Z"),
    end=parse_utc("2026-09-10T20:00:00Z"),
    interval_seconds=300,
    feed="iex",
    adjustment="raw",
    max_pages=10,
    page_limit=1000,
)
# Each of the following calls is explicit; importing the module does no I/O.
bundle = AlpacaHistoricalAdapter("/absolute/private/paper-key.json").fetch_bars(request)
report = archive_historical_bundle(bundle, "/absolute/private/new-history-archive")
verified = verify_historical_archive(report["output_dir"])
```

Equivalent command, with an existing external parent directory:

```sh
uv run --locked python scripts/stock_data_preflight.py \
  --key-file /absolute/private/paper-key.json \
  --symbols SPY QQQ GLD USO \
  --start 2026-09-08T13:30:00Z \
  --end 2026-09-10T20:00:00Z \
  --interval-seconds 300 \
  --max-pages 10 --page-limit 1000 \
  --output-dir /absolute/private/new-history-archive
```

Bounds are 1–10 unique explicit tickers, a maximum 31-day window, 1/5/15/30-minute
bars, at most 20 pages and 20,000 requested rows in total. The default bound is
10 pages of 1,000 rows each. A response is capped at 2 MB and retained raw responses
at 24 MB. Requests have 10-second deadlines and the acquisition has a 300-second
wall-clock bound. There are no redirects or automatic retries. Truncated pagination,
repeated cursors, duplicate/reversed bars, malformed numbers, missing requested
symbols, and unsupported calendars refuse a canonical result.

The result is `HistoricalBundle(dataset, quality, receipt, captures)`. `dataset`
passes the existing `atlas.research.market` schema. `receipt` records each exact
request URL, local response-receipt time, raw hash, normalization, excluded row,
and current asset metadata. It also reports requested-window coverage per symbol,
including leading and trailing missing buckets that an observed-span report cannot
detect. Missing intervals are retained as gaps; their cause is unknown.

The archive is a new `0700` directory with `0600` files: `raw-NNN.json`,
`dataset.json`, `quality.json`, and a `receipt.json` manifest written last. Existing
destinations are refused. File data and directories are synchronized before success.
The verifier checks file digests and repeats conversion from raw responses. These
checks establish internal consistency, not authenticity against an independent
provider record. Generic Coinbase import archives retain their existing independent
verifier in `atlas/research/imports.py`.

## Information timing and limitations

The source is `real` for an actual provider request, but its evidence remains
`HISTORICAL_UNVERIFIED`, `availability_basis=unknown`, and
`revision_policy=latest_only`. Canonical information-availability and ingestion
times use the completed acquisition time. This is a conservative placeholder with
explicitly unknown historical availability, not a claim that bars first became
public then. Per-response receipt times remain in the raw manifest. Revision zero
means first imported archive value, not a recovered original publication.

Consequently, a causal evaluator sees this archive as one acquired batch. It cannot
infer historical trading decisions from unavailable publication history. Any
retrospective event-time diagnostic must be separately labeled as an assumption;
it must preserve this dataset rather than backdate canonical availability.

Data is unadjusted, IEX-only, and USD-denominated. Symbol remapping is explicitly
disabled with `asof=-`. Current ticker selection is subject to survivorship bias;
current asset status does not verify historical membership. Corporate-action cash
flows, executable spreads/depth, and actual trading costs are absent. Costs are
unknown, not zero. Raw splits can create discontinuities that require separate
corporate-action handling before performance interpretation.

GLD and USO are explicitly labeled exchange-traded commodity proxies. They are
equities in the canonical schema and do not implement gold/oil futures, multipliers,
expiry, margin, delivery, or contract rolls. Futures requests are rejected. Daily
provider bars are not imported through this fixed intraday contract; downstream
daily aggregation must use each actual regular-session calendar, including early
closes.

## Other providers

`DatabentoHistoricalAdapter` and `MassiveHistoricalAdapter` are explicit injection
contracts for a caller-supplied bounded capture fetcher and provider-specific
normalizer. Without both they return
`provider_unavailable_missing_credentials_and_verified_transport_contract`.
They have no built-in network transport, credential discovery, purchases,
subscriptions, account creation, or claimed verified market coverage. Their
injected results must preserve unknown historical availability, latest-only
revision status, raw prices, exact requested identity/window, and provider source
identity. A provider-specific replayable archive implementation remains required
before their bundles can use the Alpaca archive writer.

Readiness can be inspected without files or networking:

```sh
uv run --locked python scripts/stock_data_preflight.py --provider databento --capabilities
```

## Provider contract sources

Verified September 11, 2026 against official documentation:

- [Historical bars](https://docs.alpaca.markets/us/reference/stockbars): data origin,
  symbol-major pagination, inclusive API end, raw adjustments, IEX feed and explicit
  symbol mapping. The adapter implements an exclusive caller end with API end one
  second earlier and complete-bar filtering.
- [US trading calendar](https://docs.alpaca.markets/us/reference/legacycalendar):
  paper origin, inclusive trading dates, and provider open/close times.
- [Stock minute bars](https://alpaca.markets/learn/stock-minute-bars): opening-time
  labels and volume as the sum of shares traded. The adapter does no quote-size
  conversion and does not equate traded volume with available fill depth.
- [Market data plans](https://docs.alpaca.markets/us/docs/about-market-data-api):
  Basic IEX coverage differs from consolidated US exchange coverage.
- [Paper trading](https://docs.alpaca.markets/us/docs/paper-trading): paper fills
  and omitted costs are a separate simulation contract, not proof of execution.

Verification uses generated HTTP captures with networking disabled. Test outcomes
are software evidence only; actual provider access and actual dataset receipts
must be reported separately by the operator performing the selected GET batch.
