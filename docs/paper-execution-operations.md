# Local PAPER execution operations

This is a prepared operator interface for a dedicated Alpaca **PAPER** equity
experiment. It is separate from the collector VM, its 24-hour validation, the
frozen research studies, and the exhausted BTC connectivity pilot. This work does
not activate a broker session, schedule a worker, fund an account, or place an
order. There is no LIVE mode, configurable API host, or arbitrary HTTP endpoint.

The earlier BTC pilot remains halted after two rejected requests. Its journal,
attempt budget and support history must remain intact. These commands do not
reset it or establish that its unexplained 403 has been resolved. Authentic
broker readiness and strategy qualification remain separate from passing tests.
See `alpaca-paper-pilot.md` and `paper-qualification.md`.

## Configuration

Use an absolute path to an owner-only `0600` JSON file with no symlink in its
path. Duplicate keys, unknown fields, inline credentials, LIVE/SIMULATION modes,
nonfinite numbers, hardlinks, oversized inputs, and ambiguous scalar types are
rejected. The state directory must be an explicit external private directory;
collector/operator state, repositories, research evidence, `.codex`, and the
old Alpaca pilot directory are excluded. Its parent must already exist.

This is a template, not an activated configuration; replace the identity and
path placeholders with independently reviewed values:

```json
{
  "schema_version": 1,
  "mode": "PAPER",
  "state_dir": "/Users/you/.local/state/atlas-paper-execution/session",
  "credentials_file": "/Users/you/.config/atlas-paper-execution/paper-keys.json",
  "account_fingerprint": "<64 lowercase hex characters>",
  "strategy_sha256": "<64 lowercase hex characters>",
  "allocated_principal": "100",
  "paper_execution_enabled": false,
  "risk": {
    "allowed_strategy_ids": ["reviewed-strategy"],
    "allowed_symbols": ["SPY", "QQQ", "GLD", "USO"],
    "max_order_notional": "10",
    "max_position_notional": "25",
    "max_portfolio_notional": "75",
    "max_daily_loss": "2",
    "max_drawdown_fraction": "0.05"
  }
}
```

All money, quantity, fraction and basis-point values in `risk` are decimal
strings. Count/time limits use integers. Omitted risk fields use the validated
`PaperRiskPolicy` defaults; an empty strategy allowlist blocks all strategies.
The full normalized policy and its digest appear in `config-check`.
`paper_execution_enabled` defaults to false if omitted and must be a JSON boolean.

The credential file path is validated without opening its contents. Actual
authenticated commands use the existing private Alpaca key-file loader, which
validates the credential file. Offline commands still work when that key file is
missing. Keep config and credentials outside the execution state directory.
Neither credential values nor their path are printed by `config-check`.

The first initialization requires empty broker order/activity/position history,
an active USD account with multiplier one, and starting cash/equity equal to the
bound principal. A larger account balance is not silently treated as a $100
subaccount. Subsequent gains can increase marked equity above $100. The principal,
account, strategy and normalized risk policy are persistently bound; changing
those fields cannot reopen the journal as a new experiment.

## Commands

Run from this checkout with the project's installed Python environment. The
script adds its own project directory to the import path, so it also works from
another working directory. Replace `/absolute/private/config.json` below.

```sh
python scripts/atlas_paper.py config-check --config /absolute/private/config.json
python scripts/atlas_paper.py status --config /absolute/private/config.json
python scripts/atlas_paper.py halt --config /absolute/private/config.json
```

These three commands use no network or credential contents. `config-check`
creates no state and does not prove account entitlement or broker readiness.
`status` verifies and reads existing state; it does not refresh market/broker
truth. `halt` durably enables the kill switch in existing state. Neither creates
a missing session. Status timestamps and freshness must be interpreted as the
last recorded observations.

The following commands can contact the fixed Alpaca PAPER/data APIs and are
prepared for a separately authorized operator session:

```sh
python scripts/atlas_paper.py initialize --config /absolute/private/config.json
python scripts/atlas_paper.py reconcile --config /absolute/private/config.json
python scripts/atlas_paper.py submit --config /absolute/private/config.json --decision /absolute/private/decision.json
python scripts/atlas_paper.py cancel --config /absolute/private/config.json --client-order-id OWNED_CLIENT_ID
python scripts/atlas_paper.py replace --config /absolute/private/config.json --client-order-id OWNED_CLIENT_ID --decision /absolute/private/replacement.json
```

`initialize` may create one new journal, or reopen the same bound journal. An
already initialized session is returned without resetting it. An unsuccessful
initialization preserves any state created before the failure. Every other
command opens existing state only. `reconcile` refreshes broker orders, fills,
positions, cash and risk anchors without submitting or cancelling an order.

Submit/cancel/replace are refused before broker access unless the config's
explicit PAPER execution switch is true. Enabling that switch expresses operator
intent; it does not resolve the earlier 403, authenticate research evidence, or
qualify a strategy. Account identity, durable reservations, risk checks and
one-use mutation permits remain mandatory. All mutations target the fixed
`https://paper-api.alpaca.markets` origin. The data origin is fixed to
`https://data.alpaca.markets` with IEX equity quotes.

Decision files follow the same private-file rules as configuration, with a
smaller 16 KiB limit. They contain exactly `signal_id`, `strategy_id`,
`strategy_sha256`, `data_sha256`, `symbol`, `side`, `quantity`, `limit_price`,
`signal_time`, and `expires_at`. Times are numeric UTC epoch seconds; quantity and
price are decimal strings. The strategy ID/hash must match the configured
allowlist and journal. Orders are long-only, regular-session DAY limits, with
fractional quantities allowed only for eligible assets. The same signal cannot
be resubmitted with changed terms or obtain a second broker order.

Replacement quantity is the desired **total** across the original and new
orders, at most the original quantity. Use a distinct replacement signal ID.
The engine commits the replacement plan, cancels once, waits for a reconciled
terminal state, subtracts all original fills, and risk-checks the remainder.
An acknowledgement alone never releases the reservation or permits a second leg.
There is no native PATCH replacement or automatic liquidation.

Every command emits JSON. Exit `0` means the requested bounded operation
completed; an accepted order or cancellation acknowledgement still does not
prove a fill or terminal cancellation. Exit `2` means invalid/refused input or an
operation error. Exit `3` means an incomplete/refused outcome, such as unknown or
rejected submission, failed risk/reconciliation, or replacement awaiting terminal
reconciliation. Error messages contain fixed codes, not raw provider text,
credential material, private JSON, or input paths.

## Recovery and limits

Use one dedicated account, one execution host and one OS user. A private local
flock protects each journal, and a separate per-user account lock prevents two
state directories from driving the same account simultaneously. PAPER account
registration is durable under `~/.local/state/atlas-paper-account-locks` and also
blocks a fresh directory from resetting the registered experiment. There is no
production environment/config override for the account-lock location. These are
local coordination controls, not distributed locks or protection against another
OS user/host or an administrator modifying files.

After a crash, retain the same config, journal and client IDs; inspect `status`,
then run a bounded `reconcile` when broker access is authorized. A timeout, lost
response, malformed success, or 404 lookup does not prove that a POST failed.
Unknown intent stays reserved. Repeating a signal only reconciles its original
identity. Do not delete or recreate state, clear reservations, copy an earlier
database over it, change account registration, or reset attempt counters to
obtain another try. Corrupt, missing, foreign or inconsistent state fails closed.

Provider cooldown deadlines persist without shortening valid Retry-After delays.
Local throttles and provider rate deferrals do not consume the broker-failure
circuit. Mutation failures have a cumulative session budget: healthy read calls
do not erase rejected/uncertain mutation history, and each known rejected order
is counted once across reconciliation and restart. The default failure threshold
is three. Entry halts remain sticky; this CLI has no automatic resume/reset.

The kill switch blocks new orders and replacements. Owned cancellation remains
available with execution enabled, subject to identity resolution, broker access,
and cooldowns. To stop entries independently of a CLI process, an operator can
place a `STOP` file in the registered state directory. Do not remove a halt merely
because a later snapshot looks healthy. Unknown/unmanaged orders or positions,
unexplained cash movement, unexpected account activities, missing history and
changed fill records require investigation. Actual fee completeness remains
explicitly unknown where provider records do not establish it.

Reads are deliberately bounded. Saturated order/activity histories block instead
of being mistaken for complete history; a long-running session will need reviewed
history-retention/pagination work before it reaches those bounds. Risk thresholds
cannot guarantee a maximum realized loss during gaps or outages. This CLI has no
daemon, scheduler, deployment command, credential rotation, funding command,
strategy tuner, model training, frozen evaluator, or holdout-unlock operation.

## Offline demonstration

```sh
python scripts/atlas_paper.py demo
```

This uses the actual adapter, journal and engine with `SyntheticAlpaca`, a private
temporary journal, invented prices, explicit fills, a cancellation/fill race,
and a restored synthetic broker state. It removes the temporary trading state
after completion and reports `TEST`, `SIMULATION`, and zero actual orders.
The shared account-lock mechanism may retain its empty SIMULATION lock file;
the demonstration creates no persistent trading session or research evidence.
Passing this demonstration proves those exercised software behaviors only.
