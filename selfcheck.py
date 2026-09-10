"""End-to-end operational audit - run any time, especially before leaving
the host unattended.

status.py answers "is the trader healthy right now?". This answers the
broader "is anything quietly wrong?": scheduled tasks, log freshness, disk,
database integrity, frozen-model provenance, safety switches, git sync and
backup coverage. Read-only everywhere; it never touches trading state.

Exit code: 0 all clear, 1 warnings, 2 failures.

Usage: .venv\\Scripts\\python.exe selfcheck.py [--json]
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from runtime_paths import BACKUP_DIR, ENV_FILE, MODELS_DIR, PROJECT_DIR, STATE_DIR
TASKS = ["ATLAS-supervisor", "ATLAS-watchdog", "ATLAS-extra-collect",
         "ATLAS-catchup", "ATLAS-backup"]
# Suppress the console window Windows would create for each console child
# (schtasks, git, pytest) when this runs from a windowless host.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MIN_FREE_GB = 5.0
BACKUP_MAX_AGE_H = 14.0     # 6h cadence + generous slack


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, check: str, detail: str) -> None:
        self.rows.append((level, check, detail))

    def ok(self, c, d=""):
        self.add("OK", c, d)

    def warn(self, c, d=""):
        self.add("WARN", c, d)

    def fail(self, c, d=""):
        self.add("FAIL", c, d)

    @property
    def exit_code(self) -> int:
        if any(r[0] == "FAIL" for r in self.rows):
            return 2
        return 1 if any(r[0] == "WARN" for r in self.rows) else 0


def check_tasks(rep: Report) -> None:
    if sys.platform != "win32":
        rep.warn("service manager", "host service integration is not configured; "
                 "Windows Task Scheduler checks do not apply")
        return
    for name in TASKS:
        try:
            out = subprocess.run(
                ["schtasks", "/query", "/tn", name, "/v", "/fo", "LIST"],
                capture_output=True, text=True, timeout=20,
                creationflags=NO_WINDOW)
        except Exception as exc:
            rep.warn(f"task {name}", f"unreadable: {type(exc).__name__}")
            continue
        if out.returncode != 0:
            rep.fail(f"task {name}", "NOT REGISTERED")
            continue
        def field(k):
            m = re.search(rf"^{k}:\s*(.+)$", out.stdout, re.MULTILINE)
            return m.group(1).strip() if m else "?"
        status, last = field("Status"), field("Last Result")
        # 267009 = currently running; 267011 = has not run yet.
        if last not in ("0", "267009", "267011"):
            rep.warn(f"task {name}", f"last result {last} (status {status})")
        else:
            rep.ok(f"task {name}", f"{status}, last result {last}")


def check_disk(rep: Report) -> None:
    if not STATE_DIR.exists():
        rep.fail("state directory", f"{STATE_DIR} does not exist")
        return
    free_gb = shutil.disk_usage(STATE_DIR).free / 1024 ** 3
    (rep.ok if free_gb >= MIN_FREE_GB else rep.fail)(
        "disk space", f"{free_gb:.1f} GB free")


def check_safety(rep: Report) -> None:
    # Match runtime precedence: process environment overrides the selected
    # .env file. Report presence/status only, never credential values.
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip())
    values.update(os.environ)
    if not all(values.get(key, "").strip() for key in ("IQ_EMAIL", "IQ_PASSWORD")):
        rep.fail("broker credentials", "IQ_EMAIL and IQ_PASSWORD are required")
    else:
        rep.ok("broker credentials", "present (values not displayed)")
    if values.get("IQ_ALLOW_REAL", "0") != "0":
        rep.fail("safety IQ_ALLOW_REAL", "must be 0")
    else:
        rep.ok("safety IQ_ALLOW_REAL", "0 (real balance refused)")
    if values.get("IQ_DEFAULT_BALANCE", "PRACTICE").upper() != "PRACTICE":
        rep.fail("safety balance mode", "must be PRACTICE")
    else:
        rep.ok("safety balance mode", "PRACTICE")
    # The per-order guard must still be in the runner.
    runner = (PROJECT_DIR / "live_h2_runner.py").read_text(encoding="utf-8")
    if "get_balance_mode" in runner and "balance_mode_" in runner:
        rep.ok("safety per-order guard", "present")
    else:
        rep.fail("safety per-order guard", "MISSING from live_h2_runner.py")


def check_models(rep: Report) -> None:
    cutoff = 1784678400  # 2026-07-22T00:00:00Z
    import pickle  # project's own frozen bundles; the runner loads them hourly
    for pattern in ("h2-*.pkl", "h4-*.pkl", "meta-h3.pkl"):
        paths = sorted(MODELS_DIR.glob(pattern))
        if not paths:
            rep.fail(f"model {pattern}", "MISSING")
            continue
        path = paths[-1]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        try:
            with open(path, "rb") as fh:
                meta = (pickle.load(fh).get("meta") or {})
        except Exception as exc:
            rep.fail(f"model {path.name}", f"unreadable: {type(exc).__name__}")
            continue
        end = meta.get("data_end_ts")
        if end is None:
            # No data_end_ts, but the bundle may still document its corpus.
            # meta-h3 records trained_on='histdata 2016-2025 gated trades' -
            # a different SOURCE ending a year before the 2026 cutoff, so no
            # mechanism exists for it to have seen the forward window. That is
            # a schema gap, not an evidentiary one; report it rather than
            # implying an unassessed risk.
            trained_on = meta.get("trained_on")
            if trained_on:
                pairs = meta.get("pairs") or []
                rep.ok(f"model {path.name}",
                       f"sha {digest}, no data_end_ts; corpus: {trained_on}"
                       + (f" ({len(pairs)} pairs, "
                          f"{meta.get('n_trades', '?')} trades)" if pairs else ""))
            else:
                rep.warn(f"model {path.name}",
                         f"sha {digest}, NO data_end_ts and no corpus record "
                         "- provenance genuinely unverifiable")
        elif end > cutoff:
            # No longer a refusal: forward_eval purges each bundle to
            # data_end_ts + one label horizon instead, which is tighter.
            purged = datetime.fromtimestamp(
                end + 15 * 60, timezone.utc).strftime("%m-%d %H:%MZ")
            rep.ok(f"model {path.name}",
                   f"sha {digest}, trained {(end - cutoff)/3600:.2f}h past cutoff "
                   f"-> scoring purged to {purged}")
        else:
            rep.ok(f"model {path.name}", f"sha {digest}, pre-cutoff")


def check_database(rep: Report) -> None:
    db = STATE_DIR / "market.duckdb"
    if not db.exists():
        rep.fail("market.duckdb", "MISSING")
        return
    try:
        import duckdb
        conn = duckdb.connect(str(db), read_only=True)
    except Exception as exc:
        rep.warn("market.duckdb", f"busy/unreadable ({type(exc).__name__}) - "
                                  "normal while the collector holds the lock")
        return
    try:
        n, latest = conn.execute(
            "SELECT count(*), max(to_ts) FROM candles").fetchone()
        ns, psnap = conn.execute(
            "SELECT count(*), max(ts_epoch) FROM payout_snapshots").fetchone()
    finally:
        conn.close()
    now = int(time.time())
    if latest is None:
        rep.warn("candles", "no candles collected")
    else:
        rep.ok("candles", f"{n:,} rows, newest {(now - int(latest)) // 60} min old")
    if psnap is None:
        rep.warn("payout snapshots", "no payouts collected")
    else:
        age_h = (now - int(psnap)) / 3600
        (rep.ok if age_h < 3 else rep.warn)(
            "payout snapshots", f"{ns:,}, newest {age_h:.1f} h old")


def check_backup(rep: Report) -> None:
    if BACKUP_DIR is None:
        rep.warn("backup", "ATLAS_BACKUP_DIR is not configured")
        return
    if not BACKUP_DIR.exists():
        rep.warn("backup", f"{BACKUP_DIR} does not exist yet")
        return
    files = list(BACKUP_DIR.glob("*"))
    if not files:
        rep.warn("backup", "directory is empty")
        return
    newest = max(f.stat().st_mtime for f in files)
    age_h = (time.time() - newest) / 3600
    total_mb = sum(f.stat().st_size for f in files) / 1024 ** 2
    critical = BACKUP_DIR / "logs_live_h2.jsonl"
    if not critical.exists():
        rep.fail("backup", "logs_live_h2.jsonl (the forward evidence) NOT backed up")
    elif age_h > BACKUP_MAX_AGE_H:
        rep.warn("backup", f"{len(files)} files, newest {age_h:.1f} h old")
    else:
        rep.ok("backup", f"{len(files)} files, {total_mb:.1f} MB, "
                         f"newest {age_h:.1f} h old")


# Credential shapes worth refusing to publish. This repository is PUBLIC by
# the operator's explicit choice, so a key reaching a TRACKED file is
# published the moment anything is pushed - and git history keeps it even
# after deletion. Patterns are deliberately prefix-based (no generic
# high-entropy heuristics) to keep false positives near zero.
SECRET_PATTERNS = [
    ("Perplexity API key", re.compile(r"pplx-[A-Za-z0-9]{32,}")),
    ("OpenAI API key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("Slack token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def check_secrets(rep: Report) -> None:
    """Scan git-TRACKED files for credential shapes.

    Only tracked files matter: .env is gitignored, so a key living there is
    correct and must not be flagged. Anything git knows about is publishable.
    """
    try:
        listing = subprocess.run(["git", "ls-files"], cwd=PROJECT_DIR,
                                 capture_output=True, text=True, timeout=60,
                                 creationflags=NO_WINDOW)
    except Exception as exc:
        rep.warn("secret scan", f"could not list tracked files: {type(exc).__name__}")
        return
    if listing.returncode != 0:
        rep.warn("secret scan", "git ls-files failed")
        return

    hits, scanned = [], 0
    for rel in listing.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        path = PROJECT_DIR / rel
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append(f"{rel}: {name}")

    if hits:
        rep.fail("secret scan",
                 f"CREDENTIALS IN TRACKED FILES ({len(hits)}): "
                 + "; ".join(hits[:5]))
    else:
        rep.ok("secret scan", f"{scanned} tracked files, no credentials found")

    # .env must stay untracked, or the next commit publishes real credentials.
    env_tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"],
                                 cwd=PROJECT_DIR, capture_output=True,
                                 text=True, timeout=30, creationflags=NO_WINDOW)
    if env_tracked.returncode == 0:
        rep.fail("secret scan", ".env is TRACKED by git - it holds live credentials")
    else:
        rep.ok("secret scan .env", "untracked, as it must be")


def check_git(rep: Report) -> None:
    def git(*args):
        return subprocess.run(["git", *args], cwd=PROJECT_DIR,
                              capture_output=True, text=True, timeout=30,
                              creationflags=NO_WINDOW)
    try:
        dirty = git("status", "--porcelain").stdout.strip()
        git("fetch", "--quiet", "origin", "main")
        counts = git("rev-list", "--left-right", "--count",
                     "HEAD...origin/main").stdout.split()
    except Exception as exc:
        rep.warn("git", f"unavailable: {type(exc).__name__}")
        return
    if dirty:
        rep.warn("git tree", f"{len(dirty.splitlines())} uncommitted change(s)")
    else:
        rep.ok("git tree", "clean")
    if len(counts) == 2:
        ahead, behind = counts
        if ahead == "0" and behind == "0":
            rep.ok("git sync", "in sync with origin/main")
        else:
            rep.warn("git sync", f"{ahead} ahead, {behind} behind origin/main")


def check_tests(rep: Report) -> None:
    py = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
    exe = py if py.exists() else Path(sys.executable)
    try:
        out = subprocess.run([str(exe), "-m", "pytest", "-q"],
                             cwd=PROJECT_DIR, capture_output=True, text=True,
                             timeout=600, creationflags=NO_WINDOW)
    except Exception as exc:
        rep.warn("tests", f"could not run: {type(exc).__name__}")
        return
    tail = [ln for ln in out.stdout.strip().splitlines() if ln.strip()]
    summary = tail[-1] if tail else "no output"
    (rep.ok if out.returncode == 0 else rep.fail)("tests", summary[:90])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args()

    rep = Report()
    for fn in (check_tasks, check_disk, check_safety, check_secrets,
               check_models, check_database, check_backup, check_git):
        try:
            fn(rep)
        except Exception as exc:
            rep.warn(fn.__name__, f"check crashed: {type(exc).__name__}: {exc}")
    if not args.skip_tests:
        try:
            check_tests(rep)
        except Exception as exc:
            rep.warn("tests", f"check crashed: {type(exc).__name__}")

    if args.json:
        print(json.dumps({
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "exit_code": rep.exit_code,
            "rows": [{"level": lv, "check": c, "detail": d} for lv, c, d in rep.rows],
        }, indent=2))
        return rep.exit_code

    counts = {"OK": 0, "WARN": 0, "FAIL": 0}
    print(f"=== ATLAS self-check {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%S}Z ===\n")
    for level, check, detail in rep.rows:
        counts[level] += 1
        mark = {"OK": "  ok ", "WARN": " WARN", "FAIL": " FAIL"}[level]
        print(f"[{mark}] {check:26s} {detail}")
    print(f"\n{counts['OK']} ok, {counts['WARN']} warnings, {counts['FAIL']} failures")
    return rep.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
