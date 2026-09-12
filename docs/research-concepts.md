# Precise screens, cost hurdles and bounded campaigns

These offline additions turn the September 2026 Instagram research concepts
into reproducible observations. They do not enable broker orders, identify a
profitable strategy, or change the frozen strategy catalog and historical results.

## RSI and volume observation

`atlas.research.causal_screener.scan_dataset(payload, spec, *, as_of, now)`
accepts one canonical market dataset. Specify the exact instrument contract:

```python
spec = {
    "symbol": "BTC-USD", "venue": "coinbase-exchange",
    "asset_class": "crypto", "currency": "USD", "timeframe_seconds": 900,
    "rsi_length": 14, "volume_lookback": 20,
}
report = scan_dataset(payload, spec, as_of=cutoff_utc, now=validation_clock_utc)
```

Defaults mean **simple trailing gain-share RSI strictly below 30** and
current volume **at least three times the mean of 20 prior bars**. The volume
baseline excludes the current bar: a 200% increase means 3x, not 2x. RSI is
explicitly not Wilder smoothing or a claim of TradingView numerical parity.

All contributing inputs must have closed, been published and been ingested by
`as_of`. Later valid bars/revisions cannot change a previous observation or its
visible-input hashes. Equity intraday warmup resets at each session; daily equity
continuity follows the declared calendar. The report distinguishes the last
bar timestamp from the maximum publication/ingestion time of the features.

Missing volume units, warmup, revision ancestors or required aggregate bars
produce `UNAVAILABLE` with `matched: null`. `AVAILABLE` with `matched: false`
means the screen was evaluated and did not match. Neither permits trading:
`trading_eligible` is always false. Observation age is reported without declaring
the quote fresh or executable. Futures are not silently mapped to spot BTC.

Input limits are 10,000 bars, 8 MiB of finite JSON and bounded aggregation work.
Native one/five-minute bars are supported, while resampling into sub-15-minute
bars is explicitly unavailable. Raw inputs are never modified.

## Exact economics

`atlas.research.economics.execution_break_even` separates the midpoint price
gain required to cover execution from the loss on an allocated budget:

```python
from atlas.research.economics import execution_break_even

receipt = execution_break_even(
    buy_fee_bps=120, sell_fee_bps=120,
    entry_spread_bps=5, exit_spread_bps=5,
    entry_slippage_bps=5, exit_slippage_bps=5,
    allocation_usd="100",
)
```

This is an explicit scenario, not a current fee/spread quote. Both spread inputs
are **per side**, measured relative to the midpoint. Additional slippage adds to
that side's spread. These are not direct `ReplayEngine` profile units: its full
spread and quote-relative slippage need a deliberate conversion. Costs must not
be counted twice. Fee-only break-even is `(1+buy_fee)/(1-sell_fee)-1`.

The calculation uses Decimal and continuous quantities. Broker lot increments,
fee rounding, taxes, extra commissions and funding are not inferred. The
allocated-budget loss denominator is `allocation_usd`, not the whole account.
Reducing an order to $10 reduces dollar loss, not the price-gain hurdle.

`fixed_overhead_hurdle(monthly_costs_usd, *, period_start, period_end,
initial_capital_usd="100")` attributes tool/data/model/server expenses across
actual UTC calendar months. All four mapping keys (`tool`, `data`, `model`,
`server`) must be supplied; missing/None remains unknown and explicit zero means
zero. Monthly proration is an economic attribution, not a provider billing rule.

`overhead_adjusted_pnl` subtracts that overhead from an already-net replay PnL.
It requires `replay_complete=True` and
`execution_costs_already_included=True`. Optional replay period endpoints must
both be present and match the overhead interval exactly. No execution cost is
deducted again; no historical ledger or cash balance is changed. A $1 input PnL
minus $10 overhead is a -$9 economic result, not proof of a cash debit.

## Campaign preflight

`multiasset_lab.run_campaign` now runs `campaign_preflight(plan)` before output
creation. The receipt is frozen in `manifest.json`, included in `results.json`
and summarized in the report. Two optional plan fields have explicit defaults:

```json
{"research_purpose": "diagnostic", "max_replay_evaluations": 5000}
```

The replay bound includes train, validation, test, all four stress reruns,
five benchmarks per fold and the final cash ledger. The default 59-specification,
three-risk, three-fold plan allows at most 3,733 replays. The budget is an integer
in 1..21,000. Exceeding it rejects the plan before file creation. This bounds run
count, not CPU, memory or every bootstrap operation. Actual unavailability may
reduce execution without shrinking the frozen hypothesis family.

Diagnostic windows remain short for engineering checks. `qualification` purpose
requires each split to last at least the configured minimum daily-observation
count in elapsed days (60 by default). This conservative planning policy is not
an estimate of exchange sessions or completed trades. The receipt separately
reports an optimistic UTC-date ceiling and never claims qualification.

Outside-campaign attempt history remains unknown. Risk settings, overlapping
folds and stress reruns are dependent comparisons, not independent trades or a
complete multiple-testing correction. Reused history remains retrospective.
No old gate, model, selection order, strategy rule or account permission changes.

## Verification

The 169 new tests cover fee asymmetry and denominators, unknown costs,
calendar-month proration, double-charge prevention, causal prefixes and revisions,
volume baselines, missing data, complete replay budgets and early rejection.
The full checkout passed **2,355 tests with 2 optional skips** on September 11,
2026. All eight frozen research/model artifacts matched their earlier hashes.

Example evidence uses a declared synthetic downward-price/3x-volume fixture that
matches, and the selected saved Coinbase archive that remains unavailable because
its volume unit is unknown. These are software/data-contract observations, not
a newly discovered winning backtest.

Research sources include [TradingView's official MCP catalog](https://www.tradingview.com/mcp/docs),
[Bailey and Lopez de Prado on selection inflation](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf),
and [Coinbase's account-specific fee guidance](https://help.coinbase.com/en/coinbase/trading-and-funding/advanced-trade/advanced-trade-fees).
The detailed claim review and calculations are in the separate ATLAS research
evidence report; company returns and social dashboard figures are not imported
into ATLAS as verified performance.
