# Dedicated $100 paper pilot

`paper-100-config.example.json` prepares one synthetic USD account with $100 of
starting cash. It is an inert template: no account, state, server, scheduled job,
connection or order is created by this file. No real funds are allocated.

| Setting | Pilot contract |
|---|---|
| Mode and adapter | `paper`, local deterministic simulator |
| Initial funding | $100 synthetic; immutable `initial_capital_limit: "100"` |
| Order size | At most $10 including estimated fees |
| Position size | At most $25 and 25% of equity after fees |
| Daily realized loss gate | Block new orders at realized P&L of -$5 or below |
| Daily order attempts | At most 4 durable submission attempts per UTC day |
| Fee assumption | 60 basis points each side; an illustrative stress assumption, not a broker quote |
| Instrument | BTC-USD; synthetic fixtures only in the current CLI factory |
| Stale input | Quotes and held-position valuations older than 30 seconds block orders |
| Operator stop | Presence of `state_dir/KILL_SWITCH` blocks new orders |
| MCP execution tools | Disabled in the template |

The starting-funding limit is saved in simulator metadata together with the
original cash commitment. Cash, holdings, fees, orders and fills persist across
restarts. Reopening a bound account must use the original seed and limit; lowering
`cash` does not refill losses or transform another account into this pilot. Removing
or changing the commitment refuses. Existing uncapped simulator files cannot be
adopted, even when their current balance happens to be $100. Use dedicated account
identity and state selected for the pilot, and preserve any old state for recovery.

Bound accounts use simulator schema version 3; older code refuses them. Existing
uncapped version-2 accounts keep their prior behavior and are not migrated.
The simulator has no deposit or balance-reset API. Profit can grow equity above
$100, and losses reduce remaining cash; the limit bounds original simulated funds.

The realized-loss gate is not a $5 guaranteed stop-loss: open positions can lose
value, fee and spread costs apply, and the current gate also blocks new sell orders
once breached. The kill switch blocks submissions; it does not liquidate holdings.
This template is for testing accounting and order controls, not unattended trading.

For one bounded offline engineering check, run from the checkout:

```text
uv run --locked python scripts/paper_100_smoke.py --output /new/external/evidence-directory
```

The output directory must not exist. This command generates explicitly synthetic
fixtures and calls the actual paper service methods for one simulated order. It
checks duplicate requests, restart conservation, reseeding refusal, the order
limit, stale inputs and the kill switch, then closes all handles and transient
broker helper threads. Network connections are forbidden during the check. It
leaves fixtures, both databases, hashed JSON evidence and an active kill switch.
It does not start an MCP listener, load a learned model or establish profitability.

## Prepare and verify on the selected host

1. Select the host and external configuration/state directories. The example's
   `/etc` and `/var/lib` paths are placeholders; nothing has been installed there.
2. Copy the configuration outside the checkout. Provide explicit dataset and
   quote files using the contracts in `deploy/README.md`. The current factory
   labels quote files synthetic even if their prices came from public markets;
   manually changing timestamps does not establish prospective live evidence.
3. Run `python -m atlas config-check --config /absolute/path/config.json`. It
   validates configuration without creating state or starting a service. It does
   not yet verify the dataset, quote file, account history or connection.
4. Review the full paper fixtures and expected outcomes before intentionally
   starting a service on that host. Keep execution tools disabled for observation.
   Enabling simulated execution is a separate configuration choice on that host.
5. Verify restart conservation, duplicate-order handling, stale-input refusal and
   the kill switch with synthetic fixtures. Preserve the execution and simulator
   databases together; do not delete either to clear an error or reset losses.

The current factory refuses all real broker selections and live modes. This
paper account cannot reserve money in a real brokerage account or restrict that
account's other assets. A future real $100 pilot needs an independently isolated
account or equivalent broker-enforced allocation, owner funding/activation, and
validated account, order, reconciliation and strategy evidence.
