# Collector validation operator

These separately installed Linux operator scripts preserve evidence for a
24-hour data-collection observation. They are not part of the minimal collector
release and expose no broker/order transport. The checked-in paths describe the
`20260917-diagnostics` deployment namespace; copying these files alone does not
install or activate services.

Full integrity verification copies the archive while briefly holding its shared
snapshot barrier, then releases the barrier before scanning the private copy.
Verification must match the captured archive counts, provenance, generation and
contract. The original strict `assess()` criteria are unchanged. The copy refuses
symlinks, hardlinks, public permissions, nonregular files and SQLite sidecars;
it enforces disk reserve, 512 MiB per file, 1 GiB total, 10,000 entries and a
cooperative five-second copy budget. The budget cannot preempt a stalled OS call.

The root wrapper drops to the collector account for capture. The guard serializes
operator work and retains terminal evidence once. The native timer gates full
assessment until 86,460 seconds and works independently of the local Mac. PASS
still requires at least 86,400 real seconds, unchanged boot/configuration/collector
invocation, continuous successful provider requests without failures, full archive
integrity and a new scheduled backup with verified automatic offsite upload.

Before creating a baseline, verify the installed collector release and protected
configuration, run `native_final_check.py --smoke` through the actual service
sandbox, and preserve the fresh PASS receipt, checker hash and measured snapshot
barrier duration. The baseline helper's historical smoke lookup alone does not
pin that receipt to the current checker. `prepare_baseline.py --expected-release
<verified-release>` then checks time synchronization, health and two consistent
captures and writes a baseline only once. Read back any uncertain result before
retrying; never overwrite an existing baseline. Freeze the collector and host
configuration after creation. Preserve unsuccessful observations separately.

The service defaults to root and intentionally omits an explicit `User=root`:
the installed systemd sandbox must retain the ability to drop privileges through
`runuser`. Its child capture is verified as the collector UID/GID with zero
effective/permitted/ambient capabilities. The source archive is read-only in the
service sandbox; only private evidence directories and the operator lock are
writable.

Run the portable offline operator regressions from the repository root:

```sh
uv run --locked python -m pytest -q \
  ops/collector_validation/test_check_snapshot.py \
  ops/collector_validation/test_native_final_check.py
```

These 49 tests supplement the application suite. No private archive, credentials,
account receipts, baseline captures or generated deployment bundles belong here.
Infrastructure acceptance does not establish Mac power history, authenticated
paper execution or prospective strategy qualification.
