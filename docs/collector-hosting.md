# ATLAS data-only VPS collector

## Status and scope

**PARTIALLY READY:** the foreground collector, daily archive partitions, gap and
retry checkpoints, health command, verified backups, systemd units and installer
are implemented. A Google Cloud e2-micro host has been installed, with bounded
Alpaca/Coinbase GET checks, non-root collection, verified backups, separate local
and offsite restores, and automatic collection after a real machine reboot.
The initial after-close IEX quote failure is addressed below. Continuous 24-hour
uptime and a period with the Mac shut down have not yet been demonstrated. No
order, account, transfer or strategy evaluation request is part of this deployment.

This is a separate collector process. Its installed release contains an explicit
allowlist of standard-library Python modules. It excludes broker/order modules,
MCP servers, strategy implementations, model pickles, execution journals and the
scientific Python environment. Existing research and sealed archives are not
migrated or modified automatically. Collection does not qualify a strategy.

## Audit of the starting system

| Question | State before this change |
|---|---|
| Alpaca input | `data_collector.collect_once`: pinned IEX stock bars, regular calendar and separate latest quotes; explicit external key file |
| Coinbase input | Public Coinbase Exchange BTC-USD candles and separate level-one book; no credentials |
| Collection command | `scripts/atlas_data.py collect-once`; one bounded request window, no continuing loop |
| Storage | Private external SQLite archive with raw captures, immutable contract, observation revisions and batch hashes; no Postgres requirement |
| Existing selected data | 14,843 historical bars; no prospective study established |
| Failure behavior | Refused partial dataset, retained safely received raw responses; no automatic retry/reconnect |
| Restarts and duplicates | Exact repeated observations retained their first revision/ingestion; no daemon checkpoints or reboot activation |
| Time and coverage | UTC receipt clocks rounded up, original timestamps preserved, strict chronology, calendar/gap coverage command |
| Historical distinction | Explicit historical/test/prospective labels and sticky revision lineage; no historical ingestion fabrication |
| Execution exposure | One-shot code exposed only GETs, but imported helpers through broader research/preflight modules; unrelated repo modules could represent orders |
| Resource problem | Every append scanned accumulated history; a single archive had bounded lifetime capacity, unsuitable for indefinite frequent polling |

The new collector resolves the operational gaps while preserving Data Contract V1,
the fixed universe, five-minute bars, study windows, embargoes, original evidence,
qualification thresholds and sealed-holdout rules. The shared threshold mapping
moved to a pure module without numerical changes to remove scientific imports.

## Continuous operation

`scripts/atlas_collect.sh run --config /etc/atlas-collector/config.json` is the
foreground entry point. `init` explicitly creates a generation; `once` runs a
bounded cycle. A populated, missing or damaged state directory is never silently
reseeded. Explicit init also accepts a pre-created empty private directory so a
non-root service account can initialize under `/var/lib`.

The default loop polls every 60 seconds, requesting a 900-second overlapping bar
window and current quotes. It waits interruptibly, logs JSON UTC startup/shutdown
and request failures/reconnections, journals gap events in SQLite, and handles SIGTERM/SIGINT. Provider
retry counters and next-attempt deadlines survive restart. Bounded exponential
backoff reaches 3,600 seconds; parsed Retry-After is a minimum up to 86,400 seconds.
The HTTPS clients refuse redirects and non-allowlisted paths, bound pages and
response bytes, and time out. Provider failures do not log credentials or raw text.

Request windows use the floor of the actual wall-clock sample before selecting
the latest completed five-minute boundary. Operational and ingestion receipts
still round upward. This prevents a poll just before a boundary from requesting
a future window; the provider collector's no-future-data guard remains intact.

An Alpaca quote side with zero price, zero size and a blank exchange is treated
as unavailable: its exact raw response is retained, no two-sided quote is
admitted, and valid bars in that response batch remain collectable. Other invalid
quote shapes still fail validation. Market state closes at the recorded session
close; the final-bar delivery grace is separate from quote freshness. Closed
markets do not require fresh quotes, while failed or stale HTTP collection still
reports unhealthy. This does not change Data Contract V1 or qualify missing data.

One process lock prevents competing collectors. The snapshot barrier prevents
backups from splitting an archive commit from its operational checkpoint. If a
process dies after committing a capture but before checkpointing, the next poll
can repeat the capture without duplicating the observation. SQLite rollback
journals recover committed state on restart; corrupt state stays intact and blocks.

The archive is partitioned by **event date**, under one actual-time registration.
Each event's original and revised observations stay in one UTC-day partition.
Whole-history startup/backup verification scans one day at a time; an append
verifies its affected day. Unchanged old quotes during market closures keep new
raw receipts on the current day, avoiding unbounded growth of an old session.
A catalog detects missing partitions and permits recovery of only intact matching
shards left between initialization and catalog commit. There is no holdout-price
export in this service.

## Gaps and prospective evidence

The loop compares expected mature five-minute closes with returned bars. Crypto
uses UTC 24/7; equities use explicitly returned New York regular sessions. Missing
calendar coverage is unknown, not inferred closure. Network outages and restores
are recorded as operational discontinuities. A missing provider bucket does not
prove whether there was no trading or the data source omitted it.

After an outage, the reconnect response is historical backfill, including bars
inside the overlap window. Older missing periods are requested in chunks of at
most one day. Restored backups force a discontinuity. Resolving a gap appends an
event; it never erases the incident or promotes historical records. Stale intake
is historical; test transports/clocks remain test data. A subsequent timely new
observation can be prospective, while earlier historical lineage stays historical.

Operational continuous duration resets on failures, gaps, restart and stale
heartbeat. It is neither uninterrupted market-evidence coverage nor a strategy
qualification duration. All saved observation timestamps and unknown publication,
volume/corporate-action/cost evidence remain governed by Data Contract V1.

## Host and resource requirements

Use **Ubuntu 24.04 LTS** or **Debian 13**, Python 3.12+, systemd, one shared vCPU,
1 GB RAM and initially 25 GB SSD. No Docker, Kubernetes, Postgres or third-party
Python packages are needed in the installed collector. Docker was used only for
bounded Linux verification in development.

DigitalOcean currently lists a regular Basic Droplet with one vCPU, 1 GiB RAM,
25 GiB SSD and 1,000 GiB transfer at **$6/month**, before taxes and optional add-ons.
This is an example fitting the initial budget, not a purchased or provisioned host.
[Official Droplet pricing](https://www.digitalocean.com/pricing/droplets).

Measured in an Ubuntu 24.04 container limited to **one CPU and 1 GiB**, with network
disabled and synthetic provider responses:

| Measurement | Result |
|---|---:|
| Import baseline peak process RSS | 22.5 MB |
| Collection peak process RSS | 33.0 MB |
| Simulated collection | 120 minutes, 240 provider attempts, 723 test observations |
| Elapsed execution | 27.08 seconds |
| CPU consumed | 26.31 seconds |
| Last / slowest two-provider cycle | 0.423 / 0.444 seconds |
| CPU averaged over the simulated 60-second cadence | 0.37% of one core |
| Archive written | 1.76 MB |
| Linear archive projection | 21.1 MB/day; 633 MB/30 days |
| Synthetic response payload projection | 3.93 MB/day; actual network traffic was zero |

These are process measurements over an accelerated two-hour fixture, not VPS idle
RAM, full-day behavior or observed monthly growth. TLS overhead, OS RAM, provider
response sizes, persistent logs, historical imports and operational journals add
usage. Per-append time grows within the current day and resets with partitioning;
longer peak-day workloads still need observation on the chosen host.

**One CPU / 1 GB is sufficient for this measured collection workload.** It is not
a claim that strategy optimization or the full scientific research stack fits.
20–25 GB is sufficient for initial intake, but **not established as sufficient for
the entire 270-day study with seven full local backups**. At the measured linear
rate, one archive projects to about 5.7 GB over 270 days; archive plus seven full
snapshots is about 45.6 GB, before staging, restore copies, logs and the OS. Do not
promise indefinite $6 hosting with that retention. Plan a larger disk, fewer local
snapshots after verified offsite backups, or separately reviewed compression.
The collector stops before exhausting its configured disk reserve.

## Persistence and backups

| Path | Purpose |
|---|---|
| `/var/lib/atlas-collector/state.sqlite3` | Retry deadlines, cursors, observed timestamps, durable gaps and append-only operational events |
| `archives/<generation>/registration.json` | Immutable Data Contract V1 and actual registration clock |
| `archives/<generation>/catalog.sqlite3` | Registered UTC-day partitions |
| `archives/<generation>/days/YYYY-MM-DD/archive.sqlite3` | Raw responses, observations, revisions and sealed bucket membership |
| `registrations/`, `specifications/`, `research_artifacts/` | Explicitly staged reproducibility artifacts; secret-free allowlisted file types |
| `/etc/atlas-collector/config.json` | Private operational configuration, outside backups |
| `/etc/atlas-collector/alpaca-data.json` | Private credential file, outside backups |
| `/var/backups/atlas-collector/atlas-backup-*` | Timestamped verified local snapshots |
| systemd journal | Persistent operational logs, separate from research evidence |

Daily systemd backup timer runs at approximately 04:15 UTC, with a randomized
five-minute delay and a persistent missed-run schedule. It waits up to 90 seconds
for an active collection transaction to release the snapshot barrier. It uses SQLite's online
backup API under the shared barrier, copies only approved nonsecret data, writes
a hash manifest last and verifies every file plus full archive integrity. Seven
verified snapshots are retained by default. Failed or unrecognized backups are
preserved for inspection instead of silently deleted. Secret-named files and
recognized credential content are excluded/refused; arbitrary confidential data
must never be placed in approved artifact directories.

The optional [private GCS backup extension](collector-offsite.md) adds a separate
service after local backup success. It uses the VM's short-lived identity, a
bucket-scoped create/get role, and exact-generation downloaded verification.
Its installer flag, restore drill, retention override, capacity limits and
deployment evidence requirements are documented there. Enabling the extension
alone does not prove that a scheduled backup or restore has succeeded.

Under `research_artifacts/` only, frozen `.pkl` files are bounded opaque bytes:
they are copied and hashed, never deserialized. `.db` research journals use the
SQLite online backup API, including committed WAL changes. All staged files must
be 0600, directories 0700, and owned by the collector. The separate preservation
bundle contains the eight frozen artifacts, a verified snapshot of the original
14,843-bar archive, and four contract/coverage records. Its local backup and restore
were verified without modifying the originals. This data is separate from the
installed executable release. Health counts cover the selected active generation;
backups verify and preserve all staged archives and artifacts.

These are full private copies, not compressed, encrypted or deduplicated storage.
A same-disk backup does not survive disk/VPS loss. For future S3-compatible backup,
first create and verify a local snapshot, upload that **completed snapshot directory
and manifest only**, then verify a downloaded restore. Keep storage credentials
outside `/var/lib/atlas-collector` and use a separate backup process with a restricted
bucket prefix. No remote provider or remote backup credential is configured here.

Restore always targets a new directory, verifies files and archives, and writes a
restore-discontinuity marker. An incomplete restore sentinel blocks collection.
Preserve the previous state directory until the restored archive and runtime health
have been verified. Daily snapshots do not authorize modification of sealed data.

## Secrets and order boundary

Coinbase Exchange public candles/book require **no API key**. Alpaca requires an
explicit external owner-only 0600 JSON file containing `api_key` and `api_secret`.
The bounded verification used the pre-existing paper-pilot key for market-data
GETs only; its underlying account permissions were not independently audited.
Create a separate least-privilege data credential where the provider supports it.
Never give this service live execution credentials. Alpaca market-data access may
still use an account key with broader permissions; the collector cannot establish
least privilege merely by naming the file “data”.

`.env.example` contains names only and is not sourced. Secrets never appear in
command-line arguments, logs or backups. The installer copies the selected key
privately, using its **file path** as the argument. Runtime JSON requires
`mode: data_collection` and `live_trading: false`; true live flags in the environment
refuse startup. systemd explicitly sets all live flags false. An execution process
would need a separate future release, configuration and permissions; no such
process is installed by this workflow.

systemd runs as `atlas-collector`, with no login shell, no capabilities, a strict
read-only system filesystem, private temporary/devices, bounded memory/CPU and
only the state directory writable. No health web port is opened. The installer
must run as root to manage Linux service files; the collector itself runs non-root.
The supplied journald limits are **host-wide** (128 MB and 14 days), suitable for a
dedicated VPS. Inspect them before using a shared host.

## Deployment and operations

The completed work is committed locally and has not been pushed. A fresh GitHub
clone alone will not contain it yet. Use the supplied source release archive, or
transfer the local branch before following repository-based installation.

On a new Ubuntu 24.04 / Debian 13 VPS, logged in as an SSH user with sudo:

```bash
sudo apt-get update
sudo apt-get install -y python3 ca-certificates tzdata
mkdir -p ~/atlas-release
tar -xzf ~/atlas-collector-release.tar.gz -C ~/atlas-release
cd ~/atlas-release
install -m 600 /dev/null ~/alpaca-data.json
nano ~/alpaca-data.json
# Enter the JSON object privately: api_key and api_secret only.
sudo sh ./deploy.sh --alpaca-key-file "$HOME/alpaca-data.json"
sudo -u atlas-collector python3 -I /opt/atlas-collector/current/scripts/atlas_health.py --config /etc/atlas-collector/config.json
```

Transfer the generated tarball with `scp` first. Do not paste API secrets into SSH
history. The service and daily backup timer are enabled for reboot. Initial health
can be unhealthy until first data arrives. After provider health succeeds on the
VPS, verify another collection later with the Mac offline; do not infer uptime
from `systemctl enable` alone.

Stage the supplied historical/frozen evidence once, after fresh installation.
Transfer `atlas-research-preservation.tar.gz` to the VPS first. The following
refuses existing destinations, pauses both collection and backups, and leaves
the newly registered active archive separate from the original study:

```bash
mkdir -p ~/atlas-preservation
tar -xzf ~/atlas-research-preservation.tar.gz -C ~/atlas-preservation
sudo sh -eu -s -- "$HOME/atlas-preservation/payload" <<'SH'
source_tree="$1"
test ! -e /var/lib/atlas-collector/research_artifacts
test ! -e /var/lib/atlas-collector/archives/preserved-v1-20260913
systemctl stop atlas-collector-backup.timer atlas-collector-backup.service atlas-collector.service
cp -R "$source_tree/research_artifacts" /var/lib/atlas-collector/
cp -R "$source_tree/archives/preserved-v1-20260913" /var/lib/atlas-collector/archives/
chown -R atlas-collector:atlas-collector /var/lib/atlas-collector/research_artifacts /var/lib/atlas-collector/archives/preserved-v1-20260913
systemctl start atlas-collector-backup.service
systemctl start atlas-collector.service atlas-collector-backup.timer
SH
```

If this fails, keep the services stopped, inspect the error and preserve the
partial copy. Do not overwrite existing research data to retry it. The backup
command verifies all archive chains before the restart. The included preservation
manifest records source and copied hashes; the local artifact restore check was
byte-identical to the staged payload. No historical observations are inserted into
the active generation.

| Operation | Command |
|---|---|
| STATUS | `sudo systemctl status atlas-collector.service atlas-collector-backup.timer` |
| START | `sudo systemctl start atlas-collector.service` |
| STOP | `sudo systemctl stop atlas-collector.service` |
| RESTART | `sudo systemctl restart atlas-collector.service` |
| LOGS | `sudo journalctl -u atlas-collector.service -f` |
| HEALTH | `sudo -u atlas-collector python3 -I /opt/atlas-collector/current/scripts/atlas_health.py --config /etc/atlas-collector/config.json` |
| BACKUP | `sudo systemctl start atlas-collector-backup.service` |
| BACKUP STATUS | `sudo journalctl -u atlas-collector-backup.service -n 30` |
| UPDATE | Unpack a reviewed new release, then `sudo sh ./deploy.sh` without credential/config flags; existing state/config are preserved and a verified backup precedes replacement |

Restore a selected verified snapshot (substitute its real directory name):

```bash
sudo systemctl stop atlas-collector.service
sudo systemctl stop atlas-collector-backup.timer atlas-collector-backup.service
sudo install -d -m 700 -o atlas-collector -g atlas-collector /var/lib/atlas-restore
sudo -u atlas-collector python3 -I /opt/atlas-collector/current/scripts/atlas_backup.py restore \
  --backup /var/backups/atlas-collector/atlas-backup-SELECTED \
  --destination /var/lib/atlas-restore/restored
# Preserve old state, then put the verified replacement at the configured path.
sudo mv /var/lib/atlas-collector /var/lib/atlas-collector-before-restore
sudo mv /var/lib/atlas-restore/restored /var/lib/atlas-collector
sudo systemctl start atlas-collector.service
sudo systemctl start atlas-collector-backup.timer
sudo -u atlas-collector python3 -I /opt/atlas-collector/current/scripts/atlas_health.py --config /etc/atlas-collector/config.json
```

If either destination already exists, stop and choose a new name; never overwrite
or delete prior state to force recovery. Keep API credentials and restore-point
selection separate from the archive backup. After a restore, outage records remain
and caught-up bars are historical.

## Verified boundaries and remaining work

- Complete repository suite after the after-close fix: **3,086 passed, 3 skipped, 0 failed**. The skips
  are optional IQ vendor tests, a private DuckDB dataset and the explicitly opt-in
  disposable Linux installer test. That installer test was run separately and
  passed on Ubuntu. The minimal one-CPU/1-GiB Linux collector suite passed
  **196 tests**, with only that opt-in installer case skipped.
- Real GET check: Alpaca and Coinbase succeeded; 5 responses, 19 observations,
  15 historical and 4 newly observed stock quotes. No orders or account queries.
  The old 14,843-bar archive is unchanged. A USO bucket was absent in the short
  returned stock window; its cause remains unknown.
- Offline tests cover rate limits, timeouts, bounded reconnect, duplicates,
  missing intervals, low disk, secret exclusion, backup corruption and restores.
- Real subprocess tests cover hot SQLite rollback journals, SIGKILL between
  archive commit and checkpoint, competing process locks, duplicate-safe restart,
  SIGTERM and corruption/incomplete restore refusal.
- Ubuntu integration exercises real non-root initialization, backup/update data
  preservation, installed command imports and `systemd-analyze verify`. Package
  installation and systemctl activation were simulated in that test.
- The accelerated loop/endurance and resource tests are not 24-hour operation.

Resource evidence is from the final archive implementation, including registered
partition identity and catalog guard checks. An initial Linux test-image failure
was a test-only scientific import; its shared-constant assertion now uses AST and
fixed numeric values. A separate initial macOS Docker bind-output check correctly
refused container/host owner mismatch; the benchmark then ran in a private
container-native directory. Neither failed attempt is counted as a passing run.

Outstanding: observe a 24-hour run including a period with the Mac off; independently
audit provider-side credential permissions; configure scheduled offsite backups
(the initial manual offsite restore passed); choose storage/retention that fits
the intended collection duration and verify billed cost. Keep provider gaps
explicit. Original strategy/data qualification blockers remain; none of this
permits live trading. Actual host status and deployment receipts are maintained
separately from this reusable installation guide.
