"""Append-only experiment ledger.

Every training run - kept or rejected - appends one JSON line. Nothing is
ever rewritten, so the ledger is the honest record of how many variants were
attempted against a dataset (the denominator backtest-overfitting math needs).
Each entry pins full provenance: dataset content hash, source commit, feature
code hash, dependency lock hash, complete parameters, fold time ranges and
payout source.
"""

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
LEDGER_PATH = PROJECT_DIR / "experiments.jsonl"
LOCK_PATH = PROJECT_DIR / "uv.lock"


def _sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _source_commit() -> str | None:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=PROJECT_DIR,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except Exception:
        return None


def record_experiment(
    *,
    campaign: str,
    dataset_content_sha256: str | None,
    parameters: dict,
    fold_ranges: list[dict],
    payout_source: str,
    outcome: str,
    ledger_path: Path = LEDGER_PATH,
) -> dict:
    """Append one experiment entry and return it (with its 1-based id).

    `campaign` is the persistent research-campaign / strategy-family id
    (e.g. 'eurusd-logreg'). Search-overfitting exposure is counted per
    campaign, because the dataset hash changes with every collection while
    the family of model choices being tried does not."""
    entry = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "campaign": campaign,
        "dataset_content_sha256": dataset_content_sha256,
        "source_commit": _source_commit(),
        "feature_code_hash": _sha256_file(PROJECT_DIR / "features.py"),
        "train_code_hash": _sha256_file(PROJECT_DIR / "train.py"),
        "lock_hash": _sha256_file(LOCK_PATH),
        "lock_file": LOCK_PATH.name,
        "parameters": parameters,
        "fold_ranges": fold_ranges,
        "payout_source": payout_source,
        "outcome": outcome,
    }
    with open(ledger_path, "a") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    entry["id"] = sum(1 for _ in open(ledger_path))
    return entry


def count_variants(campaign: str, ledger_path: Path = LEDGER_PATH) -> int:
    """How many experiments this research campaign has run, cumulatively.

    Counted by campaign, NOT by dataset hash: every collection changes the
    hash, so a per-hash count would reset to 1 and hide the true number of
    model choices tried against overlapping, evolving market data. The
    dataset hash is still recorded on every individual entry."""
    if not ledger_path.exists():
        return 0
    count = 0
    for line in ledger_path.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        # Entries recorded before the campaign field existed (ids 1-3, all
        # spot-EURUSD logreg experiments) must keep counting - the ledger is
        # append-only, so they are attributed here instead of rewritten.
        entry_campaign = entry.get("campaign") or "eurusd-logreg"
        if entry_campaign == campaign:
            count += 1
    return count
