# Separate paper-account preflight continuation

Status: **PARTIAL**. This continuation prepares a read-only account/data check;
no dedicated account or accepted order lifecycle has been verified yet.

The working branch is `codex/atlas-paper-execution`, continuing from `11c3f7f`.
The collector VM, deployed release, credentials, validation evidence and hourly
validation automation have not been changed or queried during this continuation.
Mac-off evidence is separate and has not been established here.

## What was checked

- The existing local credential/config inventory contains the older halted pilot
  under `~/.config/atlas/alpaca-paper`; no separate new-engine config directory was
  present. Only selected non-secret metadata from its old reports/config was read.
- Those old reports describe an ACTIVE account with $100 simulated cash on
  September 10. They are historical observations, not current account readiness.
- Connected Gmail still contains only the September 11 automated acknowledgement
  and explicitly AI-generated reply for support ticket 353754. No new human
  diagnosis or account-specific resolution was found in this check. No message
  was sent and no draft or label changed.
- Chrome connected successfully, but it blocked the Alpaca page inspection
  because another extension UI was open. The user has been asked to dismiss it.
  No account reset, creation, credential generation or order was performed.

## Engineering change

The new `atlas_paper.py preflight` command uses the owned fixed-origin PAPER API
with writes disabled. It checks account identity/state, principal, multiplier,
complete empty history, asset eligibility, the session clock, and quotes when the
session is open. It creates no journal, account registration or reservation.
The report separates account readiness, asset/data eligibility and waiting for
market open; it never grants execution permission or strategy qualification.

The old and new preflights use different account-fingerprint namespaces
(`alpaca_paper` and `alpaca-paper`). Copying an old digest into the new config
would fail identity checks. The new report computes the correct namespace from
its current authenticated account response without exposing the raw UUID.

Both preflight and normal new-engine config loading refuse keys selected from
the older pilot's protected directory. This prevents accidental reuse by path;
it cannot prove a copied key belongs to a different account. Actual independent
account identity must still be verified.

See [paper-execution-operations.md](paper-execution-operations.md) for the new
command and output/exit-status contract. `uv sync --locked --group dev` passed.
The complete `uv run --locked python -m pytest -q -ra` run finished with
**3,643 passed, 3 skipped, 0 failures** in 70.21 seconds. The same optional
IQ Option, disposable root-Linux-container, and explicit private-dataset tests
were skipped. Focused preflight/CLI verification had 105 passing tests; it is
included in the complete total, not additional to it. Independent review found
no outstanding consequential issues.

The prior engineering milestone remains recorded as 3,590 passed and 3 skipped.
This continuation adds 53 tests. No actual broker order lifecycle or strategy
performance is inferred from either test run.

## Resume at the external blocker

After Chrome's extension pop-up is dismissed, inspect the available paper
accounts. Use a genuinely separate account and key, with $100 simulated capital
and the new engine's required settings, before running the read-only preflight.
Keep the old pilot, collector credentials, and cloud validation intact. An order
lifecycle comes only after account/data checks and during an eligible session.
