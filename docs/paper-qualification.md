# Paper qualification evidence

`atlas.paper.qualification` assesses explicit, already-produced JSON records. It
does not run a strategy, fit a model, open an archive or holdout, read a credential,
contact a broker, inspect the collector VM, or authorize an order. It writes no
state. These checks are separate from the paper execution engine.

The current development can qualify engineering behavior with fixtures. The
earlier Alpaca connectivity pilot still has two rejected attempts, no accepted
order lifecycle, and no remaining attempts. This module cannot reset that pilot
or resolve the broker's 403. See `alpaca-paper-pilot.md`,
`alpaca-paper-support-followup.md`, and `multiasset-equity-paper.md`.

## Separate decisions

| Report area | What a passing result establishes | What remains outside it |
| --- | --- | --- |
| `infrastructure.engineering` | Supplied, internally consistent review records cover each engineering check | Actual broker acceptance, independent authentication, unattended service reliability |
| `strategy.numeric` | Supplied returns and completed-trade records meet the existing multiasset numerical floors | Statistical significance, an unseen experiment, a profitable executable strategy |
| `strategy.walk_forward` | Supplied fold metadata follows its declared chronology and numerical floors | Independent folds, a complete experiment family, correct upstream timestamps |
| `strategy.research_reviews` | All declared review categories have supplied, hash-bound conclusions | Independent verification of those conclusions and their referenced artifacts |
| Overall `status` | Evidence assessment: `FAIL` for an observed failed check; `PARTIAL` for missing evidence | Permission to run a paper or live strategy |

The current offline assessor deliberately cannot return overall `PASS`: external
origin and independent predeclaration remain unverified. Even a fully fabricated
bundle claiming `PAPER`, with internally consistent hashes and every review marked
`PASS`, leaves `origin_authenticated`, `prospective_qualified`,
`paper_execution_authorized`, `activation_authorized`, and `live_approved` false.
No configuration flag can remove this boundary. A future trusted evidence source
requires a separately reviewed integration; passing booleans to this assessor is
not one.

## Frozen rules stay authoritative

The canonical thresholds are `atlas.collection.primitives.DEFAULT_THRESHOLDS`:

| Metric | Existing floor |
| --- | --- |
| Completed flat-to-flat strategy/version/symbol episodes | At least 30 |
| Observed daily valuations | At least 60 |
| Net return, after costs | Greater than zero |
| Maximum observed drawdown | At most 10% |
| Sharpe | At least 1.2 |
| Profit factor | At least 1.25 |

The existing `metrics_for` and `gate_result` implementations compute the numbers
and apply these floors. The assessor never forwards caller provenance booleans
into `gate_result`. A complete calendar, fresh terminal marks, and known
fees/spread/slippage remain required. Missing values stay unavailable. Partial
exits cannot inflate completed-trade counts, and completed trades are not assumed
independent. A missing loss denominator leaves profit factor unavailable.

The original `FORWARD_TEST.md` binary-payout hypotheses and one-time evaluator are
separate. Their historical win-rate, Brier, multiplicity and power rules do not
transfer to equity or spot-crypto strategies. This module changes none of them.
Data Contract V1 still requires the elapsed sealed window and matching strategy
digest frozen before its holdout. No report produced here opens that seal.

## Python interface

```python
from atlas.paper.qualification import build_policy, assess_qualification

policy = build_policy(
    registered_at=declared_utc_seconds,
    evaluation_start=start_utc_seconds,
    evaluation_end=end_exclusive_utc_seconds,
    manifest_sha256=existing_manifest_hash,
    strategy_sha256=existing_strategy_implementation_hash,
    data_contract_sha256=existing_contract_hash,
    risk_policy_sha256=paper_risk_policy_hash,
    walk_forward_plan=declared_fold_definitions,
)
assessment = assess_qualification(
    policy=policy, report=explicit_report, manifest=explicit_manifest,
    evidence=explicit_inventory, infrastructure=engineering_reviews,
    walk_forward=explicit_fold_results, reviews=research_reviews,
    cost_runs=already_computed_cost_runs, stress_reports=already_computed_stress_runs,
    calendar="UTC_SESSION_DAYS", session_days=explicit_complete_session_days,
)
```

The example names are objects provided by the caller, not filesystem paths.
`report`, `manifest`, the inventory, and review records may be omitted to obtain an
honest incomplete assessment. Invalid schemas, credentials, nonfinite numbers,
oversized bundles and changed policy criteria raise `ValueError`. A mismatched
supplied report, manifest, or review digest produces a failed identity check.

The policy fixes the registered evaluation window, exact manifest, strategy,
contract and risk-policy identities, fold definitions, numerical floors and review
inventory. Policy creation only describes a declaration: its timestamp and hash
do not independently prove it existed before evaluation. A different policy hash
must be treated as a different proposal; rewriting and rehashing a failed study
does not create a new unseen result.

Hashes use `atlas.research.store.content_hash`, with its canonical JSON and
credential-field rejection. The supplied experiment manifest must also satisfy
the existing `validate_manifest` rules, including source, lock, runtime,
dataset/partition, strategy parameters, features, execution assumptions, split
and seed. Sealed-holdout dataset roles are refused. The report uses the existing
`metrics_for` schema: equity curve, chronological fills, initial cash, portfolio
terminal valuation and execution-quality cost records. Capital is the declared
$100 starting allocation; later marked equity can grow above $100. Cash flows
need separately reviewed adjusted accounting and currently block qualification.

The inventory has exactly these fields:

```json
{
  "policy_sha256": "<64 lowercase hex>",
  "manifest_sha256": "<64 lowercase hex>",
  "report_sha256": "<64 lowercase hex>",
  "lineage_classes": ["TEST"]
}
```

`TEST`, `SYNTHETIC`, `HISTORICAL_UNVERIFIED`, and inspected historical OOS evidence
cannot become prospective observations through relabeling. Declaring one of these
ancestors beneath a `PAPER` or `SHADOW` report fails. Separate historical fold
reports retain their own class; synthetic folds cannot establish paper strategy
qualification. Omitting or fabricating lineage cannot establish authentication.

Each named engineering or research review has exactly `status` (`PASS`, `PARTIAL`
or `FAIL`), `policy_sha256`, `evidence_class`, `evidence_refs` (unique SHA256s),
`note` (a substantive explanation), and `sha256` (hash of the other five fields).
References are recorded but never resolved or opened. A digest proves consistency,
not the source's authenticity or the reviewer's competence. Bare booleans are
rejected. Required categories are exported as `INFRASTRUCTURE_CHECKS` and
`RESEARCH_REVIEWS`; unknown categories are rejected.

Each walk-forward plan entry contains `ranges` (`train`, `validation`, `test`,
each with `start` and `end_exclusive`) and `embargo_seconds`. Results contain
`fold` (that definition plus the three sample-index lists), explicit `samples`
with event/availability/label-publication metadata, `selected_at`,
`selection_split`, and `report`. Existing `validate_fold` enforces purging and
embargo chronology. Selection must occur after validation finishes and strictly
before test begins; test windows cannot overlap. At least two predeclared,
completed folds are needed to describe stability. Their numerical results remain
separate; neither positive-fold fraction nor pooled trade counts prove independent
confirmation. No candidate is ranked using test returns.

## Diagnostic and review coverage

The assessor reuses existing financial diagnostics rather than creating a new
statistical engine. It records drawdown, Sharpe/Sortino where sample size and
denominators permit, net expectancy, wins/losses, average win/loss, turnover,
costs, concentration with best trades/month removed, and historical daily tail
risk. Sharpe uses the explicitly selected calendar and a zero risk-free-rate
assumption. The default is `UTC_24_7`; equity callers must supply
`UTC_SESSION_DAYS` and a complete externally verified exchange-session calendar.
Intraday rows are not independent daily observations. Daily historical VaR/ES
remain unavailable below 250 returns and are descriptive even above that floor.

`cost_runs` is a mapping from additional fee basis points per side to supplied
reports; spread, slippage, latency and other execution assumptions must remain
fixed for a valid comparison. Execution-model metadata is required to verify that
consistency. `stress_reports` accepts only the already declared `double_cost`,
`latency_600s`, `entry_delay_60s`, and `one_hour_downtime` scenario names. All
comparisons must share evidence class and valuation window. The assessor never
generates these runs; scenario names alone do not establish their assumptions.
Observed fragility remains visible and requires review, without rewriting the
original frozen acceptance thresholds.

Research reviews must address PIT timing and revisions; leakage and sealed
holdout lineage; all attempted/failed variants and overfit protection; dependence,
effective sample size and power; predeclared regime definitions and regime
coverage; comparable after-cost benchmarks; fee corrections and cash flows;
measured slippage/latency/downtime sensitivity; and concentration/tail limits.
Independent sample size, verified regime results, deflated Sharpe and probabilistic
Sharpe remain unknown here. A review record is not a substitute for the underlying
analysis. Missing regimes, short samples or incomplete attempted-family coverage
must remain `PARTIAL`; an observed failed criterion must remain `FAIL`.

The progression is engineering evidence, independently authenticated account and
execution evidence, trustworthy frozen OOS evidence, and a sufficiently observed
prospective paper/shadow period under the unchanged study rules. Passing an
earlier stage never skips a later one or authorizes real-money trading.
