# Offline research workflow

ATLAS can now record and reproduce a bounded research experiment. It has not
established a profitable strategy. The current local inventory lacks suitable
point-in-time market history; generated data demonstrate software behavior only.

## One command, no account or service

```bash
uv sync --locked --group dev
uv run --locked python -m atlas research demo
```

The command generates a labelled fixture, reserves its entire trial plan, evaluates
it, exports hash-verified results, reproduces the artifact exactly, then removes
its temporary directory. To retain the fixture and evidence, select a directory
outside the repository:

```bash
uv run --locked python -m atlas research demo --store /absolute/external/research-results
```

The default plan evaluates four interpretable families at two lookbacks, five
baselines/ensemble controls, six additional-fee levels, three alternative execution
scenarios, nine perturbations, and up to six chronological fold tests. The primary
family/lookback is fixed before results exist. It receives the full gauntlet;
other candidates receive explicitly incomplete scorecards. There is no winner
selection or automatic promotion.

## Explicit external dataset

```bash
uv run --locked python -m atlas research run \
  --dataset /absolute/external/data.json \
  --store /absolute/external/research-results \
  --config /absolute/external/plan.json
uv run --locked python -m atlas research reproduce \
  --experiment-id exp_REPLACE_WITH_RETURNED_ID \
  --store /absolute/external/research-results \
  --dataset /absolute/external/data.json
```

A minimal plan is `{}`; all effective defaults are stored in the manifest. For
example, `{"campaign":"first-reviewed-history","seed":42,"lookbacks":[10],
"primary_lookback":10,"primary_family":"momentum","position_weight":0.2}`
predeclares a smaller study. Unknown options, arbitrary code, holdout roles and
unbounded searches are rejected. The current gauntlet accepts 100 or more original
publication events, at most 2,000 bars and four symbols, with one common interval.
This is a deliberately small engineering/research workflow, not bulk production
data infrastructure.

`market.py` defines the canonical JSON schema. A retained synthetic fixture is a
complete shape example, **not a template for inventing historical provenance**.
Required metadata includes source classification, venue, asset class, adjustment
policy, universe selection and survivorship status, revision policy, availability
basis, quote currency and volume unit (`shares`, `base_asset`, or explicitly `unknown`). Each bar
has event-close time, information-publication time, acquisition time, UTC/session
identity, original or later revision, OHLC and volume. Equity requires an explicit
versioned regular-session calendar; it does not assume holidays or adjustments.
Unknown volume cannot be treated as executable units. No gap, timestamp, label,
split or price is silently repaired. File assertions do not authenticate a provider.

## Evidence and lineage

The external SQLite registry commits manifests and artifacts transactionally;
immutable per-experiment JSON exports have file digests. Each manifest pins the
Git commit, actual source-file hashes, dependency lock, Python/package runtime,
complete dataset and observation hashes, identity keys, feature definitions,
strategy implementation/parameters, execution assumptions, split, seed and trial
plan. Failed and unfinished attempts remain visible. Unknown evaluated counts
remain unknown. Counts cover this store and campaign, not all historical research.

Reproduction requires exact source bytes, runtime, lock, data and metadata. A
different Git commit is reported explicitly and accepted only when actual source
bytes remain identical, allowing a documentation-only commit. Source changes after
import or during evaluation refuse a successful immutable result. Reproduction
compares exact canonical artifact hashes and does not create a new search attempt.

Dataset acquisition time is retained as provenance. Features use records whose
public availability was at or before the decision; later revisions cannot alter
earlier features. Exact input hashes plus pinned data/definitions reproduce the
inputs. Warmups reset on session changes and gaps. Labels resolve strictly before
their split boundary; timestamp-based folds purge unresolved labels and embargo
adjacent windows. Fixed rules do not require fitting. Fold tests start from fresh
cash; anchored versus rolling training windows do not represent different fitted
models, and overlapping modes are not independent confirmations. Purged label
indices govern fitting/label diagnostics; replay retains in-range market marks
and decisions, including open inventory at each fold end.

Holdout seals are an operational boundary in the trusted external store. Ordinary
research cannot inspect a sealed dataset or overlapping observation identities,
including aliases, revised prices or added rows. Missing identity coverage fails
closed. Already-researched observations cannot be relabelled a fresh holdout.
Claiming a seal is single-use; abandonment does not reset it. There is no CLI or
MCP path that unlocks, consumes or tunes on a sealed final evaluation. A reviewed
one-time protocol and separate untouched data are still required. These digests
detect corruption, not an administrator rewriting the code and database.

## What the simulator assumes

Replay fills an order only at a later published market event, after configured
latency. It uses known close/bid/ask and a shared fraction of completed volume.
That is a close-publication execution proxy, not an exchange queue or a promise
that the close was tradable. It supports market/limit orders, lot/minimum rules,
fees, spread, slippage, partial fills, expiry, cancellation, stale quotes,
cash/inventory reservations and strategy-owned cost bases. The sample end marks
remaining inventory rather than forcing liquidation. USD long-only cash portfolios
exclude dividends, borrow, financing and corporate-action processing.

Optimistic/normal/pessimistic/stress scenarios retain their named assumptions.
The 0/1/2/5/10/20-bps sweep adds fees **on each side above the base model** and holds
spread/slippage fixed. Zero additional fees is not a frictionless benchmark.
Break-even is an observed grid bracket, not an interpolated guarantee. Delay,
lookback, threshold, rebalance and start/end perturbations count as attempted
variants. Removing best closed fills/months is a post-hoc concentration diagnostic,
not a reconstructed executable path.

Cash, buy-and-hold and equal-weight ensemble comparisons use the same ordinary
evaluation interval. Exposure-matched buy-and-hold uses average decision weights;
the random control rotates the candidate's observed intent weights. Both are
conditional diagnostics: neither guarantees identical realized exposure/turnover
nor supplies a tradable forecast. Marginal ensemble superiority is not established
by averaging correlated hypotheses.

Circular block resampling preserves some local dependence but assumes stability
and a chosen block length. Conditional loss/ruin frequencies describe resampled
observations, not future market probabilities. The joint maximum-mean diagnostic
is a limited multiple-comparison check; different-window/previous/unpublished
attempts leave family coverage incomplete. Deflated and probabilistic Sharpe remain
unavailable. Resampling uses the union of publication timestamps, including
revisions; irregular/multi-symbol observations are event returns, not uniform
calendar periods. Annualized metrics require an explicit convention and regular enough
observations. Daily tail-risk estimates require at least 250 daily returns and
remain descriptive. Sparse samples never become significance claims.

Method references: [time-series validation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html),
[bootstrap confidence interval caveats](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html),
and [dependent resampling and block-size sensitivity](https://arxiv.org/abs/1204.1035).
The timestamp purge and block bootstrap here are explicit project implementations,
not claims to reproduce an entire published validation methodology.

## Paper, shadow and health

`ReplayEngine(..., state_path=external_path)` can advance bounded events, close and
resume with identical market/decision/model inputs. Its separate database pins cash,
cursor, positions, fees, orders and events; it does not reseed on restart. The legacy
instant paper simulator also pins its cost model and a monotonic mutation clock.
Its older schema refuses automatic migration because prior fee assumptions cannot
be reconstructed safely; no existing account file was changed by this work.

`assess_health` compares explicit reference/current feature rows with sample floors,
standardized shifts and empirical CDF distance. It returns no IID p-values.
`HealthStore` persists observations and keeps `DISABLED` sticky across restart.
Schema mismatch, stale/invalid data and malformed features disable the observation;
small samples remain uncertain. No health status can reactivate a strategy or
issue an order. Retrospective replay is not labelled prospective paper or shadow
success. No real-time feed or service is started by these commands.

Optional `portfolio_risk` settings bind drawdown, gross/net leverage, asset-class
and correlated-group exposure, position count, turnover, projected volatility
and health/permission checks into the service policy fingerprint. The service
requires a trusted Python-assembled provider, refreshes it before each risk check,
and binds its equity, positions and mark to the current broker snapshot. Config
files cannot supply executable providers or substitute AI-authored snapshots.
Missing required data/provider blocks an order; configured volatility limits need
an exact-order-bound projection with a declared return horizon. Global, broker,
account, symbol and data permissions apply to exits too. A proven inventory-backed
exit may reduce breached entry caps or a disabled strategy, without reactivating it.

## AI and operational boundaries

An optional external `research_store` in service configuration adds 20 read-only
research MCP tools: experiment lookup/list/compare/status ordering and saved
quality, scorecard, health, regime, robustness, costs, feature definitions/drift,
exposures, drawdowns, Monte Carlo, baselines, thesis, execution, shadow and anomaly
reports. Missing evidence stays unavailable. Queries are bounded; AI text is
attributed context and cannot become data, code, probabilities, policy, approval
or a strategy activation. Configured research stores disable disposable legacy
benchmark computations; the explicit CLI produces registered evidence instead.

Server deployment remains a separate, unperformed operation. Before it can be
considered: provision reviewed data, durable external state and backups, validate
recovery, supply trusted risk observations, and complete a prospective paper/shadow
protocol. Existing live factory refusal and approval boundaries remain in force.
