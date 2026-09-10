# Server preparation only

These examples are inert files. No service, daemon, broker session, startup
job, or network listener has been installed or started. The intended eventual
host is a separate Linux server; this Mac remains a development checkout.

## CLI surface

- `python -m atlas` prints help and creates no state.
- `python -m atlas demo` runs generated bars through an in-memory paper broker
  and journal. Its cash and fills are explicitly synthetic.
- `python -m atlas config-check --config /etc/atlas/config.json` validates
  external configuration without creating state or contacting a broker.
- `python -m atlas mcp --config /etc/atlas/config.json --transport stdio`
  intentionally starts the local stdio interface when an operator invokes it.
- `python -m atlas mcp --config /etc/atlas/config.json --transport http`
  intentionally starts the authenticated loopback HTTP interface. The example
  service uses this command; neither command was run during development.

Only the paper simulator and an unconfigured research broker are wired into
the CLI factory. Robinhood adapters are tested with injected transports; the
live broker deployment factory is unavailable and refuses before state creation.
There is no hidden fallback from a real account to paper.

## Configuration and state

Copy and review the example as an external file on the chosen server. Paths
must be absolute and resolve outside the source checkout. The code does not
autoload `.env`, reuse browser sessions, or import brokerage credentials.
Configuration and dataset files must be regular files owned by the invoking
user or root, with no group/other write permission.

The default is `research` with no broker, cash, or holdings. Explicit `paper`
configuration requires `broker: "paper"`, a `paper` object containing
`account_id`, `cash`, and `fee_bps`, plus an external `quotes_file`. Generated
money is not seeded into a default research account.

The optional `initial_capital_limit` binds the simulator's original funding
commitment across restarts and refuses adoption of an older uncapped account.
See the [$100 pilot guide](paper-100-pilot.md) and
[configuration](paper-100-config.example.json) for a dedicated synthetic test.
This setting does not allocate or constrain money in a real brokerage account.

An optional external `datasets_file` maps symbols to objects with
`asset_class`, `bars`, `source`, and `evidence_kind`. Each bar uses a close
`timestamp` and numeric `close`; an observed `open` is additionally required
for baseline backtests. Future, unordered, or duplicate times are rejected.
Caller tools cannot replace these bars or set model probabilities.
A paper `quotes_file` maps symbols to `Quote` fields: `symbol`, `asset_class`,
`bid`, `ask`, `timestamp`, `market_open`, `available_quantity`, and `source`,
with optional increments and minimum quantity. Quotes are always relabeled
as operator-supplied synthetic fixtures.

The MCP benchmark supports the fixed version `1.0.0`, 20-bar momentum and
mean-reversion contracts plus buy-and-hold and seeded-random controls. It
refuses registry versions or parameters that differ from those implemented
rules. Reports expose the actual simulator parameters and original dataset
kind/source. Synthetic/test data stay labeled as such; an absent or unknown
dataset provenance is explicitly unverified. This is a baseline simulation,
not an implementation of arbitrary registered strategy versions.

Journal paths are:

| Mode | Journal |
|---|---|
| research | `state_dir/research/execution.db` |
| paper | `state_dir/paper/execution.db` |
| live-assisted / live-auto | `state_dir/live/execution.db` |

The two live modes deliberately share one ledger so switching modes cannot
escape unresolved submissions. Paper broker holdings and cash persist in
`state_dir/paper/simulator.db`. A `state_dir/KILL_SWITCH` file blocks new orders.

## Secrets and permissions

The environment accepts file paths rather than inline credentials:

| Variable | Purpose |
|---|---|
| `ATLAS_HTTP_BEARER_FILE` | HTTP bearer credential; at least 32 bytes |
| `ATLAS_EXPECTED_ACCOUNT_FILE` | Explicit expected account for a future real adapter |
| `ATLAS_APPROVAL_KEY_FILE` | Reserved verification-key injection path for future live factory assembly |

Secret files must be regular, owned by the invoking user, mode `0600`, and
outside the repository. Symlink secret files are rejected. The systemd user
would need ownership of its own bearer file; an operator approval key belongs
to the operator. No real keys or account numbers are included in examples.

Future live configuration uses the mode plus independent exact environment
flags `ATLAS_LIVE_TRADING_ENABLED=true` and, for automatic mode,
`ATLAS_LIVE_AUTO_ENABLED=true`. Those flags do not make the currently absent
deployment adapter available and cannot override deterministic risk checks.

## Approval is an operator action

The operator CLI reads an existing external live ledger without writing it:

```text
python -m atlas approve --journal /var/lib/atlas/live/execution.db \
  --proposal-id <immutable-proposal-id> \
  --key-file /operator-secrets/atlas-approval-key \
  --output /operator-receipts/receipt.json
```

It displays the complete proposal and hash, then requires the exact full hash.
Noninteractive use must supply that hash with `--proposal-hash`; there is no
generic `--yes`. The output is created exclusively with mode `0600`, fsynced,
and never submitted to a broker by this command. Expired or risk-rejected
orders cannot obtain a receipt. The MCP surface has no issuer, key input, or
tool that asserts human identity.

Current receipts use HMAC, so security depends on separating the operator's
key access from the AI caller and protecting any eventual verifier process
that holds the shared key. This is not hardware-backed human presence. Stronger
production separation should use an operator-held asymmetric signing key.

## HTTP and host hardening

All HTTP paths require a bearer token checked with constant-time comparison.
The listener accepts only loopback binding and the ASGI application rejects
non-loopback peers. `/healthz` checks journal integrity; `/readyz` reports pure
state readiness. Neither probes a broker, loads a model pickle, or starts a
subprocess. Healthy storage does not establish live readiness. Bind a private,
authenticated gateway deliberately if remote clients are required; no public
gateway or OAuth issuer is implemented by these examples.

SIGINT/SIGTERM set the service stop event before the HTTP server or stdio loop
unwinds. HTTP graceful shutdown is bounded to 20 seconds; unknown broker calls
remain uncertain and their durable intent must be reconciled after restart.
Tests exercise captured handlers and fake server exits, not actual OS signal
delivery to a deployed process.

`atlas.service.example` uses a dedicated user, restrictive filesystem access,
owner-only umask, bounded shutdown, and localhost-only networking. The selected
host still needs an independently reviewed environment, dependency installation,
secret provisioning, gateway, resource limits, and recovery testing. systemd
network restrictions may depend on host features; the file is not evidence
that those controls are enforced on any machine.

Application access logs are disabled. Standard output/error go to journald;
the eventual host must choose journald retention and disk limits. Do not log
authorization headers, request bodies containing receipts, or environment dumps.

Use the ledger's SQLite backup operation for consistent journal snapshots.
Back up the paper simulator database consistently as well; simple live copies
can omit WAL data. Model artifacts, source datasets, restore testing, offsite
retention, and secret recovery need separate coverage. No backup schedule or
rotation job is configured here.
