#!/usr/bin/env python3
"""Root operator wrapper: read-only collector checks, new private evidence files."""
import json, os, pwd, stat, subprocess, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
SCRIPTS = Path("/usr/local/libexec/atlas-checks-20260917-diagnostics")
WORK = Path("/var/lib/atlas-operator-20260917-diagnostics"); BASELINE = WORK / "baseline.json"
def check(path, uid, mode=None, directory=False):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise PermissionError("symlink_refused")
    info = path.stat()
    if (info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022
            or mode is not None and stat.S_IMODE(info.st_mode) != mode
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
        raise PermissionError("ownership_or_permissions_refused")
def save(path, value, account):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchown(stream.fileno(), account.pw_uid, account.pw_gid); os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, sort_keys=True, indent=2); stream.flush(); os.fsync(stream.fileno())
def invoke(script, args, *, collector):
    command = ["/usr/bin/python3", "-I", str(SCRIPTS / script), *map(str, args)]
    if collector:
        command = ["/usr/sbin/runuser", "--user", "atlas-collector", "--", *command]
    process = subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
    if len(process.stdout) > 16 * 1024**2:
        raise ValueError("output_bound_exceeded")
    result = json.loads(process.stdout)
    if type(result) is not dict:
        raise ValueError("object_result_required")
    return result
def main():
    phase, error_path, account = "validate_paths", None, None
    try:
        if os.geteuid() != 0:
            raise PermissionError("root_required")
        account = pwd.getpwnam("atlas-collector")
        for parent in {*SCRIPTS.parents, *WORK.parents, SCRIPTS}:
            check(parent, 0, directory=True)
        for name in ("check_unattended.py", "unattended_service_history.py", "run_unattended_check.py"):
            check(SCRIPTS / name, 0)
        check(WORK, account.pw_uid, 0o700, True); check(BASELINE, account.pw_uid, 0o600)
        prefix = "check-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + uuid.uuid4().hex[:8]
        capture, history, assessment, error_path = (WORK / (prefix + "-" + suffix + ".json") for suffix in ("capture", "history", "assessment", "error"))
        phase = "capture"
        invoke("check_unattended.py", ["--baseline", BASELINE, "--capture-out", capture], collector=True)
        check(capture, account.pw_uid, 0o600)
        phase = "export_service_history"
        save(history, invoke("unattended_service_history.py", ["--baseline", BASELINE, "--current", capture], collector=False), account)
        phase = "assess"
        result = invoke("check_unattended.py", ["--baseline", BASELINE, "--current", capture, "--service-history", history], collector=True)
        save(assessment, result, account)
        print(json.dumps({"result_path": str(assessment), **{k: result.get(k) for k in ("infrastructure_24h_status", "elapsed_seconds", "failures", "pending", "providers", "scheduled_offsite_proven", "mac_off_evidence")}, "recorded_gap_count": len(result.get("current", {}).get("gaps", []))}, sort_keys=True))
        return 0
    except Exception as exc:
        result = {"infrastructure_24h_status": "ERROR", "phase": phase, "error_type": type(exc).__name__, "execution_enabled": False}
        if isinstance(exc, subprocess.CalledProcessError):
            result["exit_code"] = exc.returncode
        if error_path is not None:
            save(error_path, result, account)
        print(json.dumps({**result, "result_path": str(error_path) if error_path else None}, sort_keys=True))
        return 1
if __name__ == "__main__":
    sys.exit(main())
