# Collector recovery: advancing archive time and retained fatal diagnostics

Status: implementation prepared locally on `codex/atlas-collector-recovery`,
based on deployed source `e36ca70`. The VM has not been changed and the failed
24-hour observation remains FAIL. No new baseline or uptime claim is made.

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
an evidence-backed inference, and the repair has not yet been validated there.

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

Deployment, a maintenance-aware fresh observation, the next scheduled backup and
offsite upload, full archive verification, Mac power evidence, new USO gaps and
strategy qualification remain separate work. The collector VM, both baselines,
the paper-execution branch, credentials and frozen research remain unchanged.
