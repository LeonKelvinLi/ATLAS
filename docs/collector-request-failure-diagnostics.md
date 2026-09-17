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
