# Multi-asset research and paper preparation

This development extension is local and has **no real-order or funding path**.
It preserves the existing Coinbase simulator and the halted Alpaca crypto pilot.
The shared research ledger starts with one hypothetical $100. Independent
strategy experiments also use $100 for comparison; they are alternative trials,
not allocations to multiple accounts.

## Strategy contract

`atlas/research/multiasset_strategies.py` registers 59 bounded hypotheses:

| Family | Hypotheses | Assets and bars | Mechanisms |
|---|---:|---|---|
| Equity mean reversion | 14 | SPY/QQQ, 15 minutes | z-score, VWAP deviation, Bollinger, RSI, EMA deviation, ATR deviation, opening range; lookbacks 10/20 |
| BTC breakout | 20 | BTC-USD, 15m/30m/1h/2h/4h | Donchian, prior-period high, consolidation, ATR-distance breakout with volume confirmation |
| Commodity proxy trend | 25 | GLD/USO, 1h/2h/4h/8h/daily | momentum, moving average, Donchian, regression slope, pullback |

Each specification runs at 0.25%,0.5% and 1% account-risk budgets. The initial
catalog is deliberately bounded; parameters can be declared through
`normalize_spec` before a new experiment. A larger search is a new multiple-test
family and is not permission to retune against a previously inspected test.

Bars end at `event_time`; features use only information published by their
`decision_time`. Revisions affect later snapshots, never historical decisions.
Features retain hashes of the actual constituent OHLCV observations. The regime
classifier distinguishes range, trend, high volatility and crash using trailing
path efficiency, ATR/price and negative returns/drawdowns. Mean reversion is
disabled in directional/shock regimes; momentum/trend families require the
appropriate conditions. These fixed heuristic definitions are hypotheses.

VWAP here is the **prior-lookback volume-weighted typical price**, not an anchored
full-session VWAP. RSI uses simple trailing gains/losses, and ATR uses trailing
true range. These exact definitions are exposed in signal metadata. Intraday
means reset at session boundaries. Trend continuity uses an explicit session
calendar, including early closes and missing-session resets. Aggregation never
fills missing intervals or crosses sessions to fabricate bars.

GLD and USO are ETFs, not gold/oil futures. A regular U.S. session cannot form an
eight-hour bar; that variant is recorded unavailable. Futures multipliers,
expiry, rolls, margin and delivery need a separate verified instrument/data
contract. Overnight-gap/pairs/futures-divergence, macro-news calendars, open
interest, order-book confirmation and additional ETF/crypto universes remain
future extensions, not claims about the current 59 variants.

## Portfolio and execution

`MultiassetReplay` extends the existing replay accounting engine with:

- ATR sizing that includes modeled round-trip fees, spread and slippage.
- One cash ledger, pending-order cash/risk reservations, no leverage or shorting.
- Default 35% per-position/cluster exposure, 1% cluster planned risk and 2% total
  planned risk; risk is rechecked before accepting and filling orders.
- Rolling correlations from matching return intervals available at that event.
  SPY/QQQ/ES/NQ share a permanent equity-beta bucket. Unknown correlations are
  linked conservatively instead of assuming diversification.
- A persistent 10% drawdown entry halt and 2% daily marked-equity loss entry halt.
  The daily clock is UTC and includes overnight gaps from the previous mark.
- Sticky protective exits, partial fills, latency, shared bar participation,
  rejected/expired orders, venue downtime and stale-market refusals.
- Exact input/model/risk hashes in transactional SQLite checkpoints. Resume
  requires the original state and creation marker; missing state cannot reseed
  cash. Protective stops and halts survive restart.

The default Coinbase cost scenario is 120 bps per fill, consistent with the
separately observed September11 fee preflight, plus 10 bps spread and 5 bps slippage.
The equity scenario uses a modeled 1 bp fee reserve, 5 bps spread and 5 bps slippage.
These are research assumptions, not current broker quotes. Every candidate is
rerun at double costs, 600-second order latency, 60-second entry delay and a
one-hour outage. Partial fills use 1% of reported completed-bar traded volume;
this is not executable market depth or an exchange queue model.

A stop is observed only when a bar is published. Its exit can fill only at a
subsequent available event and can gap past the intended budget. Loss limits
block new entries; they do not guarantee bounded losses or instantly liquidate
an unavailable market. Stale held equities conservatively block mixed-venue
fills outside market hours. There is no corporate-action/dividend/interest
engine or intrabar execution path.

## Walk-forward selection and evidence

`atlas/research/multiasset_lab.py` freezes the plan, input hashes and source hashes
before evaluation. It schedules chronological train/validation/test windows with
embargo and outcome-horizon purge contracts. Warmup can use earlier published
observations but cannot bring positions or P&L into a fresh evaluation window.
The complete validation ranking is saved before any test signals are generated.
Test availability and returns cannot change that ranking.

Numeric selection maximizes validation CAGR subject to ≥30 completed flat-to-flat
trades, ≥60 daily observations, positive net return, ≤10% drawdown, Sharpe≥1.2,
profit factor≥1.25, known modeled costs, complete daily calendar and fresh
terminal valuations. Partial sell fills are not separate trades. Missing sample
size or annualization is unavailable, never zero. Sortino, Calmar, expectancy,
turnover, costs, volatility, worst day/month and recovery time are also reported.

`pre_cutoff` only proposes paper eligibility from a selection made before a
completed later OOS window, whose end precedes allocation. Synthetic/test and
historically unverified evidence can never pass. The current archive CLI has no
authenticated point-in-time evidence route, so it keeps the tournament cash
unallocated even if a descriptive backtest happens to meet numeric thresholds.
A future verified forward experiment requires separate provenance approval and
a new execution connection; changing an evidence-class string is insufficient.

Daily block-bootstrap and completed-trade-order permutation diagnostics are
conditional sample stress tests. They are unavailable with too little data and
do not forecast probabilities. Prespecified lookback/risk variants are reported
as sensitivity groups, without test-window tuning. A separate fresh forward
period is needed after inspecting results.

Benchmarks are independent $100 SPY, QQQ and relevant underlying buy-and-hold
replays with the same windows/costs, next-bar entry and terminal marked inventory.
A benchmark without an actual simulated entry fill is unavailable. Cash assumes
zero interest. 60/40 and T-bill benchmarks require additional return series.

## Commands and artifacts

Run from the repository after `uv sync --locked --group dev`:

```sh
# Engineering demonstration only. No credentials, network or broker process.
uv run --locked python scripts/multiasset_research.py \
  --synthetic --synthetic-days 12 --max-folds 2 \
  --output-dir /absolute/existing-parent/new-synthetic-campaign

# Explicit retrospective assumption; original archive remains untouched.
uv run --locked python scripts/multiasset_research.py \
  --archive /absolute/existing-iex-archive \
  --assume-archive-close --max-folds 2 \
  --output-dir /absolute/existing-parent/new-history-campaign
```

The archive must verify against raw captures first. `--assume-archive-close`
creates separate `derived-research-bars.json`, retaining original capture times
and the explicit unverified close-time assumption. Without that flag the
acquisition batch has no historical trading opportunities, and typically no
walk-forward windows. It is not silently backdated.

Each new external 0700 campaign directory contains 0600 immutable files:
`manifest.json`, input receipts, full validation snapshots, per-fold results,
`results.json`, `REPORT.md`, gzipped detailed test reports, and a durable cash-only
tournament checkpoint. Strategy and benchmark replay use full resolution; an
unallocated cash ledger only records the last observed quotes per UTC day.
Existing campaigns refuse overwrite. Failed runs remain for diagnosis.

## Brokerage and data readiness

[Alpaca equity paper preparation](multiasset-equity-paper.md) maps the official
paper request/response contract through fixtures. It cannot connect or place an
order. The old Alpaca account 403 refusal remains unresolved. Successful IEX GETs
do not demonstrate order acceptance. [Historical intake](multiasset-data.md)
explains the implemented Alpaca adapter and future Databento/Massive contracts.

Robinhood now documents an official Agentic account/integration, but this run did
not activate it or verify a paper sandbox for it. Alpaca remains the current
research contract because it explicitly documents a paper environment. This is
an integration choice, not a claim that either broker improves strategy returns.
[Robinhood overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/),
[Alpaca paper environment](https://docs.alpaca.markets/us/docs/paper-trading).

LEAN has not been installed or substituted for ATLAS's existing audited replay.
Independent LEAN parity checks would be an additional validation layer, with
explicit fees/fills and the same data assumptions. Provider subscriptions,
futures/order-book history, 60/40/T-bill series, fresh forward evidence and an
accepted authenticated equity paper session remain outstanding. No live
activation, funding, service or scheduled task was performed.
