# ATLAS

A Python toolkit for researching short-horizon FX direction and checking whether model predictions translate into executable binary-option contracts.

ATLAS combines candle storage, causal features, calibrated models, expected-value signal rules, and a validation harness that accounts for overlapping labels and correlated trades. An optional IQ Option adapter exposes market data and guarded account operations through MCP. You can explore the core pipeline locally without a broker account.

## Start with the offline demo

Prerequisites: Git, **uv**, and Python **3.12 or later**. uv can provision a compatible Python interpreter when needed. Installing dependencies requires an internet connection; running the demo does not.

```bash
git clone https://github.com/Noctilucenty/ATLAS.git
cd ATLAS
uv sync --locked --group dev
uv run python demo.py
uv run python -m pytest -q
```

The demo uses deterministic **synthetic candles** to exercise the storage, feature, and walk-forward prediction pipeline. It writes a readable `report.md`, a machine-readable `report.json`, and the generated `candles.parquet` to `output/offline-demo/`.

No credentials, broker connection, historical-data download, or frozen research model is needed. The demo is an engineering example: its predictions and metrics do not measure a financial edge or reproduce the registered research. Outputs are kept separate from the operational databases and research artifacts.

The development group includes LightGBM and Optuna for the synthetic model tests. For historical-data research modules, the optional research extra also supplies the HistData downloader:

```bash
uv sync --locked --group dev --extra research
```

Installing these dependencies does not run an experiment. Before conducting research, read the protocol and experiment record linked below.

`uv.lock` pins the new development environment. The older `requirements.lock` is retained as historical provenance; it contains a machine-specific broker-library path and is not a portable installation file. Frozen model artifacts may need their original dependency versions.

The test suite skips private datasets by default. To explicitly smoke-test a local copy, set `ATLAS_TEST_DATASET=/absolute/path/to/copy.duckdb` and run `uv run python -m pytest tests/test_features.py::test_real_dataset_if_available`. The selected database is opened read-only.

Deployment is intended for a server. This checkout is for development; the offline quickstart installs no startup jobs or application services. See [server readiness](docs/server-readiness.md) before configuring a host.

The Python runtime supports an external state directory via `ATLAS_STATE_DIR`, with separate overrides for models, the environment file, and backups. Inspect the resolved paths without starting an application or creating directories:

```bash
uv run --locked python runtime_paths.py
```

The supervisor requires an explicit `--mode paper`, `--mode practice`, or `--mode real` (`--paper` remains an alias). Real execution also requires `IQ_ALLOW_REAL=1` and an explicit finite positive `--amount` in the account's currency. These are future server runtime choices, not part of the offline quickstart. Watchdog recovery and dashboard startup are also opt-in. Server process management and deployment remain unconfigured.

## What is in the repository

| Component | Entry points | Purpose |
|---|---|---|
| Offline example | [`demo.py`](demo.py) | Run the pipeline on generated data and inspect its outputs |
| Data layer | [`storage.py`](storage.py), [`validation.py`](validation.py), [`collector.py`](collector.py) | Store and deduplicate candles in DuckDB, validate OHLC data, report gaps, and collect broker data when configured |
| Features and training | [`features.py`](features.py), [`train.py`](train.py) | Construct features and labels; train, calibrate, freeze, and predict across chronological folds |
| Pooled research | [`research_pooled.py`](research_pooled.py), [`research_meta.py`](research_meta.py) | Build cross-asset features and evaluate a second model that filters candidate signals |
| Statistical checks | [`validation_stats.py`](validation_stats.py), [`acceptance_report.py`](acceptance_report.py) | Account for dependent trades, multiple experiments, calibration, and sample size |
| Experiment history | [`registry.py`](registry.py), [`research_registry.jsonl`](research_registry.jsonl) | Record experiment families and trial counts |
| Frozen forward test | [`FORWARD_TEST.md`](FORWARD_TEST.md), [`forward_eval.py`](forward_eval.py) | Define and evaluate the pre-registered hypotheses under their original rules |
| Broker integration | [`server.py`](server.py), [`instruments.py`](instruments.py), [`execution_guard.py`](execution_guard.py) | Expose MCP tools, resolve instrument identifiers, and guard order execution |
| Operational tools | [`supervisor.py`](supervisor.py), [`mission_control.py`](mission_control.py), [`journal.py`](journal.py) | Coordinate collection and explicitly selected sessions, inspect health, and reconcile broker outcomes |
| Tests | [`tests/`](tests/) | Check pipeline behavior, leakage boundaries, evaluation criteria, and operational guards |

Research modules also cover historical replication, era holdouts, OTC/spot comparisons, spread sensitivity, and calibration. Their results and rejected approaches are indexed in [`FINDINGS.md`](FINDINGS.md).

## How a signal is formed

The research pipeline estimates the probability of an upward price move over a specified horizon. Chronological calibration uses validation blocks inside the training window; the subsequent test window stays separate.

For a binary contract with a profit payout of `r` per unit staked, the decision rule computes:

```text
call_EV = p_up × r − (1 − p_up)
put_EV  = (1 − p_up) × r − p_up
```

A signal must clear the configured expected-value margin. A lower payout therefore requires a higher estimated chance of winning. Some registered hypotheses also apply a meta-model that scores the candidate signal's context. These are model-based estimates; broker acceptance, the actual contract expiry, and observed outcomes are separate measurements.

The validation code uses chronological purges to keep overlapping labels out of training/test boundaries, removes overlapping trades per asset, and groups simultaneous cross-asset activity for statistical analysis. The registered test fixes its hypotheses and multiplicity correction in advance.

## What the available evidence supports

The tracked research notes contain historical results and audit corrections through July 2026. The checkout includes three historical model pickles (`h2-20260722.pkl`, `h4-20260722.pkl`, and `meta-h3.pkl`) and a small legacy `journal.db`. It does not include the full `market.duckdb` history or live execution logs needed to reproduce the reported results, establish a completed forward-test verdict, or verify a live trading edge. The model snapshots require their provenance, cutoff constraints, and dependency compatibility to be checked before use.

The model pickles and journal remain tracked despite matching `.gitignore`; use `git ls-files` when inspecting the repository inventory. The offline demo and default tests do not load these model snapshots or the journal.

Two later findings are especially relevant when reading earlier win-rate claims:

| Recorded finding | Limit on interpretation |
|---|---|
| The July 29 tradeability audit reported **14 accepted orders from 259 signals**. All 214 signals in the 20–21 UTC rollover hours were unexecutable. | A blended candle win rate includes signals the broker would not accept. The tradeable-hours subset had only 45 scored signals and was too small for a firm conclusion. |
| The July 28 expiry audit reported broker agreement of **13/13** after applying the broker's snapped expiry, versus **10/13** under the registered 15-bar label. | This identifies a contract-timing mismatch in a small sample. It does not establish general execution fidelity or profitability. |

These are **repository-reported observations**, not results reproduced by the offline demo. Read the accompanying sample sizes, corrections, and caveats in [`FORWARD_TEST.md`](FORWARD_TEST.md). Older references to expected verdict dates, running hosts, or pending measurements describe the state when those notes were written; they are not a current operational status report.

## Research protocol and reading order

1. [`FORWARD_TEST.md`](FORWARD_TEST.md) governs the frozen hypotheses, configuration, evaluation rules, and the pre-committed power rule. The registered evaluation is permitted **once**; it is not an onboarding command.
2. [`FINDINGS.md`](FINDINGS.md) records historical experiments, rejected ideas, implementation defects, and operational lessons. Its early conclusions should be read alongside the later protocol audits.
3. [`RESEARCH_QUEUE.md`](RESEARCH_QUEUE.md) records candidate follow-up work and the boundaries around the existing test.
4. [`research_registry.jsonl`](research_registry.jsonl) records experiment families used in trial-count accounting.

Keep new engineering examples separate from frozen models and forward evidence. A new research experiment belongs in the registry; new hypotheses require their own registration and fresh evaluation data. Do not retune a frozen hypothesis against its forward window.

## Optional broker integration

The broker adapter uses the separately installed, unofficial [`iqoptionapi`](https://github.com/iqoptionapi/iqoptionapi) library, whose maintainers caution against real-account use. It is not needed for the offline demo. Broker workflows additionally need account credentials, appropriate collected data, and compatible model artifacts. The included historical pickles do not establish that a fresh clone is ready for operation.

For explicit PRACTICE-account use, copy [`.env.example`](.env.example) to the gitignored `.env` file and supply the broker credentials there. Keep these settings:

```dotenv
IQ_DEFAULT_BALANCE=PRACTICE
IQ_ALLOW_REAL=0
```

The runner and supervisor support these explicit selections:

| Mode | Order behavior | Required stake selection |
|---|---|---|
| `paper` | Log predictions; place no orders | `--amount` is rejected |
| `practice` | Place orders on the PRACTICE balance | Defaults to 1 account-currency unit; `--amount` may override it |
| `real` | Place orders on the REAL balance only with `IQ_ALLOW_REAL=1` | An explicit finite positive `--amount` is required |

The runner selects its balance from the CLI mode and checks it again before every order. An unexpected or unreadable balance blocks that order. Real signals, heartbeats, cross-sections, and supervisor logs live beneath `logs/real/`; the frozen paper/practice evidence stays at its historical paths. One account-currency unit does not necessarily mean one US dollar.

Each real runner submission first creates `logs/real/order_submission_pending.json`. An unknown acknowledgement or crash during submission leaves this marker in place and blocks automatic restarts. Reconcile the exact submission against broker history before manually clearing a remaining marker; neither the supervisor nor a successful restart clears it for you. See [server readiness](docs/server-readiness.md) for the recovery boundary and verification limits.

`server.py` reads `.env` when its process starts. Its MCP tools cover connection status, market data, payouts, and guarded order operations. Only one trading runner should control a given account at a time. The local development work has not enabled a funded account, connected to the broker, or placed an order.

[`WINDOWS_SETUP.md`](WINDOWS_SETUP.md) and the operational scripts document the earlier host setup. Paths, scheduled jobs, and host state in those records need to be checked for the machine being configured. Cloning the repository or running the offline quickstart does not start a collector, supervisor, or trading session.
