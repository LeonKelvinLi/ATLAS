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

## Observed diagnostic result

Evidence class: **`HISTORICAL_UNVERIFIED`**. `alpha_validation_eligible: false`.

**No candidate demonstrates sufficient evidence.** The primary is `REJECTED`
within this diagnostic. This does not prove that an entire strategy family can
never work; it rejects this parameterization's observed result under these
assumptions. The importer and exact reproduction succeeded.

Both archives retain **288 expected hourly candles, zero missing buckets and one
explicitly excluded endpoint row**. There are zero spread observations and no
recovered historical revision stream. The acquisition-only archive preserves
unknown publication and volume units. Only the separately labeled assumed-history
archive was used for the experiment.

One immutable attempt completed **33 of 33 predeclared variants**: four candidates,
five controls/ensemble, six additional-cost levels, three execution scenarios,
nine perturbations and six fold replays. These are not 33 independent strategies.
The primary alone receives the complete gauntlet; other candidate scorecards
explicitly state their partial coverage.

All figures below are modeled net marked portfolio returns over the same
development evaluation window, **September 5 at 05:01 UTC through September 10
at 00:01 UTC**. Annualized returns and Sharpe remain unavailable.

| Fixed strategy/control | Net return | Interpretation |
|---|---:|---|
| Momentum, lookback 10 — primary | **−0.244%** | Rejected; below cash and conditional controls |
| Mean reversion, lookback 10 | −0.271% | Rejected; partial scorecard |
| Moving-average rule, lookback 10 | 0.000% | No fills; no superiority to cash |
| Volatility-scaled momentum, lookback 10 | −0.076% | Rejected; partial scorecard |
| Cash | 0.000% | No exposure |
| Full-allocation buy-and-hold | −1.720% | Different exposure from the primary; not a matched-risk win |
| Exposure-conditioned buy-and-hold | −0.049% | Conditional diagnostic; realized exposure not exactly matched |
| Conditional random control | +0.026% | Uses observed intent weights; not a deployable selected strategy |
| Equal-weight ensemble | −0.142% | No positive result |

The primary had **70 usable feature timestamps**, **14 order intents**, **four
simulated fills** and **two realized sell fills**. Ten intents were rejected as
below-minimum/no-change. Sell fills are not an independent-trade sample. The
modeled primary loss was **$24.36 on $10,000 starting cash**, with observed maximum
drawdown **0.293%**. There was no real account loss or live execution.

Adding 20 fee bps on each side worsened the primary return to **−0.403%**. It was
already negative at zero *additional* fees; that case still includes normal
fees, spread and slippage. The saved cost report's
`execution_assumptions_verified` field checks consistency across the modeled
cost sweep, not whether actual venue execution assumptions are authenticated.
Similarly, replay's `data_quality_passed` is a simulator setting, not the dataset's
alpha-validation eligibility. The separately assessed strategy-health report is
`DISABLED` because the research data-quality gate is not satisfied.

## Exact identity and reproduction

Implementation commit: `62267f4b51f1332227357cd0ef792af23f689f74`.

Experiment: `exp_c22c0ebc0b7c4ba2bd504154248187c5`.

Result SHA256:
`7f22214c8648c574ba61b4501e6be5605a1eef959ff5a102a72bef3dd2bafa45`.

Assumed-history canonical dataset SHA256:
`1032eccd44b7ead1fecac208cc3ce43b3b535b785ee28fc13313eba6d6f333cb`.

The retained bundle has **39 files / 1,509,627 bytes**, including its manifest,
quality report, source and feature lineage, complete trial plan, orders, fills,
equity history, scorecards, costs and limitations. The exact reproduction check
returned `reproduced: true`, `identical_source_bytes: true` and
`new_attempt_recorded: false`. The report-only commit after the implementation
does not alter its source bytes. Future source changes require the pinned
implementation for reproduction.

```bash
uv run --locked python -m atlas research run \
  --dataset /Users/leon/Desktop/dev/ATLAS-research-evidence/data-intake-20260909/assumed-history/dataset.json \
  --store /Users/leon/Desktop/dev/ATLAS-research-evidence/data-intake-20260909/research \
  --config /Users/leon/Desktop/dev/ATLAS-research-evidence/data-intake-20260909/plan.json

uv run --locked python -m atlas research reproduce \
  --experiment-id exp_c22c0ebc0b7c4ba2bd504154248187c5 \
  --store /Users/leon/Desktop/dev/ATLAS-research-evidence/data-intake-20260909/research \
  --dataset /Users/leon/Desktop/dev/ATLAS-research-evidence/data-intake-20260909/assumed-history/dataset.json
```

The first command above records the actual completed run; use **reproduce** to
check it rather than creating another experiment. Command arguments, exit codes
and outputs are retained as `diagnostic-command.json` and
`reproduction-command.json`. A compact inspection is saved in
`diagnostic-summary.json` beside them.

Open the [compact diagnostic summary](/Users/leon/Desktop/dev/ATLAS-research-evidence/data-intake-20260909/diagnostic-summary.json),
[import quality report](/Users/leon/Desktop/dev/ATLAS-research-evidence/data-intake-20260909/assumed-history/quality.json),
or [primary scorecard](/Users/leon/Desktop/dev/ATLAS-research-evidence/data-intake-20260909/research/results/exp_c22c0ebc0b7c4ba2bd504154248187c5/scorecard.json)
for the exact saved values.

## Final verification and Git state

| Check | Observed result |
|---|---|
| `uv sync --locked --group dev` | Success; 78 packages resolved / 67 checked |
| `uv run --locked python -m pytest -q -ra` | **1,252 passed, 2 skipped in 44.43 seconds** |
| Targeted importer, CLI and market tests | **108 passed** |
| New CLI plus existing interface checks | **33 passed** |
| Undefined-name checks for new/edited research modules | Passed |
| `git diff --check` | Passed |
| Both public-data import archives | Exact raw/conversion verification passed |
| Diagnostic artifact reproduction | Exact canonical artifact hash matched |
| Eight frozen model/protocol/history files | All byte hashes match pre-change snapshot |
| Experiment source files | All 82 match the completed experiment's pinned bytes |
| ATLAS application process check | None detected at final handoff |

The skips require the optional legacy `iqoptionapi.constants` package and an
explicitly selected private DuckDB dataset. The new tests use generated
provider-shaped fixtures, block network and broker operations, and exercise
timestamp mapping, precision, ordering, incomplete data, window coverage,
assumptions, tampered receipts/files, special files, symlinks and overwrite refusal.
Independent code review and the first real import exposed no unresolved failure.

Work remains on `codex/atlas-offline-onboarding`. The implementation and this
subsequent report are committed locally, eight commits ahead and zero behind
the read-only verified remote main
`9800677ec1a5fa7a1c53e3e4e6b1554ea8085378`. Nothing was pushed. No brokerage
authentication, order submission, deployment, service, listener, startup job or
one-time forward evaluation occurred. The public HTTP GET was read-only market
data acquisition. No ATLAS application process was started; final handoff checks
the working tree and process state separately.
