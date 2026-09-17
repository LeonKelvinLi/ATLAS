"""One foreground GET-only process, with durable retry and gap checkpoints."""

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import signal
import threading
import time

from atlas.collection.config import CollectionError, check_environment, contract_fingerprint, private, validate_config
from atlas.research.data_collector import safe_failure_diagnostic
from atlas.collection.state import (
    apply_restore_marker, clock_sample, connection, event, file_lock, gap, initialize_state, metadata,
    provider_state, save_metadata, save_provider, snapshot, sync_dir, utc_now,
)


EQUITIES = ("SPY", "QQQ", "GLD", "USO")


def emit(kind, now, *, provider=None, **fields):
    print(json.dumps({"time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "event": kind, "provider": provider, **fields}, sort_keys=True, allow_nan=False), flush=True)


def init_runtime(config, *, clock=None):
    """Explicit initialization only. Partial state is retained and refuses reuse."""
    from atlas.collection.archive import init_collection_archive
    from atlas.research.data_contract_v1 import default_contract
    config = validate_config(config)
    now = utc_now(clock)
    root = Path(config["state_dir"])
    if root.is_symlink() or not root.parent.is_dir():
        raise CollectionError("collector_initialization_requires_new_state_directory")
    if root.exists():
        private(root, directory=True)
        if any(root.iterdir()):
            raise CollectionError("collector_initialization_requires_new_state_directory")
    contract = default_contract(config["generation_id"], config["research_start"], now)
    try:
        if not root.exists():
            root.mkdir(mode=0o700)
        (root / "archives").mkdir(mode=0o700)
        init_collection_archive(config["archive_dir"], contract, clock=clock)
        initialize_state(config, now, test_clock=clock is not None)
        sync_dir(root.parent)
    except OSError:
        raise CollectionError("collector_initialization_failed_retained_for_inspection") from None
    return {"initialized": True, "registered_at": now, "execution_enabled": False,
            "generation_id": config["generation_id"]}


def backoff_seconds(failures, *, poll_seconds, maximum, retry_after=None):
    delay = min(maximum, poll_seconds * 2 ** min(max(failures - 1, 0), 16))
    if type(retry_after) in (int, float) and 0 <= retry_after <= 86400:
        # A server's Retry-After is a minimum; never shorten it to the normal cap.
        delay = max(delay, int(retry_after + 0.999999))
    return delay


def expected_events(provider, start, end, calendar):
    """Expected mature bar closes; a declared empty calendar means a holiday."""
    if provider == "coinbase":
        return {"BTC-USD": set(range(start + 300, end + 1, 300))}, True
    result = {symbol: set() for symbol in EQUITIES}
    if type(calendar) is not list or not calendar:
        return result, False
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    first_date = datetime.fromtimestamp(start, ny).date().isoformat()
    last_date = datetime.fromtimestamp(max(start, end - 1), ny).date().isoformat()
    if not any(row["start_date"] <= first_date and row["end_date"] >= last_date for row in calendar):
        return result, False
    for coverage in calendar:
        for session in coverage["sessions"]:
            events = set(range(max(start, session["open_time"]) + 300,
                               min(end, session["close_time"]) + 1, 300))
            for symbol in EQUITIES:
                result[symbol].update(events)
    return result, True


def _runs(events):
    first = last = None
    for value in sorted(events):
        if first is not None and value != last + 300:
            yield first - 300, last
            first = None
        if first is None:
            first = value
        last = value
    if first is not None:
        yield first - 300, last


def _observations(result):
    rows = result.get("collection_observations")
    if type(rows) is not list or len(rows) > 20000:
        raise CollectionError("collector_result_metadata_missing_or_invalid")
    allowed_symbols = {*EQUITIES, "BTC-USD"}
    for row in rows:
        if (type(row) is not dict or not {"symbol", "record_type", "event_time"} <= row.keys()
                or row["symbol"] not in allowed_symbols or row["record_type"] not in ("bar", "quote")
                or type(row["event_time"]) is not int):
            raise CollectionError("collector_result_metadata_missing_or_invalid")
    return rows


def _record_success(db, config, provider, state, result, now, start, end, *, recovery, outage=False):
    rows = _observations(result)
    coverage = result.get("collection_calendar_coverage", [])
    if type(coverage) is not list:
        raise CollectionError("collector_result_calendar_invalid")
    mature_end = end if recovery else max(start, end - 300)
    expected, known = expected_events(provider, start, mature_end, coverage)
    seen = {symbol: set() for symbol in expected}
    for row in rows:
        symbol, kind, stamp = row["symbol"], row["record_type"], row["event_time"]
        if stamp > now or stamp <= 0:
            raise CollectionError("collector_result_timestamp_invalid")
        if kind == "bar" and symbol in seen:
            seen[symbol].add(stamp)
        state["last_observation_at"] = max(state["last_observation_at"] or 0, stamp)
        name = "last_bar_event" if kind == "bar" else "last_quote_event"
        state[name] = max(state[name] or 0, stamp)
        value = state["last_by_symbol"].setdefault(symbol, {"bar": None, "quote": None})
        value[kind] = max(value[kind] or 0, stamp)
    missing = 0
    if known:
        for symbol, wanted in expected.items():
            for begin, finish in _runs(wanted - seen[symbol]):
                gap(db, now, provider, symbol, begin, finish, "missing_provider_bars")
                missing += (finish - begin) // 300
        # Filled historical gaps remain visible as resolved historical incidents.
        for gap_id, symbol, begin, finish in db.execute(
                "SELECT id,symbol,start,end FROM gaps WHERE provider=? AND resolved_at IS NULL AND start>=? AND end<=?",
                (provider, start, mature_end)).fetchall():
            if symbol == "*":
                complete = all(not (wanted - seen[name]) for name, wanted in expected.items())
            else:
                wanted = set(range(begin + 300, finish + 1, 300))
                complete = symbol in seen and wanted <= seen[symbol]
            if complete:
                db.execute("UPDATE gaps SET resolved_at=? WHERE id=?", (now, gap_id))
                event(db, now, "gap_filled_historically", provider, gap_id=gap_id)
    if not recovery:
        old_end = state["cursor_end"]
        gap_end = end if outage else start
        if old_end < gap_end:
            gap(db, now, provider, "*", old_end, gap_end, "collection_outage")
            state["recovery_cursor"] = min(state["recovery_cursor"] or old_end, old_end)
            state["recovery_until"] = max(state["recovery_until"] or gap_end, gap_end)
        state["cursor_end"] = max(old_end, end)
        state["calendar_coverage"] = coverage
    else:
        state["recovery_cursor"] = end
        if end >= state["recovery_until"]:
            state.update(recovery_cursor=None, recovery_until=None)
    reconnected = state["failures"] > 0
    state.update(last_success_at=now, failures=0, last_error=None,
                 next_attempt_at=now + config["poll_seconds"])
    event(db, now, "reconnected" if reconnected else "request_succeeded", provider,
          start=start, end=end, recovery=recovery, appended_records=result.get("appended_records", 0),
          observed_records=len(rows), missing_mature_bars=missing, calendar_known=known)
    return missing, known, reconnected


def _attempt(config, provider, start, end, *, recovery, acquire, clock, log):
    now = utc_now(clock)
    # Backups cannot split an archive commit from its operational checkpoint.
    with file_lock(config["state_dir"], ".snapshot.lock", timeout=10):
        with connection(config["state_dir"], write=True) as db:
            state = provider_state(db, provider)
            meta = metadata(db)
            last_good = state["last_success_at"] or meta["registered_at"]
            outage = not recovery and now - last_good > config["poll_seconds"] * 3
            state["last_attempt_at"] = now
            save_provider(db, provider, state)
            event(db, now, "request_started", provider, start=start, end=end, recovery=recovery)
        kwargs = {"provider": provider, "start": start, "end": end,
                  "key_file": config["alpaca_key_file"] if provider == "alpaca" else None,
                  "acquisition_mode": "outage_backfill" if recovery or outage else "prospective"}
        if clock is not None:
            kwargs["clock"] = clock
        result = acquire(config["archive_dir"], **kwargs)
        completed = utc_now(clock)
        if completed < now:
            raise CollectionError("collector_clock_regressed")
        failure_fields = {}
        with connection(config["state_dir"], write=True) as db:
            state, meta = provider_state(db, provider), metadata(db)
            if result.get("collection_complete") is True:
                missing, known, reconnected = _record_success(
                    db, config, provider, state, result, completed, start, end, recovery=recovery, outage=outage)
                if missing or not known or outage:
                    meta["continuous_since"] = None
                elif not recovery and meta["continuous_since"] is None and all(
                    name == provider or (provider_state(db, name)["last_success_at"] is not None
                        and completed - provider_state(db, name)["last_success_at"] <= config["poll_seconds"] * 3
                        and provider_state(db, name)["failures"] == 0)
                    for name in config["providers"]):
                    meta["continuous_since"] = completed
                outcome = "reconnected" if reconnected else "request_succeeded"
            else:
                state["failures"] += 1
                # Exact owned codes and bounded primitives survive recovery in
                # append-only events, even when HTTP failed before any capture.
                # Never reflect exception text, URLs, headers or remote bodies.
                diagnostic = safe_failure_diagnostic(result.get("error"),
                    status=result.get("http_status"),
                    retry_after_seconds=result.get("retry_after_seconds"),
                    role=result.get("request_role"))
                state["last_error"] = diagnostic["error"]
                delay = backoff_seconds(state["failures"], poll_seconds=config["poll_seconds"],
                    maximum=config["max_backoff_seconds"], retry_after=diagnostic["retry_after_seconds"])
                state["next_attempt_at"] = completed + delay
                meta["continuous_since"] = None
                failure_fields = {"diagnostic_version": 1, **diagnostic,
                    "duration_seconds": completed - now, "failures": state["failures"],
                    "retry_in_seconds": delay}
                event(db, completed, "request_failed", provider, **failure_fields)
                outcome = "request_failed"
            meta["heartbeat_at"] = completed
            save_provider(db, provider, state)
            save_metadata(db, meta)
        log(outcome, completed, provider=provider, recovery=recovery,
            appended_records=result.get("appended_records", 0), next_attempt_at=state["next_attempt_at"],
            **failure_fields)
    return result.get("collection_complete") is True


def run(config, *, once=False, stop_event=None, clock=None, acquire=None,
        archive_verify=None, max_cycles=None, log=emit):
    """Foreground loop. Test clocks/transports never create production evidence."""
    from atlas.collection.archive import acquire_collection_once, verify_collection_archive
    config = validate_config(config)
    if max_cycles is not None and (type(max_cycles) is not int or not 1 <= max_cycles <= 10000):
        raise CollectionError("collector_cycle_bound_invalid")
    if acquire is not None and clock is None:
        raise CollectionError("collector_injected_acquirer_requires_test_clock")
    acquire = acquire_collection_once if acquire is None else acquire
    verifier = verify_collection_archive if archive_verify is None else archive_verify
    stop = threading.Event() if stop_event is None else stop_event
    old_handlers = {}
    with file_lock(config["state_dir"], ".collector.lock"):
        incomplete = Path(config["state_dir"]) / ".restore-incomplete"
        if incomplete.exists() or incomplete.is_symlink():
            raise CollectionError("collector_incomplete_restore_refused")
        # A killed SQLite writer may leave a hot DELETE journal. The one-writer
        # lock permits SQLite's own rollback before a read-only integrity scan.
        with connection(config["state_dir"], write=True):
            pass
        initial = snapshot(config)
        if clock is not None and not initial["metadata"]["test_clock"]:
            raise CollectionError("collector_test_clock_on_production_state_refused")
        now = utc_now(clock)
        if now < initial["metadata"]["heartbeat_at"]:
            raise CollectionError("collector_clock_regressed")
        with file_lock(config["state_dir"], ".snapshot.lock"):
            verified = verifier(config["archive_dir"], clock=clock, full=True, recover=True)
            if (verified.get("generation_id") != config["generation_id"]
                    or verified.get("started_at") != initial["metadata"]["registered_at"]
                    or verified.get("contract_sha256") != contract_fingerprint(config, initial["metadata"]["registered_at"])):
                raise CollectionError("collector_archive_registration_mismatch")
            apply_restore_marker(config, now)
            with connection(config["state_dir"], write=True) as db:
                meta = metadata(db)
                if meta["running"]:
                    event(db, now, "unclean_restart")
                meta.update(running=True, heartbeat_at=now, continuous_since=None)
                save_metadata(db, meta)
                event(db, now, "started")
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda _number, _frame: stop.set())
        log("started", now, execution_enabled=False)
        cycles, attempts = 0, 0
        try:
            while not stop.is_set():
                check_environment()
                sampled_time = clock_sample(clock)
                now = math.ceil(sampled_time)
                if shutil.disk_usage(config["state_dir"]).free < config["min_free_bytes"]:
                    raise CollectionError("collector_disk_reserve_exhausted")
                current = snapshot(config, thorough=False)
                if now < current["metadata"]["heartbeat_at"]:
                    raise CollectionError("collector_clock_regressed")
                for provider in config["providers"]:
                    if stop.is_set():
                        break
                    state = current["providers"][provider]
                    if now < state["next_attempt_at"]:
                        continue
                    # Receipt time rounds up; a completed window must not.
                    end = math.floor(sampled_time) // 300 * 300
                    start = end - config["overlap_seconds"]
                    ok = _attempt(config, provider, start, end, recovery=False,
                                  acquire=acquire, clock=clock, log=log)
                    attempts += 1
                    if ok and not stop.is_set():
                        with connection(config["state_dir"]) as db:
                            state = provider_state(db, provider)
                        if state["recovery_cursor"] is not None:
                            begin = state["recovery_cursor"]
                            finish = min(begin + 86400, state["recovery_until"])
                            if begin < finish:
                                _attempt(config, provider, begin, finish, recovery=True,
                                         acquire=acquire, clock=clock, log=log)
                                attempts += 1
                with connection(config["state_dir"], write=True) as db:
                    meta = metadata(db)
                    meta["heartbeat_at"] = utc_now(clock)
                    save_metadata(db, meta)
                cycles += 1
                if once or max_cycles is not None and cycles >= max_cycles:
                    break
                stop.wait(min(config["poll_seconds"], 30))
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            stopped = utc_now(clock)
            with file_lock(config["state_dir"], ".snapshot.lock", timeout=10):
                with connection(config["state_dir"], write=True) as db:
                    meta = metadata(db)
                    meta.update(running=False, heartbeat_at=stopped, continuous_since=None)
                    save_metadata(db, meta)
                    event(db, stopped, "stopped")
            log("stopped", stopped, execution_enabled=False)
    return {"cycles": cycles, "attempts": attempts, "execution_enabled": False}
