# Collector recovery: advancing archive time and retained fatal diagnostics

Status: repair deployed as release `111531c11ff6e931` from `e5e0108`.
Actual reboot recovery and offsite verification passed. A new immutable 24-hour
validation started September 16, 2026 at **08:06:05.971466 UTC / 1:06:05.971466 AM
Pacific**. Its earliest valid full-day result is **September 17 at the same time**.
The two earlier observations remain failed and preserved. This run is PENDING.

## Reproduced defect

A provider response can contain both fresh observations and stale observations
for the same UTC day. Acquisition groups them by origin, appending the fresh
group before the historical group. Each append records its actual ingestion
time. Previously, every group's partition check reused the timestamp sampled
before the append loop. If the first append crossed a second boundary, the next
check treated its newly committed batch as future data and raised
`archive_batch_chain_invalid`.

The fix samples the current clock for each partition check. It preserves the
original receipt-based grouping and acquisition classification. It does not
clamp timestamps, relax archive integrity, reclassify historical data as
prospective, or change the frozen research contract.

The regression reproduces a first append at `1789475487` followed by a check
using the earlier `1789475486`. Before the fix, the advancing-clock test failed;
the backward-clock and partial-commit recovery tests passed. After the fix,
all three pass. A real backward-clock jump remains a refusal, and recovery
preserves the first committed observation without duplicate or revised records.

## Connection to the recorded incident

Read-only metadata from the deployed archive confirms that all 68 failed Alpaca
attempts committed one `builtin_get` batch, exactly one second after their last
HTTP capture. No failed-request checkpoint followed. This matches the reproduced
partial-append timing defect closely. The original process logged only a generic
error, so its precise exception was not retained; production attribution remains
an evidence-backed inference. The repair is now running on the VM; its complete
24-hour operational qualification remains pending.

The two earlier service restarts coincided with unattended Python/libc upgrades.
That maintenance evidence is separate from the later application-error loop.
Another offline probe reproduced reader/writer contention with an uncoordinated
archive verifier; no such concurrent reader was established in the incident.
The approved observation wrapper coordinates its reads with the snapshot lock.

The exact failed assessment, full event capture, journals and read-only archive
metadata are preserved outside the repository in
`/Users/leon/Desktop/dev/ATLAS-research-evidence/hosting-selection-20260914/clock-boundary-20260915/`.

## Fatal diagnostics

The collector CLI retains its nonzero exit, `REFUSED` status and
`collector_operation_failed` error. It now includes a fixed execution stage,
allowlisted cause, exact exception type, bounded owned-source locations and
sanitized exception context. Unexpected ordinary exceptions also receive this
format; termination exceptions remain unhandled as before.

Exception messages, notes, local variables, response bodies, URLs, arbitrary
class names and filesystem paths are excluded. Diagnostic writes have bounded
retries and a stdout fallback if stderr is unavailable. If both sinks fail,
the process still fails; retention cannot be guaranteed without a working sink.
The CLI creates no diagnostic state file or broker session. Source locations are
debugging hints, not proof of code authenticity.

## Verification and remaining work

Independent review found no consequential issues. Focused checks cover advancing
and backward time, partial commits, lineage/deduplication, secret-bearing
exceptions, unavailable logging, and a staged standard-library-only CLI failure.
`uv sync --locked --group dev` completed. The full required regression command,
`uv run --locked python -m pytest -q -ra`, passed **3,189 tests, with 3 intentional
skips and 0 failures**, in 95.31 seconds. The skips require the optional IQ Option
package, a disposable root Linux container, and explicit private-dataset opt-in.
Focused and independent-review totals overlap this full result and are not added
to it. These are local engineering tests, not a repaired production observation.

The collector repair was deployed, eligible OS maintenance completed, and the VM
rebooted before the new baseline. Nine eligible packages upgraded with no removals;
Ubuntu's nine phased updates were not forced. NTP is synchronized, dpkg is clean,
and no reboot is pending. The collector automatically restarted at September 16
00:11:56 UTC, with boot `aefc23af-8751-485b-866d-13cc9d671765` and invocation
`41340c8ab2684e1aa24bcade61d77989`. Configuration, credentials, generation,
registration, both old baselines and frozen research artifact hashes matched.

## New preserved observation

The baseline SHA-256 is
`1f551e9373d4bb5f0e71d5eb33d8424727b45d65994456bb2046eab6b3ff5c30`.
It is stored at `/var/lib/atlas-operator-20260915-recovery/baseline.json`.
At creation, all daily partitions passed archive integrity: **4,613 records**,
including 4,515 prospective observations, 98 historical backfill records and zero
TEST records. This provenance does not establish strategy qualification.

The first reviewed check, at September 16 09:39 UTC, covered **5,627.76 seconds**
with no failures: 88 new successful requests per provider, each with a maximum
success gap of 65 seconds. Full final integrity, the next scheduled backup and
86,400 elapsed seconds remain pending. Closed-market Alpaca requests establish
request continuity; they are not new equity-session observations.

The VM-native `atlas-validation-final-20260915-recovery.timer` is enabled and
fires every 15 minutes. It gates full verification until 86,460 elapsed seconds,
retains the first terminal assessment, and shares an operator lock with the
existing hourly Codex follow-up. The first eligible timer slot is approximately
September 17 08:15 UTC / 1:15 AM Pacific. Local app availability affects notification
retrieval, not the installed VM timer or collector.

The native checker passed its actual systemd sandbox smoke test after removing
only redundant `User=root`, which caused systemd's explicit-UID/seccomp path to
drop CAP_SETUID. Root remains the system-service default. NoNewPrivileges,
read-only filesystem restrictions, AF_UNIX-only networking and resource bounds
were retained. The capture child was UID/EUID 999, GID 988, with effective,
permitted and ambient capabilities all zero. Twenty-two local operator tests
passed; the original acceptance criteria and capture logic were unchanged.

## Backups, gaps and remaining evidence

A post-reboot backup and the later scheduled backup both passed server checksum
and downloaded snapshot integrity checks. The scheduled backup
`atlas-backup-20260916T041741Z-978d9c2989bf7927` uploaded automatically and verified
at 04:21:43 UTC. It predates the new baseline and does not satisfy its next daily
backup requirement. Offsite storage is private with a seven-day object lifecycle
and seven-day soft delete; current disk capacity is adequate. Long-run growth
and actual billed cost remain observations to measure.

All nine outstanding USO gaps were traced to saved provider responses omitting
the target USO bar while comparison symbols were present. Hashes and complete
pagination were checked across 112 distinct archived capture locations; bars
remain unresolved and were not invented. Provider-internal cause is unknown.

The Alpaca permission audit is complete with a limitation: local credentials
have private ownership/mode, but the existing retail paper key has not been
proven data-only at the provider. Broker API scoped-key documentation does not
establish scopes for that retail key. A separate paper account/session remains
necessary; the collector key and halted historical paper journal were preserved.

The separate paper implementation includes source-bound intake, durable replay
checks, a pinned account/risk/strategy/generation binding and an execution cycle
that submits no orders by default. Full combined regression: **3,777 passed,
3 intentional skips, 0 failures** in 174.59 seconds; independent review findings
were resolved. Paper commit `8f26c39` and collector commit `e5e0108` were merged
without changing the tested paper tree. The cloud release contains no order
modules. No actual authenticated Alpaca lifecycle completed, and no account was
funded or live order enabled.

The actual Alpaca login remains required before the separate $100 PAPER account
preflight/lifecycle. Strategy qualification then requires its prospective
observation period and performance evidence; no result can be substituted from
synthetic tests. IQ Option remains inactive and does not block this collector.

Mac power logs show prebaseline clamshell sleep and maintenance dark wakes from
September 15 17:31:56 to full wake at 19:29:40 Pacific. That is not proof of power-off
or sleep for this new 24-hour window. Collector validation and physical Mac power
evidence remain separate.

Do not modify or restart the VM/collector, rotate credentials, run manual backups,
or replace the baseline during this observation. Both old failures remain in
`clock-boundary-20260915` and `offsite-gcp`; current private receipts, baseline,
operator scripts, audits and handoff are in the sibling evidence directory
`collector-recovery-20260915` under the hosting-selection-20260914 evidence root.
