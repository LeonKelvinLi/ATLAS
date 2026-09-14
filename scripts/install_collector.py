"""Install an allowlisted standard-library collector on Ubuntu/Debian systemd.

No Python package download, strategy module, frozen model, order module or trading
secret is copied into the service release. Importing this installer does nothing.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import uuid

SOURCE = Path(__file__).resolve().parents[1]
LEGACY_FILES = (
    "atlas/__init__.py", "atlas/research/__init__.py", "atlas/collection/__init__.py",
    "atlas/collection/primitives.py", "atlas/collection/config.py", "atlas/collection/state.py",
    "atlas/collection/runtime.py", "atlas/collection/health.py", "atlas/collection/archive.py",
    "atlas/collection/backup.py", "atlas/research/data_contract_v1.py",
    "atlas/research/data_archive.py", "atlas/research/data_collector.py", "atlas/research/data_coverage.py",
    "scripts/atlas_collect.py", "scripts/atlas_health.py", "scripts/atlas_backup.py",
)
OFFSITE_FILES = ("atlas/collection/offsite.py", "scripts/atlas_offsite_backup.py")
FILES = LEGACY_FILES + OFFSITE_FILES
UNITS = ("atlas-collector.service", "atlas-collector-backup.service", "atlas-collector-backup.timer",
         "atlas-collector-offsite.service")
CONFIG_DIR = Path("/etc/atlas-collector")
STATE_DIR = Path("/var/lib/atlas-collector")
BACKUP_DIR = Path("/var/backups/atlas-collector")
BASE = Path("/opt/atlas-collector")
SYSTEMD_DIR = Path("/etc/systemd/system")
JOURNALD_DIR = Path("/etc/systemd/journald.conf.d")
OFFSITE_DROPIN = "atlas-collector-backup.service.d/20-offsite.conf"


def _run(args):
    subprocess.run(args, check=True)


def release_manifest(source=None, *, files=None):
    source = SOURCE if source is None else Path(source)
    result = {}
    for name in FILES if files is None else files:
        path = source / name
        _no_links(path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 500000:
            raise ValueError("collector_release_source_invalid")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def stage_release(destination, source=None):
    source = SOURCE if source is None else Path(source)
    manifest = release_manifest(source)
    destination = Path(destination)
    if destination.exists():
        raise ValueError("collector_release_destination_exists")
    destination.mkdir(mode=0o755)
    for name in FILES:
        target = destination / name
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with target.open("xb") as output:
            os.fchmod(output.fileno(), 0o644)
            output.write((source / name).read_bytes())
            output.flush()
            os.fsync(output.fileno())
    _replace_file(destination / "release.json", (json.dumps(manifest, indent=2, sort_keys=True)+"\n").encode())
    for path in sorted((p for p in destination.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        _sync_directory(path)
    _sync_directory(destination)
    _sync_directory(destination.parent)
    if release_manifest(destination) != manifest:
        raise ValueError("collector_release_changed_during_copy")
    return manifest


def _private_json(path, value, uid, gid):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchown(stream.fileno(), uid, gid)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _host_requirements():
    if (sys.platform != "linux" or os.geteuid() != 0 or sys.version_info < (3,12)
            or not Path("/run/systemd/system").is_dir()):
        raise ValueError("requires_root_linux_systemd_python312")


def _no_links(path):
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("installation_symlink_or_relative_path_refused")


def _directory(path, uid, gid, mode):
    _no_links(path)
    if path.exists():
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != mode:
            raise ValueError("installation_directory_owner_or_mode_refused")
    else:
        path.mkdir(mode=mode)
        os.chown(path, uid, gid)
        path.chmod(mode)
        _sync_directory(path)
        _sync_directory(path.parent)


def _release(path, expected=None):
    _no_links(path)
    manifest_path = path / "release.json"
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
        raise ValueError("collector_release_manifest_invalid")
    recorded = json.loads(manifest_path.read_text())
    if type(recorded) is not dict or set(recorded) not in (set(LEGACY_FILES), set(FILES)):
        raise ValueError("collector_release_allowlist_invalid")
    # A verified pre-offsite release remains a valid rollback target. Only these
    # two complete allowlists are accepted; arbitrary subsets are not releases.
    manifest = release_manifest(path, files=tuple(recorded))
    if recorded != manifest or (expected is not None and manifest != expected):
        raise ValueError("collector_release_integrity_mismatch")
    return manifest


def _replace_file(path, contents):
    _no_links(path)
    temporary = path.parent / ("." + path.name + "-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _sync_directory(path.parent)


def _switch(target, release):
    temporary = target.parent / (".current-" + uuid.uuid4().hex)
    temporary.symlink_to(release)
    os.replace(temporary, target)
    _sync_directory(target.parent)


def _as_collector(script, *arguments):
    return ["runuser", "-u", "atlas-collector", "--", "/usr/bin/python3", "-I", str(script), *map(str, arguments)]


def _key_file(value):
    path = Path(value)
    _no_links(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1):
            raise ValueError("credential_file_must_be_private_regular_unlinked")
        raw = stream.read(65537)
    if not raw or len(raw) > 65536:
        raise ValueError("credential_file_size_invalid")
    sys.path.insert(0, str(SOURCE))
    from atlas.collection.primitives import _json
    key = _json(raw)
    if type(key) is not dict or set(key) != {"api_key", "api_secret"}:
        raise ValueError("credential_json_fields_invalid")
    if any(type(v) is not str or not 1 <= len(v) <= 1024 or not v.isascii()
           or any(ord(c) < 33 or ord(c) > 126 for c in v) for v in key.values()):
        raise ValueError("credential_json_values_invalid")
    return key


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alpaca-key-file", help="existing private JSON with api_key/api_secret; copied outside backups")
    parser.add_argument("--coinbase-only", action="store_true", help="explicit partial deployment without stock collection")
    parser.add_argument("--check", action="store_true", help="validate packaging only; change no host state")
    parser.add_argument("--offsite-bucket", help="explicit GCS bucket; enable the separate uploader after local backup success")
    args = parser.parse_args(argv)
    if args.coinbase_only and args.alpaca_key_file:
        parser.error("choose --alpaca-key-file or --coinbase-only")
    if args.offsite_bucket is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", args.offsite_bucket):
        parser.error("offsite bucket must be an explicit 3..63 character lowercase DNS-style name")
    manifest = release_manifest()
    for name in (*UNITS, "atlas-collector-journald.conf", "atlas-collector-backup-offsite.conf"):
        path = SOURCE / "deploy" / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError("collector_unit_source_invalid")
    if args.check:
        print(json.dumps({"release_files": len(manifest), "third_party_python_dependencies": 0,
                          "live_trading": False, "host_changed": False}))
        return 0
    _host_requirements()
    sys.path.insert(0, str(SOURCE))
    from atlas.collection.config import check_environment
    check_environment()
    for path in (CONFIG_DIR, STATE_DIR, BACKUP_DIR, BASE, SYSTEMD_DIR, JOURNALD_DIR):
        _no_links(path)
    cfg_path = CONFIG_DIR / "config.json"
    _no_links(cfg_path)
    fresh = not cfg_path.exists()
    key = None
    target = BASE / "current"
    old_release = None
    if target.is_symlink():
        old_release = target.resolve(strict=True)
        if not old_release.is_relative_to(BASE / "releases"):
            raise ValueError("current_release_must_be_managed")
        _release(old_release)
    elif target.exists():
        raise ValueError("current_release_must_be_managed_symlink")
    if fresh:
        if old_release is not None or STATE_DIR.exists():
            raise ValueError("partial_installation_retained_requires_explicit_recovery")
        if not args.coinbase_only:
            if not args.alpaca_key_file:
                parser.error("first installation requires --alpaca-key-file, or explicitly --coinbase-only")
            key = _key_file(args.alpaca_key_file)
    else:
        if old_release is None or not STATE_DIR.is_dir():
            raise ValueError("partial_installation_retained_requires_explicit_recovery")
        if args.alpaca_key_file or args.coinbase_only:
            parser.error("update preserves configuration; credential/config changes require explicit maintenance")
    if fresh:
        _run(["apt-get", "update"])
        _run(["apt-get", "install", "-y", "python3", "ca-certificates", "tzdata"])
    try:
        account = pwd.getpwnam("atlas-collector")
    except KeyError:
        _run(["useradd", "--system", "--user-group", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "atlas-collector"])
        account = pwd.getpwnam("atlas-collector")
    if account.pw_uid == 0 or account.pw_shell not in {"/usr/sbin/nologin", "/sbin/nologin"}:
        raise ValueError("collector_account_must_be_nonroot_nologin")
    for path in (CONFIG_DIR, BACKUP_DIR):
        _directory(path, account.pw_uid, account.pw_gid, 0o700)
    _directory(BASE, os.geteuid(), os.getegid(), 0o755)
    releases = BASE / "releases"
    _directory(releases, os.geteuid(), os.getegid(), 0o755)
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
    release = releases / digest
    if not release.exists():
        stage_release(release)
    _release(release, manifest)
    _run(_as_collector(release / "scripts/atlas_collect.py", "--help"))
    old_units = {}
    for name in UNITS:
        destination = SYSTEMD_DIR / name
        _no_links(destination)
        old_units[name] = destination.read_bytes() if destination.exists() else None
    offsite_env = CONFIG_DIR / "offsite.env"
    offsite_dropin = SYSTEMD_DIR / OFFSITE_DROPIN
    old_offsite = {}
    if args.offsite_bucket is not None:
        for path in (offsite_env, offsite_dropin):
            _no_links(path)
            old_offsite[path] = path.read_bytes() if path.exists() else None
    journal_path = JOURNALD_DIR / "atlas-collector.conf"
    _no_links(journal_path)
    old_journal = journal_path.read_bytes() if journal_path.exists() else None
    if not fresh:
        # The old, verified runtime snapshots its own state while collection is
        # still running. Any failed backup leaves the active release untouched.
        _run(_as_collector(old_release / "scripts/atlas_backup.py", "create",
                          "--state-dir", STATE_DIR, "--backup-dir", BACKUP_DIR, "--retention", "7", "--wait-seconds", "90"))
    if fresh:
        _directory(STATE_DIR, account.pw_uid, account.pw_gid, 0o700)
        if key is not None:
            _private_json(CONFIG_DIR / "alpaca-data.json", key, account.pw_uid, account.pw_gid)
        started = int(datetime.now(timezone.utc).timestamp()) // 86400 * 86400
        generation = "collection-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8]
        config = {"version": 1, "mode": "data_collection", "live_trading": False,
                  "state_dir": str(STATE_DIR), "archive_dir": str(STATE_DIR / "archives" / generation),
                  "generation_id": generation, "research_start": started,
                  "providers": ["coinbase"] if args.coinbase_only else ["alpaca", "coinbase"],
                  "alpaca_key_file": None if args.coinbase_only else str(CONFIG_DIR / "alpaca-data.json")}
        _private_json(cfg_path, config, account.pw_uid, account.pw_gid)
        # Only the dedicated service user creates operational files. The parent
        # /var/lib remains root owned and no existing state is recursively chowned.
        _run(_as_collector(release / "scripts/atlas_collect.py", "init", "--config", cfg_path))
    _run(_as_collector(release / "scripts/atlas_health.py", "--help"))
    _run(_as_collector(release / "scripts/atlas_backup.py", "--help"))
    _run(_as_collector(release / "scripts/atlas_offsite_backup.py", "--help"))
    stopped = False
    try:
        if not fresh:
            _run(["systemctl", "stop", "atlas-collector.service"])
            stopped = True
        _switch(target, release)
        for name in UNITS:
            _replace_file(SYSTEMD_DIR / name, (SOURCE / "deploy" / name).read_bytes())
        if args.offsite_bucket is not None:
            _directory(offsite_dropin.parent, os.geteuid(), os.getegid(), 0o755)
            _replace_file(offsite_env, ("ATLAS_OFFSITE_BUCKET=" + args.offsite_bucket + "\n").encode())
            _replace_file(offsite_dropin, (SOURCE / "deploy/atlas-collector-backup-offsite.conf").read_bytes())
        _run(["systemd-analyze", "verify", *[str(SYSTEMD_DIR / name) for name in UNITS]])
        JOURNALD_DIR.mkdir(mode=0o755, exist_ok=True)
        _replace_file(journal_path, (SOURCE / "deploy/atlas-collector-journald.conf").read_bytes())
        _run(["systemctl", "restart", "systemd-journald"])
        _run(["systemctl", "daemon-reload"])
        _run(["systemctl", "enable", "--now", "atlas-collector.service", "atlas-collector-backup.timer"])
        _run(["systemctl", "is-active", "atlas-collector.service"])
    except (ValueError, OSError, subprocess.CalledProcessError):
        # Roll back code and unit files; never restore/overwrite research state.
        # A first-install failure retains its state/config for explicit recovery.
        for name, contents in old_units.items():
            path = SYSTEMD_DIR / name
            if contents is not None:
                _replace_file(path, contents)
            elif path.exists():
                path.unlink()
        for path, contents in old_offsite.items():
            if contents is not None:
                _replace_file(path, contents)
            elif path.exists():
                path.unlink()
        if old_journal is not None:
            _replace_file(journal_path, old_journal)
        elif journal_path.exists():
            journal_path.unlink()
        if old_release is not None:
            _switch(target, old_release)
            _run(["systemctl", "daemon-reload"])
            if stopped:
                _run(["systemctl", "restart", "atlas-collector.service"])
        else:
            _run(["systemctl", "stop", "atlas-collector.service", "atlas-collector-backup.timer"])
        raise
    print(json.dumps({"installed": True, "release": digest, "live_trading": False,
                      "continuous_operation_proven": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError):
        print("collector installation refused; inspect local systemd status; secrets are not logged", file=sys.stderr)
        raise SystemExit(1) from None
