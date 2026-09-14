"""Read-only operational health. This is never a strategy qualification report."""

from datetime import datetime
from pathlib import Path
import shutil
from zoneinfo import ZoneInfo

from atlas.collection.config import CollectionError, contract_fingerprint, validate_config
from atlas.collection.state import snapshot, utc_now


def market_state(provider, now, coverage):
    if provider == "coinbase":
        return "open"
    today = datetime.fromtimestamp(now, ZoneInfo("America/New_York")).date().isoformat()
    day = [row for row in coverage if row["start_date"] <= today <= row["end_date"]]
    if not day:
        return "unknown"
    sessions = [session for row in day for session in row["sessions"] if session["session_date"] == today]
    return "open" if any(row["open_time"] <= now < row["close_time"] for row in sessions) else "closed"


def report(config, *, now=None, verify=True, archive_verify=None, disk_usage=None):
    from atlas.collection.archive import verify_collection_archive
    config = validate_config(config)
    now = utc_now() if now is None else utc_now(lambda: now)
    result = {"status": "UNHEALTHY", "checked_at": now, "blockers": [],
        "providers": {}, "archive": {"integrity": "UNKNOWN", "records": None, "latest_timestamp": None},
        "prospective_collection": {"started": None, "continuous_duration_seconds": None,
            "detected_gaps": None, "unresolved_gaps": None},
        "disk": {"used_bytes": None, "remaining_bytes": None},
        "trading": {"live_trading": "DISABLED", "execution_enabled": False},
        "qualification_established": False}
    blockers = result["blockers"]
    marker = Path(config["state_dir"]) / ".restore-incomplete"
    if marker.exists() or marker.is_symlink():
        blockers.append("collector_incomplete_restore")
    try:
        state = snapshot(config)
        meta = state["metadata"]
        age = now - meta["heartbeat_at"]
        if age < 0:
            blockers.append("collector_clock_regressed")
        if not meta["running"]:
            blockers.append("collector_process_stopped")
        elif age > max(180, config["poll_seconds"] * 3):
            blockers.append("collector_heartbeat_stale")
        result["prospective_collection"] = {
            "started": meta["registered_at"], "last_heartbeat": meta["heartbeat_at"],
            "continuous_duration_seconds": max(0, now - meta["continuous_since"]) if meta["continuous_since"] and meta["running"] and 0 <= age <= max(180, config["poll_seconds"] * 3) else None,
            "detected_gaps": state["gap_count"], "unresolved_gaps": state["unresolved_gaps"],
            "test_clock": meta["test_clock"], "duration_is_operational_not_qualification": True}
        for provider, item in state["providers"].items():
            market = market_state(provider, now, item["calendar_coverage"])
            source_blockers = []
            success = item["last_success_at"]
            if success is None:
                source_blockers.append("no_successful_request")
            elif now - success < 0:
                source_blockers.append("request_timestamp_future")
            elif now - success > max(180, config["poll_seconds"] * 3):
                source_blockers.append("provider_requests_stale")
            if item["failures"]:
                source_blockers.append("provider_retrying")
            if market == "unknown":
                source_blockers.append("market_calendar_unknown")
            # The final regular-session bar can arrive after the market closes.
            # Keep its delivery window separate from current quote freshness:
            # closed markets do not owe us a continuing two-sided quote.
            bar_delivery_active = provider == "coinbase" or any(
                row["open_time"] + 300 + config["poll_seconds"] + 60 <= now <= row["close_time"] + 900
                for coverage in item["calendar_coverage"] for row in coverage["sessions"])
            if market == "open" or bar_delivery_active:
                symbols = ("BTC-USD",) if provider == "coinbase" else ("SPY", "QQQ", "GLD", "USO")
                for symbol in symbols:
                    latest = item["last_by_symbol"].get(symbol, {})
                    for kind, limit in (("bar", 900), ("quote", max(90, config["poll_seconds"] + 60))):
                        if (kind == "bar" and not bar_delivery_active
                                or kind == "quote" and market != "open"):
                            continue
                        stamp = latest.get(kind)
                        if stamp is None or now - stamp > limit or stamp > now:
                            source_blockers.append(symbol + "_" + kind + "_missing_or_stale")
            result["providers"][provider] = {
                "status": "HEALTHY" if not source_blockers else "UNHEALTHY", "market": market,
                "last_attempt_at": item["last_attempt_at"], "last_successful_request": success,
                "last_successful_observation": item["last_observation_at"],
                "observation_age_seconds": None if item["last_observation_at"] is None else now - item["last_observation_at"],
                "last_bar_event": item["last_bar_event"], "last_quote_event": item["last_quote_event"],
                "consecutive_failures": item["failures"], "next_attempt_at": item["next_attempt_at"],
                "quote_contract_age_limit_seconds": 60,
                "quote_operational_age_limit_seconds": max(90, config["poll_seconds"] + 60),
                "catchup_pending": item["recovery_cursor"] is not None, "blockers": source_blockers}
            blockers.extend(provider + ":" + code for code in source_blockers)
    except (ValueError, OSError, KeyError, TypeError):
        blockers.append("collector_state_unavailable_or_corrupt")
    try:
        verifier = verify_collection_archive if archive_verify is None else archive_verify
        archive = verifier(config["archive_dir"], clock=lambda: now, full=verify)
        if (archive.get("generation_id") != config["generation_id"]
                or result["prospective_collection"]["started"] is not None
                and (archive.get("started_at") != result["prospective_collection"]["started"]
                     or archive.get("contract_sha256") != contract_fingerprint(config, result["prospective_collection"]["started"]))):
            raise CollectionError("collector_archive_registration_mismatch")
        result["archive"] = {"integrity": "PASS" if archive["integrity_verified"] else "NOT_CHECKED",
            "records": archive["record_count"], "latest_timestamp": archive.get("latest_timestamp"),
            "batches": archive.get("batch_count"), "acquisition_kinds": archive.get("acquisition_kinds"),
            "verification_scope": archive.get("verification_scope")}
        if verify and not archive["integrity_verified"]:
            blockers.append("archive_integrity_not_verified")
    except (ValueError, OSError, KeyError, TypeError):
        result["archive"]["integrity"] = "FAIL"
        blockers.append("archive_integrity_failed")
    try:
        usage = (shutil.disk_usage if disk_usage is None else disk_usage)(config["state_dir"])
        result["disk"] = {"used_bytes": usage.used, "remaining_bytes": usage.free,
                          "minimum_free_bytes": config["min_free_bytes"]}
        if usage.free < config["min_free_bytes"]:
            blockers.append("disk_reserve_exhausted")
    except OSError:
        blockers.append("disk_status_unavailable")
    result["status"] = "UNHEALTHY" if blockers else "HEALTHY"
    return result
