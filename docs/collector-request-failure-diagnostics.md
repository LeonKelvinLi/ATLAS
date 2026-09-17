# Collector request failure diagnosis

The preserved validation beginning September 16, 2026 at 08:06:05 UTC failed.
Coinbase recorded a failed request at September 17 04:15:25 UTC and automatically
reconnected at 04:16:28 UTC. The original boot, collector invocation and
configuration were unchanged. The failed request began at 04:15:15 UTC. Its
ten-second duration is consistent with a timeout, but the exact cause is unknown:
the old runtime discarded the returned error/status, and no completed Coinbase
capture or failure archive batch exists in that interval.

The repair retains allowlisted acquisition error codes, a fixed request role,
bounded HTTP status and retry metadata in the append-only failure event and
structured journal output. Recovery clears current failure state but keeps that
event. It works even when the request fails before any response is captured.
Raw response bodies, URLs, headers, credentials and exception messages never
become diagnostic text. Known built-in network exception types have fixed labels;
unknown types and untrusted injected metadata fall back to a generic label.

The HTTP transport now deducts connection/send time from the remaining socket
budget before waiting for headers. The same deadline continues through body
reads. It does not add retries, bypass certificate verification, relax parsing,
change source/provenance classification or enable an order endpoint. This is not
a claim that all platform DNS/connect stages can be forcibly interrupted at the
deadline or that an external provider can never fail.

The strict 24-hour acceptance criteria are unchanged. A recovered request failure
still fails the observation. The original baseline and terminal evidence must
remain intact; any replacement observation uses a separate identity after
deployment and the actual VM checker smoke test. Neither engineering tests nor
infrastructure uptime establish prospective strategy qualification.

The scheduled September 17 backup completed and its offsite receipt verified at
04:26:28 UTC, including server checksum and downloaded snapshot integrity. This
does not change the failed validation result or satisfy a later observation's
requirement for a new scheduled backup.

Private incident evidence is in the external `coinbase-failure-20260917` folder
under the ATLAS hosting-selection evidence directory. It includes the exact
failed capture/history, protected-file hashes and bounded archive metadata.

## Deployed repair and independent checker fix

Application commit `1dccf563a84dd8aaad536dc619449650c8176d7f` passed the full
suite: **3,828 passed, 3 intentional skips, zero failures**. Collector release
`1b3b55f693e61062` is deployed with all nineteen allowlisted source hashes
verified and protected configuration unchanged. The minimal release excludes
order modules.

The first prebaseline checker smoke exposed a separate defect: a full archive
scan held the collector's snapshot barrier long enough to cause three automatic
`collector_lock_unavailable` restarts. That failed smoke and its outage evidence
are preserved. The [operator repair](../ops/collector_validation/README.md) copies
the archive under a bounded brief barrier, then performs full verification on
that private consistent copy after releasing the collector. The `assess()` source
and its strict acceptance criteria remain byte-for-byte unchanged.

All **49 operator regression tests passed**. The actual VM sandbox smoke passed
with healthy providers, full archive integrity and unchanged collector invocation;
its barrier lasted 0.192 seconds. At the actual new baseline it lasted 0.098
seconds. The child ran as the collector UID/GID with zero effective, permitted
or ambient capabilities. These measurements establish this run's behavior, not
a guarantee that an OS disk operation can always be interrupted within the budget.

The new immutable baseline is **September 17, 2026 at 04:59:48.940222 UTC**
(September 16, 9:59:48.940222 PM Pacific), SHA-256
`3bcde6e059e31b9551abb6062dd7962faa8824d93df64fc9527c40323d05d3f0`.
Its 7,850 records passed full integrity: 7,746 prospective observations, 104
historical backfill records and zero TEST records. Both providers were healthy
without consecutive failures or catchup. The independently read-back baseline
matches the creation receipt exactly. Prior gaps remain visible.

The earliest possible full-day checkpoint is **September 18 at 04:59:48.940222
UTC** (September 17, 9:59:48.940222 PM Pacific). The separate VM-native timer
is enabled and gates final assessment until 86,460 elapsed seconds; it does not
depend on the Mac. This observation is **PENDING**, not a completed 24-hour PASS.
Do not modify or restart the collector, VM, configuration or credentials while
it runs. Actual paper execution, prospective strategy qualification and physical
Mac-off evidence remain separate and unproven. Live trading remains disabled.
