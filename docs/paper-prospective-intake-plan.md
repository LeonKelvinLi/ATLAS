# Prospective paper intake: implementation and activation boundary

Implementation continuation: September 15, 2026, based on paper branch `056e837`.

The PAPER execution engine, separate bounded intake/projection library and
explicit reconciliation/submission adapter are implemented. The design below
is retained as the reasoning for that boundary.
No strategy, research holdout or qualification rule changes are involved. No
actual broker session was run and local hashes do not authenticate a provider.
The current interface and remaining activation work are recorded at the end.

## Existing components reused by the intake

- `atlas/paper/strategy.py` accepts explicit `TEST` or
  `HISTORICAL_UNVERIFIED` bars only. Its result remains `simulation_only` and
  does not grant execution authority. The six hashed source files and normalized
  specification bind the offline bridge. The separate intake adds its own
  implementation identity without changing that bridge's accepted provenance.
- `atlas/research/data_archive.py` verifies an append-only local capture chain.
  Built-in intake can create `prospective_observation` lineage after registration;
  imports, injected clocks/transports and outage backfills cannot gain that
  lineage through a later revision. Verification explicitly leaves
  `provider_authenticity_verified` false.
- `atlas/research/data_contract_v1.py` calls those observations
  `PROSPECTIVE_OBSERVATION_UNVERIFIED`. This is a distinct class, not `PAPER`,
  a provider signature, or independently qualified evidence.
- `read_bucket` applies the existing elapsed-window and frozen-digest holdout
  barrier. The collector uses daily partitions through `atlas/collection/archive.py`;
  its public verification result contains counts, not a strategy snapshot.
- The canonical strategy library consumes a complete local revision chronology.
  It groups decisions by `information_available_time` and requires consecutive
  revision numbers. `as_of_records` instead selects only the latest visible
  revision. Passing its output straight into the library can either omit the
  original revision or represent a late correction incorrectly.

These existing components do not establish the source-to-execution boundary on
their own; the separate intake implements the behaviors described below.

## Boundary design

1. **Register a separate paper observation stream.** Keep the existing research
   archive and sealed holdout outside the execution input paths. Registration
   binds a single normalized existing strategy, its exact implementation, the
   data contract, runtime/lockfile, paper risk policy, account fingerprint and
   future evaluation start. It is append-only and cannot be backdated. A local
   registration is a recorded declaration; independent predeclaration remains a
   separate evidence requirement. Historical warm-up retains its original class
   and must be reported separately from post-registration observations.

2. **Own the source-to-receipt path.** Reuse the pinned Alpaca GET parser and V1
   record validation with a dedicated selected data credential. Retain exact raw
   responses, request metadata, receipt clocks, ingestion clocks and chained
   immutable receipts before admitting inputs. The public runtime must not accept
   arbitrary bars, an HTTP host, a replacement transport, or a caller's
   `authenticated=true` flag. Tests use a separately marked injected path whose
   complete lineage remains `TEST`. Label an owned HTTPS collection path
   explicitly; do not turn that transport observation into a provider signature,
   proof against owner tampering, or overall qualification.

3. **Read one bounded, consistent snapshot.** Use a public archive API that owns
   the entire read transaction and returns contract/registration identities,
   selected stream identity, capture/batch heads, all visible revisions needed
   for the strategy, and visible calendar assertions. Enforce both receipt and
   ingestion visibility before exposing payloads to strategy code. Fail closed on
   missing predecessors, inconsistent partition contracts, changes to source,
   contradictory calendars, incomplete pages and capacity limits. There is no
   direct SQL route around the research holdout reader. A dedicated paper stream
   must not silently copy protected holdout prices into a new archive.

4. **Preserve when inputs became usable.** For the paper bridge, derive an
   explicit local usable time as `max(available_time, ingestion_time)` while
   retaining both original timestamps in the receipt. Feed that derived time to
   the strategy's information-availability input, and bind this transformation
   and source code into the paper implementation digest. Do not rewrite stored
   receipt/publication times or change the canonical strategy implementation.
   Reading an archive at a later time never refreshes a signal's original time
   or expiry. A late revision affects only decisions made after it was usable.

5. **Make warm-up and no-action results explicit.** Reuse the selected canonical
   strategy and session aggregation, with no search or parameter tuning. It
   needs at least `lookback + 1` complete aggregated bars with required continuity;
   opening-range variants also need their declared first-session bars. A default
   20-bar, 15-minute strategy therefore needs 21 complete aggregated bars. Missing
   bars, zero ATR, stale data, incomplete aggregation and an unchanged target
   produce a recorded no-action outcome, not a fabricated flat signal or sell.

6. **Join to the existing durable engine.** Bind each eligible signal to its
   registration, source head, exact input/calendar digests and original expiry.
   Persist that evidence before submission. Use the existing deterministic
   client ID, reservations, reconciliation, risk checks, PAPER switch and
   fixed PAPER origin. A restored process resumes the same stream and journal;
   it does not regenerate a new account binding, reset attempts, or refresh an
   expired decision. Record unknown/rejected lifecycle states accurately.

7. **Collect outcome evidence without promoting it.** Persist authoritative
   broker observations, fills, cash/position reconciliation, execution costs
   where known and scheduled daily valuations alongside the unchanged study
   declaration. Keep source lineage, broker acknowledgement, operational
   readiness and strategy qualification as separate report fields. Unknown
   fee corrections, calendar authentication, missing regimes and independent
   origin/predeclaration evidence remain explicit blockers.

The runtime interface is bounded to one receipt acquisition and one paper
decision/reconciliation cycle. A daemon and scheduled paper execution remain
separate activation work after an actual broker lifecycle. The collector VM
remains a data-only deployment.

## Required adversarial checks before activation

| Boundary | Required behavior |
| --- | --- |
| Historical or test ancestor followed by fresh revision | Preserve the earlier lineage; never promote it |
| Sealed research holdout or another generation | Refuse input before strategy payload access |
| Corrected bar with revision 1 or later | Supply verified visible predecessor chronology; no renumbering |
| Response received before decision but ingested after it | Exclude it from that decision |
| Calendar received or ingested later | Exclude it from earlier decisions |
| Later archive read or process restart | Preserve original signal time/expiry and deterministic identity |
| Incomplete aggregate, gap, zero ATR or inadequate warm-up | Record no action; do not liquidate because features are missing |
| Changed source, parser, strategy, risk or registration | Refuse the old binding instead of silently creating a new study |
| Response success followed by failure to persist receipt | No order; retain an explicit acquisition/storage failure |
| Order response lost or ambiguous | Existing reservation stays unresolved; no automatic second order |
| Frozen researcher artifacts and old BTC pilot | No writes or state changes in all test/runtime paths |
| Injected transport, clock or fabricated report | TEST evidence, no authenticated/prospective qualification |

## Work that elapsed time or account access still controls

- Chrome/account setup must establish a genuinely separate PAPER account and
  dedicated key. Current preflight requires a flat ACTIVE USD account, multiplier
  one, empty history and exactly $100 simulated capital. Account-level fractional
  permission remains distinct from an asset's `fractionable` flag.
- An accepted order and a reconciled terminal lifecycle require actual PAPER API
  access and an eligible market session. A rejected or unknown response cannot
  be made successful by resetting the older halted pilot.
- The paper account is durably bound on initialization. Decide whether a
  connectivity-only smoke experiment uses its own account or shares the final
  predeclared strategy binding before initializing; the CLI has no reset path.
- The existing numerical floors require at least 60 daily valuations and 30
  completed flat-to-flat episodes, plus positive after-cost returns, Sharpe at
  least 1.2, profit factor at least 1.25 and drawdown at most 10%. Those floors
  alone are not independent prospective qualification. Full review and a
  genuinely unseen observation period cannot be replaced with more fixtures.

## Implemented bounded interface

`atlas/paper/intake.py` now owns registration, fixed-source collection, consistent
revision/calendar projection and durable candidate/no-action receipts. It uses
a new private stream outside repositories, research evidence, collector/operator
state and the old halted pilot. It checks source registration and archive
identity/freeze metadata before decoding observation payloads.

The stream uses only the first open 90-day V1 window beginning at current
registration. It cannot open a researcher archive as its input or read later
validation/holdout windows. There is no historical warm-up import command: it
waits for its own observations. The original stored receipt/ingestion clocks are
unchanged. Derived usable times and complete revision chronology are bound into
the candidate evidence. Calendar ancestry is taken from verified batch origins;
historical/test calendar-only imports cannot fill missing owned coverage.

These calls are an operator interface, not an active account or selected strategy:

```python
from atlas.paper.intake import PaperIntake

intake = PaperIntake.register(
    "/absolute/private/new-paper-input",
    generation_id="paper-study-v1",
    spec=selected_existing_spec,
    account_fingerprint=current_preflight_account_fingerprint,
    risk_policy=selected_paper_risk_policy,
)
registration = intake.registration()
acquisition = intake.acquire(key_file="/absolute/private/separate-paper-data-keys.json")
projection = intake.project(
    symbol="SPY", quantity=reviewed_quantity, limit_price=reviewed_limit_price,
    held_quantity=last_reconciled_held_quantity,
    expected_registration_sha256=registration["registration_sha256"],
)
```

The selected canonical strategy must be in the policy allowlist. Registration
binds the account, policy, exact strategy/intake source files, lockfile and Python
version. Its `implementation.strategy_sha256` deliberately differs from the old
offline bridge hash. The engine must bind that new hash before initialization;
do not change an initialized account's binding or reset the old BTC pilot.

For restart, open `PaperIntake` with the existing root instead of registering
again. Only `PaperIntake.testing` accepts injected clocks/transports; it can
create/open only a TEST registration. Test credentials and owned/test crossing
are refused. Actual acquisition selects a separate key; the older protected key
path and credentials stored inside intake state are refused.

`CANDIDATE` means a persisted canonical target change, not execution permission.
`NO_ACTION` includes a fixed reason and no candidate. Closed sessions, stale
signals, incomplete features, missing calendars, zero ATR and satisfied targets
cannot manufacture an order. Candidate replay reconstructs the original source
snapshot from its verified archive head and as-of time, checking all evidence
digests, counts and lineage. Changing a saved provenance flag or recomputing a
fabricated evidence digest cannot promote a TEST receipt.

Acquisition reports the actual archive lineage counts/classes. The owned path
remains `PROSPECTIVE_OBSERVATION_UNVERIFIED`; test input remains `TEST`.
Provider authenticity, prospective qualification, execution authorization and
live enablement remain false. An owned HTTPS path does not establish independent
provider attestation or protection against a privileged owner replacing history.

The interface has no daemon or automatic polling. Normally acquire once after a
completed five-minute bar during the regular session. Repeated calls preserve
more captures and consume the existing 10,000-batch archive bound. Projection is
bounded to 20,000 visible bar versions and 10,000 receipts, with a symmetric
262,144-byte receipt write/read limit. Capacity exhaustion fails closed; it is
not permission to delete evidence. The existing daily collector remains separate.

The dedicated account, real preflight, accepted/terminal PAPER lifecycle and
sufficient prospective observation period remain external requirements. The
reviewed collector recovery fix was cherry-picked as `22d67f2` before final
verification. Source changes correctly invalidate an existing intake binding.
Python version binding does not attest the host clock, timezone database or
unknown provider publication time.

## Bounded engine cycle

`atlas/paper/intake_cycle.py` joins the registered source to an existing engine:

```python
from atlas.paper.intake_cycle import run_cycle

outcome = run_cycle(
    intake, existing_engine,
    symbol="SPY", quantity=reviewed_quantity, limit_price=reviewed_limit_price,
    expected_registration_sha256=registration["registration_sha256"],
    execute=False,
)
```

The default performs broker reconciliation reads and persists the source pin
and projection locally, but does not submit. Account, strategy, policy and
source identity must match; TEST intake cannot cross into a PAPER engine. An
enabled cycle obtains
holdings from successful engine reconciliation, persists the canonical candidate,
and routes it through the existing engine's order/risk/freshness gates. It never
initializes or resets an account, supplies fake broker holdings, or bypasses the
engine's fixed PAPER origin. The cycle does not acquire data, create a broker
client, initialize a journal, or schedule a worker. This is a Python adapter,
not a CLI command or autonomous service. The actual account, authorization
flags and terminal order lifecycle still require external verification; no
paper worker is active.

## Verification checkpoint — September 15, 2026

- Locked dependencies: `uv sync --locked --group dev` passed.
- Combined intake, cycle, existing strategy and CLI checks: **191 passed** in
  23.82 seconds. This includes 65 intake and 26 cycle tests.
- Required full suite: `uv run --locked python -m pytest -q -ra` — **3,777 passed,
  3 intentionally skipped, 0 failures** in 174.59 seconds. The skips require the
  optional IQ Option dependency, a disposable root Linux container and an
  explicitly selected private dataset, respectively.
- Independent review findings for saved-receipt provenance, pre-payload archive
  identity/freeze checks, symmetric receipt bounds, calendar ancestry and exact
  replay snapshot reconstruction are closed with adversarial tests.

These are offline engineering results. No actual account was authenticated,
no broker order was submitted, no study was registered against a real account,
and the earlier halted BTC pilot was not reset. An authenticated accepted and
terminal PAPER lifecycle is still unproven. Prospective strategy qualification
still requires its declared observation period and unchanged evaluation gates;
passing tests cannot supply those observations. Live trading remains disabled.
