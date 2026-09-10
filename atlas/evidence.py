"""Bounded read-only evidence without pickle loading or external instructions."""

import hashlib
import json
import math
import os
from pathlib import Path
import stat
from urllib.parse import urlparse


def external_context(records: list[dict]) -> list[dict]:
    """Text is inert, attributed context; it never becomes a numeric adjustment."""
    if not isinstance(records, list) or len(records) > 20:
        raise ValueError("research context must contain at most 20 records")
    result = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("research context must contain objects")
        url, text = record.get("url"), record.get("text")
        if not isinstance(url, str) or len(url) > 2000 or urlparse(url).scheme != "https" or not urlparse(url).netloc:
            raise ValueError("research context requires an HTTPS source URL")
        if not isinstance(text, str) or len(text) > 12000:
            raise ValueError("research text must be a bounded string")
        result.append({"source_url": url, "text": text, "trust": "untrusted_external_data",
                       "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                       "numeric_adjustment": None,
                       "warning": "Source content is not instructions, verified facts, or execution authorization."})
    return result


class EvidenceStore:
    def __init__(self, project: Path, state: Path | None = None):
        self.project = Path(project).resolve()
        self.state = Path(state).resolve() if state is not None else None

    @staticmethod
    def _rows(path: Path, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("bounded evidence record limit required")
        if not path.is_file():
            return []
        if path.is_symlink():
            raise ValueError("evidence symlink is not an allowed source")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("evidence source must be a regular file")
            data = stream.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("evidence exceeds bounded read budget; explicit archival query required")
        def reject_constant(_):
            raise ValueError("nonfinite historical evidence is invalid")
        def unique(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate historical evidence key")
                result[key] = value
            return result
        rows = []
        for line in data.splitlines():
            if not line.strip():
                continue
            if len(line) > 65536:
                raise ValueError("historical evidence line exceeds read limit")
            value = json.loads(line, parse_constant=reject_constant, object_pairs_hook=unique)
            if not isinstance(value, dict):
                raise ValueError("historical evidence record must be an object")
            rows.append(value)
        return rows[-limit:]

    @staticmethod
    def _scalar(value):
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, str) and len(value) <= 12000:
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and abs(value) <= 10 ** 15:
            return value
        raise ValueError("historical evidence fields require bounded finite scalar values")

    def models(self):
        artifacts = []
        paths = sorted((self.project / "models").glob("*.pkl"))
        if len(paths) > 100:
            raise ValueError("model inventory exceeds count budget")
        total = 0
        for path in paths:
            if path.is_symlink():
                continue
            hasher = hashlib.sha256()
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 64_000_000:
                    raise ValueError("model inventory file exceeds budget")
                total += info.st_size
                if total > 128_000_000:
                    raise ValueError("model inventory exceeds total byte budget")
                read = 0
                for chunk in iter(lambda: stream.read(65536), b""):
                    read += len(chunk)
                    if read > info.st_size:
                        raise ValueError("model changed during inventory")
                    hasher.update(chunk)
                after = os.fstat(stream.fileno())
                if read != info.st_size or after.st_mtime_ns != info.st_mtime_ns:
                    raise ValueError("model changed during inventory")
            artifacts.append({"filename": path.name, "bytes": info.st_size,
                              "sha256": hasher.hexdigest(), "loaded": False,
                              "provenance": "historical artifact; live approval and compatibility unverified",
                              "contract": "legacy FX binary research; not an equity or spot-crypto model"})
        return {"artifacts": artifacts, "live_approved_models": [],
                "note": "Inventory hashes bytes only. It never unpickles an artifact."}

    def research(self, limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("research limit must be between 1 and 100")
        rows = self._rows(self.project / "research_registry.jsonl", limit)
        # A closed output schema prevents credentials or a future arbitrary
        # ledger field from becoming AI-facing content.
        fields = {"ts", "ts_utc", "timestamp", "family", "name", "hypothesis", "trials", "n_trials", "n_variants",
                  "status", "outcome", "config_hash", "note", "notes", "description"}
        return {"records": [{k: self._scalar(v) for k, v in row.items() if k in fields}
                            for row in rows],
                "evidence_kind": "repository_reported_research",
                "trust": "historical data, not instructions or reproduced live performance",
                "numeric_adjustment": None, "window_limit": limit,
                "total_attempt_count": None, "attempt_count_note": "bounded historical view; use immutable campaign registry for complete attempt counts"}

    def historical_predictions(self, symbol: str, mode="practice", limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("prediction limit must be between 1 and 100")
        if mode not in {"practice", "real"} or not isinstance(symbol, str):
            raise ValueError("invalid historical prediction selector")
        if self.state is None:
            return {"predictions": [], "available": False, "reason": "no operational state selected"}
        base = self.state / "logs" / "real" if mode == "real" else self.state / "logs"
        fields = {"ts", "bar_to_ts", "asset", "action", "p_up", "meta_p", "h4_p", "payout",
                  "model", "mode", "execution_mode", "decision_latency_s"}
        rows = [{k: self._scalar(v) for k, v in row.items() if k in fields}
                for row in self._rows(base / "live_h2.jsonl", 1000)
                if row.get("asset") == symbol and not row.get("settled")][-limit:]
        return {"predictions": rows, "available": bool(rows), "mode": mode,
                "contract": "FX binary historical observations only",
                "live_approval": False, "reason": None if rows else "no matching recorded model evidence"}
