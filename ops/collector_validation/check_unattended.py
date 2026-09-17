#!/usr/bin/env python3
"""Run as atlas-collector: capture a private baseline, or assess it without mutations."""
import argparse, fcntl, hashlib, json, math, os, shutil, stat, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from unattended_service_history import attribution, stamp
RELEASE = "/opt/atlas-collector/current"
CONFIG = "/etc/atlas-collector/config.json"
RECEIPT = "/var/lib/atlas-collector-offsite/latest.json"
SUCCESS = {"request_succeeded", "reconnected"}
WORK = Path("/var/lib/atlas-operator-20260917-diagnostics")
SNAPSHOT_MAX_FILE_BYTES = 512 * 1024**2
SNAPSHOT_MAX_TOTAL_BYTES = 1024**3
SNAPSHOT_MAX_ENTRIES = 10000
SNAPSHOT_LOCK_SECONDS = 5
SNAPSHOT_CHUNK_BYTES = 64 * 1024


def _private_info(info, *, directory=False):
    if (info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise ValueError("snapshot_owner_type_or_permissions_invalid")


def _plain_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("snapshot_path_invalid")
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("snapshot_symlink_refused")
    return path


def _copy_archive_snapshot(source, destination, *, deadline, reserve):
    """Copy exact idle DELETE-journal files while the caller holds the barrier.

    No source file is opened writable and no SQLite connection is created here.
    Any sidecar is refused, including a leftover journal: the checker must not
    repair it or accidentally verify a database without its transaction state.
    Directory-relative no-follow opens keep every copied inode inside the tree.
    """
    source = _plain_path(source)
    destination = _plain_path(destination)
    tally = {"entries": 0, "bytes": 0, "files": 0}

    def budget():
        if time.monotonic() >= deadline:
            raise ValueError("snapshot_copy_budget_exceeded")

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def copy_dir(source_fd, target):
        budget()
        _private_info(os.fstat(source_fd), directory=True)
        target.mkdir(mode=0o700)
        with os.scandir(source_fd) as entries:
            for entry in entries:
                budget()
                tally["entries"] += 1
                if tally["entries"] > SNAPSHOT_MAX_ENTRIES:
                    raise ValueError("snapshot_entry_bound_exceeded")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError("snapshot_symlink_refused")
                if stat.S_ISDIR(info.st_mode):
                    _private_info(info, directory=True)
                    fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=source_fd)
                    try:
                        if identity(os.fstat(fd)) != identity(info):
                            raise ValueError("snapshot_source_changed")
                        copy_dir(fd, target / entry.name)
                    finally:
                        os.close(fd)
                    continue
                _private_info(info)
                if entry.name.endswith(("-journal", "-wal", "-shm")):
                    raise ValueError("snapshot_sqlite_sidecar_refused")
                if (info.st_size > SNAPSHOT_MAX_FILE_BYTES
                        or tally["bytes"] + info.st_size > SNAPSHOT_MAX_TOTAL_BYTES):
                    raise ValueError("snapshot_size_bound_exceeded")
                if shutil.disk_usage(target).free < reserve + info.st_size:
                    raise ValueError("snapshot_disk_reserve_exhausted")
                fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=source_fd)
                with os.fdopen(fd, "rb") as reader:
                    opened = os.fstat(reader.fileno())
                    _private_info(opened)
                    if identity(opened) != identity(info):
                        raise ValueError("snapshot_source_changed")
                    output = target / entry.name
                    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    count = 0
                    with os.fdopen(fd, "wb") as writer:
                        while True:
                            budget()
                            chunk = reader.read(min(SNAPSHOT_CHUNK_BYTES, info.st_size - count + 1))
                            if not chunk:
                                break
                            count += len(chunk)
                            if count > info.st_size:
                                raise ValueError("snapshot_source_changed")
                            writer.write(chunk)
                        writer.flush()
                        if os.fstat(writer.fileno()).st_size != count:
                            raise ValueError("snapshot_copy_incomplete")
                    if count != info.st_size or identity(os.fstat(reader.fileno())) != identity(info):
                        raise ValueError("snapshot_source_changed")
                tally["bytes"] += count
                tally["files"] += 1
        budget()

    fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        copy_dir(fd, destination)
    finally:
        os.close(fd)
    return tally


def _verify_snapshot(config, health, snapshot, *, now, registration_sha256, verifier):
    """Reconcile a full scan of the detached copy with the locked state capture."""
    from atlas.collection.config import contract_fingerprint
    registration = (snapshot / "registration.json").read_bytes()
    if hashlib.sha256(registration).hexdigest() != registration_sha256:
        raise ValueError("snapshot_registration_changed")
    archive = verifier(snapshot, clock=lambda: math.ceil(now), full=True)
    operational = health["archive"]
    started = health["prospective_collection"]["started"]
    if (archive.get("integrity_verified") is not True
            or archive.get("verification_scope") != "all_daily_partitions"
            or archive.get("generation_id") != config["generation_id"]
            or started is None or archive.get("started_at") != started
            or archive.get("contract_sha256") != contract_fingerprint(config, started)
            or operational.get("integrity") != "NOT_CHECKED"
            or any(archive.get(source) != operational.get(target) for source, target in (
                ("record_count", "records"), ("batch_count", "batches"),
                ("latest_timestamp", "latest_timestamp"), ("acquisition_kinds", "acquisition_kinds")))):
        raise ValueError("snapshot_archive_metadata_mismatch")
    # Existing operational blockers remain intact. Only a complete matching scan
    # upgrades NOT_CHECKED; neither failed verification nor partial counts can.
    health["archive"] = {**operational, "integrity": "PASS",
        "verification_scope": archive["verification_scope"],
        "verification_source": "private_consistent_snapshot"}


def _capture_consistent(config, baseline, *, report, connection, verifier):
    scratch = None
    try:
        lock_path = _plain_path(Path(config["state_dir"]) / ".snapshot.lock")
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as lock:
            _private_info(os.fstat(lock.fileno()))
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError("snapshot_busy")
                    time.sleep(.05)
            held_since = time.monotonic()
            deadline = held_since + SNAPSHOT_LOCK_SECONDS
            now = time.time()
            full = baseline is None or now - baseline["captured_at"] >= 86400
            health = report(config, now=math.ceil(now), verify=False)
            with connection(config["state_dir"]) as db:
                watermark = db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
                events = db.execute("SELECT id,time,kind,provider,body FROM events WHERE id>? ORDER BY id",
                                    ((baseline or {}).get("event_watermark", watermark),)).fetchall()
                gaps = [dict(zip(("id", "provider", "symbol", "start", "end", "reason", "detected_at", "resolved_at"), r))
                        for r in db.execute("SELECT * FROM gaps ORDER BY id")]
            if full:
                work = _plain_path(WORK)
                _private_info(work.lstat(), directory=True)
                scratch = Path(tempfile.mkdtemp(prefix=".archive-snapshot-", dir=work))
                snapshot = scratch / "archive"
                copied = _copy_archive_snapshot(config["archive_dir"], snapshot,
                    deadline=deadline, reserve=config["min_free_bytes"])
                registration_sha256 = hashlib.sha256((snapshot / "registration.json").read_bytes()).hexdigest()
            if time.monotonic() >= deadline:
                raise ValueError("snapshot_copy_budget_exceeded")
            barrier_seconds = time.monotonic() - held_since
        # The archive scan can be expensive. It runs only after closing the
        # shared lock, using immutable copied bytes and the captured clock.
        if full:
            _verify_snapshot(config, health, snapshot, now=now,
                registration_sha256=registration_sha256, verifier=verifier)
            health["archive"]["snapshot_bytes"] = copied["bytes"]
            health["archive"]["snapshot_files"] = copied["files"]
            health["archive"]["snapshot_barrier_seconds"] = barrier_seconds
        return now, health, watermark, events, gaps
    finally:
        if scratch is not None:
            shutil.rmtree(scratch)

def systemd(unit):
    wanted = {"InvocationID", "ActiveState", "Result", "ExecMainStartTimestamp", "LastTriggerUSec", "OnSuccess"}
    raw = subprocess.check_output(["systemctl", "show", unit, "--no-pager", "--property=" + ",".join(sorted(wanted))],
                                  env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}, text=True, timeout=10)
    return {k: v for k, v in (line.split("=", 1) for line in raw.splitlines() if "=" in line) if k in wanted}
def capture(baseline=None):
    sys.path.insert(0, RELEASE)
    from atlas.collection.config import load_config, read_json
    from atlas.collection.health import report
    from atlas.collection.state import connection
    config = load_config(CONFIG)
    unit = systemd("atlas-collector.service")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    from atlas.collection.archive import verify_collection_archive
    now, health, watermark, events, gaps = _capture_consistent(config, baseline,
        report=report, connection=connection, verifier=verify_collection_archive)
    try:
        receipt = read_json(RECEIPT)
    except (OSError, ValueError):
        receipt = None
    return {"version": 1, "captured_at": now, "utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "boot_id": boot, "collector": unit, "capture_stable": unit == systemd("atlas-collector.service")
            and boot == Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "config_sha256": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
            "event_watermark": watermark, "events": events, "gaps": gaps, "health": health,
            "backup_timer": systemd("atlas-collector-backup.timer"),
            "backup_service": systemd("atlas-collector-backup.service"),
            "offsite_service": systemd("atlas-collector-offsite.service"), "offsite_receipt": receipt}
def assess(base, current, history=None):
    elapsed = current["captured_at"] - base["captured_at"]
    bad, pending, providers = [], [], {}
    for key in ("boot_id", "config_sha256"):
        if not base[key] or base[key] != current[key]:
            bad.append(key + "_changed_or_missing")
    if not base["collector"].get("InvocationID") or base["collector"].get("InvocationID") != current["collector"].get("InvocationID"):
        bad.append("collector_invocation_changed_or_missing")
    for label, item in (("baseline", base), ("current", current)):
        if not item["capture_stable"] or item["collector"].get("ActiveState") != "active" or item["health"]["status"] != "HEALTHY":
            bad.append(label + "_not_healthy_or_stable")
        if item["health"]["prospective_collection"].get("test_clock") is not False:
            bad.append(label + "_clock_not_verified_real")
    if any(p.get("catchup_pending") for p in base["health"]["providers"].values()):
        bad.append("baseline_recovery_not_finished")
    if base["health"]["archive"]["integrity"] != "PASS":
        bad.append("baseline_archive_not_verified")
    incidents = [r for r in current["events"] if r[2] not in {"request_started", "request_succeeded", "gap_detected", "gap_filled_historically"}
                 or r[2] == "gap_detected" and json.loads(r[4]).get("reason") == "collection_outage"]
    if incidents or [r[0] for r in current["events"]] != list(range(base["event_watermark"] + 1, current["event_watermark"] + 1)) or current["event_watermark"] < base["event_watermark"]:
        bad.append("interruption_failure_or_operator_event")
    for key in ("records", "batches"):
        if not isinstance(current["health"]["archive"].get(key), int) or current["health"]["archive"][key] < base["health"]["archive"][key]:
            bad.append("archive_" + key + "_decreased_or_unknown")
    if set(base["health"]["providers"]) != {"alpaca", "coinbase"} or set(current["health"]["providers"]) != {"alpaca", "coinbase"}:
        bad.append("required_provider_set_changed")
    for provider, state in base["health"]["providers"].items():
        times = [r[1] for r in current["events"] if r[3] == provider and r[2] in SUCCESS]
        points = [state["last_successful_request"] or base["captured_at"], *times, current["captured_at"]]
        gap = max((b - a for a, b in zip(points, points[1:])), default=elapsed)
        providers[provider] = {"new_successes": len(times), "max_success_gap_seconds": gap,
                               "first_new_success": times[0] if times else None, "last_success": points[-2]}
        if gap > 180 or any(b < a for a, b in zip(points[:-1], points[1:-1])):
            bad.append(provider + "_success_gap_or_clock_regression")
    if elapsed < 0:
        bad.append("clock_regressed")
    if elapsed < 86400:
        pending.append("24_hours_not_elapsed")
    if current["health"]["archive"]["integrity"] != "PASS":
        (pending if elapsed < 86400 else bad).append("full_archive_integrity_pending_or_failed")
    receipt, prior = current["offsite_receipt"] or {}, base["offsite_receipt"] or {}
    trigger = stamp(current["backup_timer"].get("LastTriggerUSec"))
    start = stamp(current["backup_service"].get("ExecMainStartTimestamp"))
    created = stamp(receipt.get("backup_created_at"))
    verified = stamp(receipt.get("verified_at"))
    attributed, service_failures, service_pending = attribution(base, current, history, trigger)
    bad.extend(service_failures); pending.extend(service_pending)
    scheduled = (receipt.get("ok") is True and receipt.get("downloaded_snapshot_integrity_verified") is True
                 and bool(receipt.get("generation")) and receipt.get("generation") != prior.get("generation")
                 and base["captured_at"] < trigger <= start <= created <= verified <= math.ceil(current["captured_at"])
                 and start - trigger <= 120 and created - start <= 300 and verified - created <= 3600
                 and bool(receipt.get("offsite_service_invocation_id"))
                 and receipt.get("offsite_service_invocation_id") == current["offsite_service"].get("InvocationID")
                 and current["backup_service"].get("Result") == "success" and current["offsite_service"].get("Result") == "success" and attributed)
    if not scheduled:
        pending.append("new_scheduled_offsite_backup_not_proven")
    return {"infrastructure_24h_status": "FAIL" if bad else "PENDING" if pending else "PASS", "failures": bad,
            "pending": pending, "elapsed_seconds": elapsed, "providers": providers, "scheduled_offsite_proven": scheduled,
            "incident_count": len(incidents), "incidents": [r[:4] for r in incidents[:20]],
            "mac_off_evidence": "UNKNOWN_REQUIRES_USER_ATTESTATION", "mac_off_24h_test_passed": False,
            "baseline_at": base["captured_at"], "current": {k: v for k, v in current.items() if k != "events"},
            "qualification_established": False, "execution_enabled": False}
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--baseline-out")
    modes.add_argument("--baseline")
    parser.add_argument("--capture-out", help="Save a current snapshot before root exports its bounded service journal")
    parser.add_argument("--current", help="Assess this frozen current snapshot instead of contacting systemd/state again")
    parser.add_argument("--service-history", help="Sanitized matching privileged journal export")
    args = parser.parse_args()
    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
    if (args.current or args.capture_out or args.service_history) and not args.baseline or args.current and args.capture_out:
        parser.error("snapshot and journal options require --baseline; choose --current or --capture-out")
    current = json.loads(Path(args.current).read_text()) if args.current else capture(baseline)
    if args.baseline_out or args.capture_out:
        path = Path(args.baseline_out or args.capture_out)
        protected = ("/etc", "/opt", "/proc", "/sys", "/dev", "/var/lib/atlas-collector", "/var/lib/atlas-collector-offsite", "/var/backups")
        if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)) or any(path.is_relative_to(p) for p in protected):
            raise ValueError("baseline_path_not_operator_location")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(current, stream, indent=2, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        result = {"snapshot_saved": str(path), "baseline_saved": bool(args.baseline_out), "full_24h_passed": False}
    else:
        result = assess(baseline, current, json.loads(Path(args.service_history).read_text()) if args.service_history else None)
    print(json.dumps(result, indent=2, sort_keys=True))
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"infrastructure_24h_status": "ERROR", "error_type": type(exc).__name__, "execution_enabled": False}))
        sys.exit(1)
