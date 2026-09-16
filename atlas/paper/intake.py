"""Separate, source-bound paper observations and persisted strategy candidates.

The owned runtime acquires only pinned Alpaca GETs. Test injection has a distinct
registration and can never produce paper-eligible evidence. No broker order,
engine activation, researcher archive, holdout read or qualification is exposed.
Local archive integrity and an owned HTTPS path are not provider attestation.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import time

from atlas.domain import financial_context
from atlas.ledger import canonical_json, digest
from atlas.paper.journal import _directory, _safe_file, _sync
from atlas.paper.models import PaperError, amount, decision, sha256, timestamp
from atlas.paper.risk import PaperRiskPolicy
from atlas.paper.strategy import strategy_identity, SOURCE_FILES as STRATEGY_SOURCES
from atlas.research import data_archive as archive, data_collector as collector
from atlas.research.data_contract_v1 import default_contract
from atlas.research.data_coverage import _calendar
from atlas.research.multiasset_strategies import signals_for, StrategyError

VERSION = "paper-owned-intake-v1"
MAX_INPUT_BARS = 20_000
MAX_RECEIPTS = 10_000
SOURCE_FILES = tuple(sorted(set(STRATEGY_SOURCES) | {
    "atlas/paper/intake.py", "atlas/paper/journal.py", "atlas/paper/risk.py",
    "atlas/paper/intake_cycle.py", "atlas/paper/engine.py", "atlas/collection/archive.py", "scripts/atlas_collect.py",
    "atlas/paper/cli.py", "atlas/paper/broker.py", "atlas/alpaca_paper_preflight.py",
    "atlas/research/data_archive.py", "atlas/research/data_contract_v1.py",
    "atlas/research/data_collector.py", "atlas/research/data_coverage.py",
    "atlas/collection/primitives.py", "uv.lock",
}))
_ROOT_FILES = {"registration.json", ".intake.lock", "observations", "receipts"}


def _identity(spec):
    strategy = strategy_identity(spec)
    project = Path(__file__).resolve().parents[2]
    sources = {name: hashlib.sha256((project / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    implementation = {"version": VERSION, "strategy": strategy, "sources": sources,
                      "python": platform.python_version(), "usable_time": "max(receipt,ingestion,calendar_visibility)"}
    return {**implementation, "strategy_sha256": digest(implementation)}


def _sequence_digest(rows):
    """Hash a bounded sequence without the journal's one-object JSON size limit."""
    value = hashlib.sha256()
    for row in rows:
        value.update(bytes.fromhex(digest(row)))
    return value.hexdigest()


def _new_json(path, value):
    raw = canonical_json(value).encode()
    if len(raw) > 262144:
        raise PaperError("paper_intake_metadata_bound")
    fd = _safe_file(path, create=True)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _sync(path.parent)
    except BaseException:
        # A partial write remains for inspection; never erase or replace it.
        raise


def _read_json(path):
    archive._trusted(path)
    if path.stat().st_size > 262144:
        raise PaperError("paper_intake_metadata_bound")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(262145)
    try:
        value = json.loads(raw)
        if canonical_json(value).encode() != raw:
            raise ValueError
        return value
    except (ValueError, TypeError, RecursionError):
        raise PaperError("paper_intake_metadata_invalid") from None


class PaperIntake:
    """Open an explicitly registered paper stream; construction never collects.

    ``register`` creates a new stream with the current system clock. ``testing``
    is the only injection interface and requires/creates a TEST registration.
    Neither interface can open an existing research archive as an intake stream.
    """

    def __init__(self, root):
        self._root = _directory(root, False)
        self._clock, self._transport, self._testing = time.time, None, False
        with self._locked():
            self._registration()

    @classmethod
    def register(cls, root, *, generation_id, spec, account_fingerprint, risk_policy):
        return cls._register(root, generation_id, spec, account_fingerprint, risk_policy)

    @classmethod
    def testing(cls, root, *, clock, transport, generation_id=None, spec=None,
                account_fingerprint=None, risk_policy=None):
        if not callable(clock) or not callable(transport):
            raise PaperError("paper_intake_test_callbacks_required")
        if generation_id is not None:
            return cls._register(root, generation_id, spec, account_fingerprint, risk_policy,
                                 clock=clock, transport=transport)
        instance = object.__new__(cls)
        instance._root = _directory(root, False)
        instance._clock, instance._transport, instance._testing = clock, transport, True
        with instance._locked():
            instance._registration()
        return instance

    @classmethod
    def _register(cls, root, generation_id, spec, account_fingerprint, risk_policy,
                  *, clock=None, transport=None):
        if type(risk_policy) is not PaperRiskPolicy:
            raise PaperError("paper_intake_risk_policy_required")
        identity = _identity(spec)
        if identity["strategy"]["strategy_id"] not in risk_policy.allowed_strategy_ids:
            raise PaperError("paper_intake_strategy_not_allowed")
        if not set(identity["strategy"]["spec"]["symbols"]) <= set(risk_policy.allowed_symbols):
            raise PaperError("paper_intake_symbols_not_allowed")
        created = archive.receipt_time(clock)
        contract = default_contract(generation_id, created - created % 300, created)
        role = "TEST" if clock is not None or transport is not None else "PAPER_OBSERVATION"
        registration = {"version": VERSION, "role": role, "created_at": created,
                        "contract": contract, "implementation": identity,
                        "account_fingerprint": sha256(account_fingerprint),
                        "risk_policy": risk_policy.public(), "policy_sha256": risk_policy.fingerprint()}
        registration["registration_sha256"] = digest(registration)
        root = _directory(root, True)
        try:
            fd = _safe_file(root / ".intake.lock", create=True)
            os.close(fd)
            _new_json(root / "registration.json", registration)
            (root / "receipts").mkdir(mode=0o700)
            # Registration and archive share one captured current clock, avoiding
            # a second-boundary mismatch without exposing backdating to callers.
            archive._create_archive(root / "observations", {"archive_version": 1, "contract": contract,
                "clock_basis": "injected_test_clock" if role == "TEST" else "system_utc"})
            _sync(root)
        except (OSError, ValueError):
            raise PaperError("paper_intake_initialization_failed_retained") from None
        if role == "TEST":
            return cls.testing(root, clock=clock, transport=transport)
        return cls(root)

    @contextmanager
    def _locked(self):
        _directory(self._root, False)
        if {p.name for p in self._root.iterdir()} != _ROOT_FILES:
            raise PaperError("paper_intake_layout_invalid")
        archive._trusted(self._root / "receipts", directory=True)
        fd = _safe_file(self._root / ".intake.lock")
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PaperError("paper_intake_busy") from None
            yield
        finally:
            os.close(fd)

    def _now(self):
        return timestamp(self._clock())

    def _registration(self):
        reg = _read_json(self._root / "registration.json")
        fields = {"version", "role", "created_at", "contract", "implementation", "account_fingerprint",
                  "risk_policy", "policy_sha256", "registration_sha256"}
        if type(reg) is not dict or set(reg) != fields:
            raise PaperError("paper_intake_registration_invalid")
        try:
            policy = PaperRiskPolicy(**reg["risk_policy"])
            expected_role = "TEST" if self._testing else "PAPER_OBSERVATION"
            if (reg["version"] != VERSION or reg["role"] != expected_role
                    or reg["registration_sha256"] != digest({k: v for k, v in reg.items() if k != "registration_sha256"})
                    or reg["implementation"] != _identity(reg["implementation"]["strategy"]["spec"])
                    or policy.fingerprint() != reg["policy_sha256"]
                    or reg["implementation"]["strategy"]["strategy_id"] not in policy.allowed_strategy_ids
                    or not set(reg["implementation"]["strategy"]["spec"]["symbols"]) <= set(policy.allowed_symbols)
                    or type(reg["created_at"]) is not int
                    or reg["contract"] != default_contract(reg["contract"]["generation_id"],
                        reg["created_at"] - reg["created_at"] % 300, reg["created_at"])):
                raise ValueError
            sha256(reg["account_fingerprint"])
        except (ValueError, TypeError, KeyError):
            raise PaperError("paper_intake_registration_or_source_changed") from None
        now = self._now()
        if math.ceil(now) < reg["created_at"]:
            raise PaperError("paper_intake_clock_rewind")
        # The dedicated paper stream uses only its first open 90-day window.
        # Refuse before opening payload storage at any later window/embargo.
        if now >= reg["contract"]["windows"]["research"]["end_exclusive"]:
            raise PaperError("paper_intake_window_elapsed")
        return reg

    def registration(self):
        """Read the source/account/policy binding without a key or broker call."""
        with self._locked():
            return deepcopy(self._registration())

    def _scan(self, db, reg):
        # Inspect identity-only metadata before decoding any observation payload.
        headers = db.execute("SELECT id,body,hash FROM manifest").fetchall()
        if len(headers) != 1 or headers[0][0] != 1:
            raise PaperError("paper_intake_archive_binding_invalid")
        header = archive._decode(headers[0][1], max_bytes=100000)
        basis = "injected_test_clock" if self._testing else "system_utc"
        if (header != {"archive_version": 1, "contract": reg["contract"], "clock_basis": basis}
                or archive._sha(headers[0][1]) != headers[0][2]
                or db.execute("SELECT COUNT(*) FROM strategy_freeze").fetchone()[0] != 0):
            raise PaperError("paper_intake_archive_binding_invalid")
        state = archive._scan(db, math.ceil(self._now()))
        if (state["contract"] != reg["contract"] or state["manifest"]["clock_basis"] != basis
                or state["strategy_freeze"] is not None):
            raise PaperError("paper_intake_archive_binding_invalid")
        return state

    def acquire(self, *, key_file=None):
        """Acquire at most the latest completed 15 minutes, using GETs only.

        No arbitrary bars, date range, host or runtime transport can be supplied.
        Older outage data is not fetched as a fresh prospective observation.
        The archive retains safe completed captures even on provider failure.
        """
        with self._locked():
            reg = self._registration()
            if self._testing:
                if key_file is not None:
                    raise PaperError("paper_intake_test_credentials_refused")
            else:
                from atlas.paper.cli import _paper_key_path
                path = _paper_key_path(key_file)
                if path == self._root or path.is_relative_to(self._root):
                    raise PaperError("paper_intake_credentials_inside_state")
            now = self._now()
            end = math.floor(now / 300) * 300
            start = max(math.ceil(reg["created_at"] / 300) * 300, end - 900)
            if start >= end:
                return {"status": "WAIT_COMPLETE_BAR", "orders_submitted": 0}
            with archive._connection(self._root / "observations") as db:
                before = self._scan(db, reg)
            failure = None
            try:
                bundle = collector.collect_once(reg["contract"], provider="alpaca", start=start, end=end,
                    key_file=None if self._testing else str(path),
                    transport=self._transport if self._testing else None,
                    clock=self._clock if self._testing else None)
            except collector.DataCollectorError as exc:
                failure = exc.code
                bundle = {"records": [], "captures": exc.captures, "calendar_coverage": [],
                          "provenance": "collection_failed:" + exc.code}
            current = math.ceil(self._now())
            groups = {}
            for row in bundle["records"]:
                lag_limit = reg["contract"]["policies"]["max_bar_lag_seconds" if row["record_type"] == "bar" else "max_quote_age_seconds"]
                origin = "test" if self._testing else "historical_import" if current - row["event_time"] > lag_limit else "builtin_get"
                groups.setdefault(origin, []).append(row)
            if not groups and bundle["captures"]:
                groups["test" if self._testing else "builtin_get"] = []
            results = []
            for origin, rows in sorted(groups.items()):
                results.append(archive._append(self._root / "observations", records=rows,
                    captures=bundle["captures"], calendar_coverage=bundle["calendar_coverage"], origin=origin,
                    provenance=bundle["provenance"], clock=self._clock if self._testing else None))
            counts = (results[-1] if results else archive._summary(before))["acquisition_kinds"]
            classes = [{"test": "TEST", "historical_backfill": "HISTORICAL_UNVERIFIED",
                        "prospective_observation": "PROSPECTIVE_OBSERVATION_UNVERIFIED"}[kind]
                       for kind, count in counts.items() if count]
            return {"status": "ACQUIRED" if failure is None else "ACQUISITION_FAILED", "error": failure,
                    "start": start, "end": end, "batches": len(results),
                    "appended_records": sum(r["appended_records"] for r in results),
                    "captures_retained": sum(r["captures_retained"] for r in results),
                    "evidence_classes": sorted(classes), "lineage_counts": counts,
                    "lineage_scope": "archive_after_acquisition",
                    "provider_authenticity_verified": False, "orders_submitted": 0}

    def _persist(self, report, *, signal_id=None):
        folder = self._root / "receipts"
        entries = list(folder.iterdir())
        if any(not re.fullmatch(r"(?:signal|observation)-[a-f0-9]{64}\.json", p.name) for p in entries):
            raise PaperError("paper_intake_receipt_layout_invalid")
        identity = digest(signal_id) if signal_id is not None else digest(report)
        path = folder / (("signal-" if signal_id is not None else "observation-") + identity + ".json")
        replayed = path.exists()
        if replayed:
            old = _read_json(path)
            if signal_id is not None:
                invariants = {"version", "status", "reason", "registration_sha256", "account_fingerprint",
                    "policy_sha256", "strategy_sha256", "symbol", "evidence_class", "source_path",
                    "source_signal",
                    "archive_integrity_verified", "provider_authenticity_verified", "prospective_qualified",
                    "execution_authorized", "live_enabled", "paper_candidate_eligible"}
                if (type(old) is not dict or set(old) != set(report)
                        or any(old[key] != report[key] or type(old[key]) is not type(report[key]) for key in invariants)
                        or type(old.get("evidence")) is not dict or type(old.get("candidate")) is not dict
                        or old["candidate"].get("data_sha256") != digest(old["evidence"])
                        or old["evidence"].get("registration_sha256") != report["registration_sha256"]):
                    raise PaperError("paper_intake_receipt_binding_invalid")
                decision(old["candidate"])
                self._verify_saved_evidence(old, report)
                expected, current = old.get("candidate"), report.get("candidate")
                if (not isinstance(expected, dict) or not isinstance(current, dict)
                        or {k: v for k, v in expected.items() if k != "data_sha256"}
                        != {k: v for k, v in current.items() if k != "data_sha256"}):
                    raise PaperError("paper_intake_signal_terms_changed")
                report = old  # Preserve its original evidence head and expiry.
            elif old != report:
                raise PaperError("paper_intake_receipt_conflict")
        else:
            if len(entries) >= MAX_RECEIPTS:
                raise PaperError("paper_intake_receipt_capacity")
            _new_json(path, report)
        if signal_id is not None:
            candidate = report["candidate"]
            deadline = min(candidate["expires_at"], report["source_signal"]["metadata"]["feature_event_time"]
                           + candidate["expires_at"] - candidate["signal_time"])
            if self._now() >= deadline:
                raise PaperError("paper_intake_candidate_expired_during_persistence")
        return {**deepcopy(report), "receipt_sha256": digest(report), "receipt_name": path.name,
                "replayed": replayed}

    def _verify_saved_evidence(self, old, current):
        """Reconstruct the original bounded snapshot, including its archive head.

        A self-consistent replacement evidence hash alone cannot authenticate a
        replay. The original head must still exist in the verified source chain.
        """
        try:
            observed = timestamp(old["observed_at"])
            evaluated = timestamp(old["evaluated_at"])
            as_of = old["as_of"]
            reg = self._registration()
            if (type(as_of) is not int or as_of != math.floor(observed)
                    or not reg["created_at"] <= observed <= current["observed_at"]
                    or not observed <= evaluated <= current["evaluated_at"]):
                raise ValueError
            with archive._connection(self._root / "observations") as db:
                state = self._scan(db, reg)
                count, calendars, calendar_lineage, found = 0, [], {}, False
                for body, head in db.execute("SELECT body,hash FROM batches ORDER BY id"):
                    batch = archive._decode(body)
                    count += len(batch["observations"])
                    for item in batch["calendar_coverage"]:
                        row = {**item, "ingestion_time": batch["ingested_at"]}
                        if max(row["received_at"], row["ingestion_time"]) <= as_of:
                            origin = batch["origin"]
                            calendar_lineage[origin] = calendar_lineage.get(origin, 0) + 1
                            if origin == ("test" if self._testing else "builtin_get"):
                                calendars.append(row)
                    if head == old["archive_head_sha256"]:
                        found = True
                        break
                if not found:
                    raise ValueError
                rows = [row for row in state["records"][:count] if row["record_type"] == "bar"
                        and row["symbol"] == old["symbol"]
                        and max(row["available_time"], row["ingestion_time"]) <= as_of]
            lineage = {}
            for row in rows:
                lineage[row["acquisition_kind"]] = lineage.get(row["acquisition_kind"], 0) + 1
            if (type(old["bar_version_count"]) is not int or old["bar_version_count"] != len(rows)
                    or canonical_json(lineage) != canonical_json(old["lineage_counts"])
                    or canonical_json(calendar_lineage) != canonical_json(old["calendar_lineage_counts"])):
                raise ValueError
            merged, _ = _calendar(calendars, as_of)
            first = min(date.fromisoformat(row["session"]["session_date"]) for row in rows)
            last = max(date.fromisoformat(row["session"]["session_date"]) for row in rows)
            days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
            calendar = {"timezone": "America/New_York", "source_id": "alpaca-paper-calendar-v2",
                        "sessions": [{"session_date": day.isoformat(), "open_time": merged[day]["session"][0],
                                      "close_time": merged[day]["session"][1]}
                                     for day in days if merged[day]["session"] is not None]}
            bars = []
            for row in rows:
                day = date.fromisoformat(row["session"]["session_date"])
                usable = max(row["available_time"], row["ingestion_time"],
                             max(merged[d]["visible_at"] for d in days if d <= day))
                bars.append({key: row[key] for key in ("symbol", "asset_class", "venue", "source_id", "event_time", "revision", "interval_seconds")}
                    | {"information_available_time": usable, "ingestion_time": usable,
                       "session_date": day.isoformat(), "session": "regular"}
                    | {key: row["payload"][key] for key in ("open", "high", "low", "close", "volume", "currency", "volume_unit")})
            expected = {"source_records_sha256": _sequence_digest(rows), "source_record_count": len(rows),
                        "calendar_sha256": digest(calendar), "calendar_receipts_sha256": _sequence_digest(calendars),
                        "calendar_receipt_count": len(calendars), "projected_bars_sha256": _sequence_digest(bars),
                        "registration_sha256": reg["registration_sha256"]}
            if canonical_json(old["evidence"]) != canonical_json(expected):
                raise ValueError
            signals = signals_for(bars, reg["implementation"]["strategy"]["spec"], calendar=calendar)
            if not signals or signals[-1] != old["source_signal"]:
                raise ValueError
        except (ValueError, TypeError, KeyError, OverflowError):
            raise PaperError("paper_intake_receipt_evidence_invalid") from None

    @financial_context
    def project(self, *, symbol, quantity, limit_price, held_quantity,
                expected_registration_sha256, ttl_seconds=300):
        """Persist a bounded candidate or an explicit no-action observation.

        The caller supplies reviewed sizing and current holdings; the engine must
        independently reconcile holdings, quotes, account and risk before an order.
        A candidate is never an execution authorization or qualification result.
        """
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise PaperError("paper_intake_ttl_invalid")
        qty, price, held = amount(quantity, positive=True), amount(limit_price, positive=True), amount(held_quantity)
        with self._locked():
            reg = self._registration()
            if sha256(expected_registration_sha256) != reg["registration_sha256"]:
                raise PaperError("paper_intake_registration_mismatch")
            spec = reg["implementation"]["strategy"]["spec"]
            if symbol not in spec["symbols"]:
                raise PaperError("paper_intake_symbol_not_selected")
            now = self._now()
            as_of = math.floor(now)
            with archive._connection(self._root / "observations") as db:
                state = self._scan(db, reg)
                # Validation and extraction share the same SQLite read transaction.
                rows = [row for row in state["records"] if row["record_type"] == "bar" and row["symbol"] == symbol
                        and max(row["available_time"], row["ingestion_time"]) <= as_of]
                # The base archive calendar summary has no origin field. Retain
                # batch ancestry here instead of accepting an imported calendar
                # as owned runtime evidence. Owned duplicate captures remain
                # usable even if a stale quote is saved in a historical batch.
                calendars, calendar_lineage = [], {}
                for (body,) in db.execute("SELECT body FROM batches ORDER BY id"):
                    batch = archive._decode(body)
                    for item in batch["calendar_coverage"]:
                        row = {**item, "ingestion_time": batch["ingested_at"]}
                        if max(row["received_at"], row["ingestion_time"]) <= as_of:
                            origin = batch["origin"]
                            calendar_lineage[origin] = calendar_lineage.get(origin, 0) + 1
                            if origin == ("test" if self._testing else "builtin_get"):
                                calendars.append(row)
            report = {"version": VERSION, "status": "NO_ACTION", "reason": None,
                      "registration_sha256": reg["registration_sha256"], "account_fingerprint": reg["account_fingerprint"],
                      "policy_sha256": reg["policy_sha256"], "strategy_sha256": reg["implementation"]["strategy_sha256"],
                      "archive_head_sha256": state["head_sha256"], "observed_at": now, "as_of": as_of,
                      "symbol": symbol, "bar_version_count": len(rows), "lineage_counts": {},
                      "calendar_lineage_counts": calendar_lineage,
                      "evidence_class": "TEST" if self._testing else "PROSPECTIVE_OBSERVATION_UNVERIFIED",
                      "source_path": "injected_test" if self._testing else "owned_alpaca_https_get",
                      "archive_integrity_verified": True, "provider_authenticity_verified": False,
                      "prospective_qualified": False, "execution_authorized": False, "live_enabled": False,
                      "paper_candidate_eligible": False, "candidate": None}
            def no_action(reason):
                return self._persist({**report, "reason": reason})
            if len(rows) > MAX_INPUT_BARS:
                return no_action("input_capacity")
            if not rows:
                return no_action("warmup_unavailable")
            for row in rows:
                kind = row["acquisition_kind"]
                report["lineage_counts"][kind] = report["lineage_counts"].get(kind, 0) + 1
            if not self._testing and set(report["lineage_counts"]) != {"prospective_observation"}:
                report["evidence_class"] = "TEST" if "test" in report["lineage_counts"] else "HISTORICAL_UNVERIFIED"
                return no_action("nonprospective_input_lineage")
            if any(row["event_time"] - 300 < reg["created_at"] or row["asset_class"] != "equity"
                   or row["venue"] != "alpaca-iex" or row["source_id"] != "alpaca-iex-bars-v2"
                   or row["payload"]["volume_unit"] != "shares" for row in rows):
                return no_action("source_or_registration_window_mismatch")
            merged, _ = _calendar(calendars, as_of)
            first = min(date.fromisoformat(row["session"]["session_date"]) for row in rows)
            last = max(date.fromisoformat(row["session"]["session_date"]) for row in rows)
            days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
            if any(day not in merged for day in days):
                return no_action("calendar_coverage_unavailable")
            latest_session = merged[last]["session"]
            if latest_session is None or not latest_session[0] <= now < latest_session[1]:
                return no_action("regular_session_closed")
            calendar = {"timezone": "America/New_York", "source_id": "alpaca-paper-calendar-v2",
                        "sessions": [{"session_date": day.isoformat(), "open_time": merged[day]["session"][0],
                                      "close_time": merged[day]["session"][1]}
                                     for day in days if merged[day]["session"] is not None]}
            bars = []
            for row in rows:
                day = date.fromisoformat(row["session"]["session_date"])
                declared = merged[day]["session"]
                if declared != (row["session"]["open_time"], row["session"]["close_time"]):
                    return no_action("calendar_bar_session_mismatch")
                usable = max(row["available_time"], row["ingestion_time"],
                             max(merged[d]["visible_at"] for d in days if d <= day))
                bars.append({key: row[key] for key in ("symbol", "asset_class", "venue", "source_id", "event_time", "revision", "interval_seconds")}
                            | {"information_available_time": usable, "ingestion_time": row["ingestion_time"],
                               "session_date": day.isoformat(), "session": "regular"}
                            | {key: row["payload"][key] for key in ("open", "high", "low", "close", "volume", "currency", "volume_unit")})
            # A later calendar can make a bar usable after its archive ingestion.
            # The library's ingestion field represents use by this projection;
            # original receipt and archive clocks stay in the bound source rows.
            for bar in bars:
                bar["ingestion_time"] = max(bar["ingestion_time"], bar["information_available_time"])
            try:
                signals = signals_for(bars, spec, calendar=calendar)
            except StrategyError:
                return no_action("strategy_input_chronology_or_aggregation_unavailable")
            if not signals:
                return no_action("complete_aggregate_unavailable")
            signal = signals[-1]
            metadata = signal["metadata"]
            evaluated = self._now()
            if evaluated < now:
                raise PaperError("paper_intake_clock_rewind")
            report["evaluated_at"] = evaluated
            if not latest_session[0] <= evaluated < latest_session[1]:
                return no_action("regular_session_closed")
            start = signal["decision_time"]
            expires = min(signal["expires_at"], start + ttl_seconds)
            if not start <= evaluated < expires or evaluated - metadata["feature_event_time"] >= ttl_seconds:
                return no_action("original_signal_stale")
            if (metadata["features"] is None or metadata["atr"] is None or amount(metadata["atr"]) == 0
                    or spec["variant"] == "opening_range" and not metadata["features"]["opening_complete"]):
                return no_action("features_or_warmup_unavailable")
            if signal["direction"] == "long" and held > 0 or signal["direction"] == "flat" and held == 0:
                return no_action("target_already_satisfied")
            if signal["direction"] == "flat" and qty > held:
                return no_action("reduction_exceeds_holdings")
            evidence = {"source_records_sha256": _sequence_digest(rows), "source_record_count": len(rows),
                        "calendar_sha256": digest(calendar), "calendar_receipts_sha256": _sequence_digest(calendars),
                        "calendar_receipt_count": len(calendars),
                        "projected_bars_sha256": _sequence_digest(bars), "registration_sha256": reg["registration_sha256"]}
            candidate = decision({"signal_id": signal["signal_id"], "strategy_id": signal["strategy_id"],
                "strategy_sha256": reg["implementation"]["strategy_sha256"], "data_sha256": digest(evidence),
                "symbol": symbol, "side": "buy" if signal["direction"] == "long" else "sell",
                "quantity": str(qty), "limit_price": str(price), "signal_time": start, "expires_at": expires})
            return self._persist({**report, "status": "CANDIDATE", "reason": "canonical_strategy_target_change",
                "candidate": candidate, "source_signal": signal, "evidence": evidence,
                "paper_candidate_eligible": not self._testing}, signal_id=signal["signal_id"])
