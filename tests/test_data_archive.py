"""Durable offline intake, clock provenance and holdout access boundaries."""

from copy import deepcopy
import json
import os
import socket
import sqlite3

import pytest

import atlas.research.data_archive as archive
from atlas.research.data_contract_v1 import default_contract, VERSION
from atlas.research.imports import _encoded, _sha

NOW = 1789257600  # 2026-09-13 UTC
RESEARCH_START = NOW - 180 * 86400


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("data archive tests cannot contact providers")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(archive.time, "time", lambda: NOW)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "observations"
    archive.init_archive(path, default_contract("test-generation", RESEARCH_START, NOW))
    return path


def fixture(*, event=NOW-300, received=NOW, price="100", kind="historical_backfill"):
    raw = _encoded({"fixture": True, "event": event, "price": price})
    capture = {"role": "bars", "request_url": "https://api.exchange.coinbase.com/products/BTC-USD/candles",
               "received_at": received, "body": raw}
    row = {"contract_version": VERSION, "record_type": "bar", "symbol": "BTC-USD", "asset_class": "crypto",
           "venue": "coinbase-exchange", "source_id": "coinbase-exchange-candles", "event_time": event,
           "publication_time": None, "available_time": received, "ingestion_time": received,
           "revision": 0, "previous_observation_hash": None, "raw_capture_sha256": _sha(raw),
           "acquisition_kind": kind, "revision_basis": "locally_observed_versions", "interval_seconds": 300,
           "session": None, "payload": {"open": price, "high": price, "low": price, "close": price,
                                        "volume": "1", "volume_unit": "unknown", "currency": "USD",
                                        "price_adjustment": "unadjusted", "corporate_actions_status": "not_applicable"}}
    return row, capture


def append(root, **kwargs):
    row, capture = fixture(**kwargs)
    return archive.append_backfill(root, records=[row], captures=[capture])


def bytes_at(root):
    return {p.name: p.read_bytes() for p in root.iterdir()}


def test_initial_archive_and_read_only_verification(root):
    before = bytes_at(root)
    result = archive.verify_archive(root)
    assert result["record_count"] == 0 and result["integrity_verified"] is True
    assert result["execution_enabled"] is False
    assert bytes_at(root) == before
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "archive.sqlite3").stat().st_mode & 0o777 == 0o600


def test_registration_cannot_be_backdated_or_reuse_destination(root, tmp_path):
    with pytest.raises(ValueError, match="current_receipt"):
        archive.init_archive(tmp_path / "old", default_contract("old", RESEARCH_START, NOW-1))
    with pytest.raises(ValueError, match="new_external"):
        archive.init_archive(root, default_contract("second", RESEARCH_START, NOW))


def test_backfill_label_cannot_promote_and_ingestion_is_append_clock(root):
    row, cap = fixture(event=NOW, received=NOW, kind="prospective_observation")
    result = archive.append_backfill(root, records=[row], captures=[cap])
    assert result["acquisition_kinds"] == {"historical_backfill": 1, "prospective_observation": 0, "test": 0}
    rows = archive.read_bucket(root, bucket="validation", as_of=NOW)
    assert rows[0]["acquisition_kind"] == "historical_backfill"
    assert rows[0]["publication_time"] is None
    assert archive.read_bucket(root, bucket="validation", as_of=NOW-1) == []


def test_old_capture_retained_new_ingestion_stamped(root):
    row, cap = fixture(event=NOW-600, received=NOW-200)
    archive.append_backfill(root, records=[row], captures=[cap])
    saved = archive.read_bucket(root, bucket="validation", as_of=NOW)[0]
    assert saved["available_time"] == NOW-200
    assert saved["ingestion_time"] == NOW
    assert archive.read_bucket(root, bucket="validation", as_of=NOW-100) == []


def test_unchanged_capture_keeps_first_observation(root, monkeypatch):
    append(root)
    monkeypatch.setattr(archive.time, "time", lambda: NOW+10)
    result = append(root, received=NOW+10)
    assert result["record_count"] == 1 and result["batch_count"] == 2
    assert result["unchanged_observations"] == 1 and result["captures_retained"] == 1
    assert archive.read_bucket(root, bucket="validation", as_of=NOW+10)[0]["ingestion_time"] == NOW


def test_changed_value_appends_revision_and_asof_keeps_original(root, monkeypatch):
    append(root)
    monkeypatch.setattr(archive.time, "time", lambda: NOW+10)
    append(root, received=NOW+10, price="101")
    old = archive.read_bucket(root, bucket="validation", as_of=NOW)[0]
    new = archive.read_bucket(root, bucket="validation", as_of=NOW+10)[0]
    assert old["payload"]["close"] == "100" and old["revision"] == 0
    assert new["payload"]["close"] == "101" and new["revision"] == 1
    assert new["previous_observation_hash"] == _sha(_encoded(old))


@pytest.mark.parametrize("first_kind", ["historical_import", "test"])
def test_changed_builtin_observation_never_promotes_lineage(root, monkeypatch, first_kind):
    row, cap = fixture(event=NOW)
    archive._append(root, records=[row], captures=[cap], calendar_coverage=[], origin=first_kind, provenance="offline fixture")
    monkeypatch.setattr(archive.time, "time", lambda: NOW+10)
    row, cap = fixture(event=NOW, received=NOW+10, price="101")
    archive._append(root, records=[row], captures=[cap], calendar_coverage=[], origin="builtin_get", provenance="offline fixture")
    newest = archive.read_bucket(root, bucket="validation", as_of=NOW+10)[0]
    assert newest["acquisition_kind"] == ("test" if first_kind == "test" else "historical_backfill")


@pytest.mark.parametrize("change", ["raw_hash", "future_ingestion", "receipt_mismatch", "duplicate", "calendar", "source"])
def test_invalid_append_rolls_back_entire_transaction(root, change):
    append(root)
    before = bytes_at(root)
    row, cap = fixture(price="101")
    rows, calendars = [row], []
    if change == "raw_hash":
        row["raw_capture_sha256"] = "a"*64
    elif change == "future_ingestion":
        row["ingestion_time"] = NOW+1
    elif change == "receipt_mismatch":
        cap["received_at"] -= 1
    elif change == "duplicate":
        rows *= 2
    elif change == "source":
        row["source_id"] = "invented-source"
    else:
        calendars = [{"unexpected": True}]
    with pytest.raises(ValueError):
        archive.append_backfill(root, records=rows, captures=[cap], calendar_coverage=calendars)
    assert archive.verify_archive(root)["record_count"] == 1
    assert bytes_at(root) == before


def test_injected_registration_never_produces_prospective_data(tmp_path):
    root = tmp_path / "injected"
    archive.init_archive(root, default_contract("fixture", RESEARCH_START, NOW), clock=lambda: NOW)
    row, cap = fixture(event=NOW)
    result = archive._append(root, records=[row], captures=[cap], calendar_coverage=[], origin="builtin_get", provenance="fixture")
    assert result["acquisition_kinds"]["test"] == 1


def test_holdout_sealed_until_elapsed_matching_frozen_spec(root, monkeypatch):
    contract = archive.load_contract(root)
    start = contract["windows"]["prospective_holdout"]["start"]
    end = contract["windows"]["prospective_holdout"]["end_exclusive"]
    archive.freeze_strategy(root, strategy_sha256="a"*64)
    monkeypatch.setattr(archive.time, "time", lambda: start+300)
    append(root, event=start+300, received=start+300)
    with pytest.raises(ValueError, match="holdout_sealed"):
        archive.read_bucket(root, bucket="prospective_holdout", as_of=start+300, strategy_sha256="a"*64)
    monkeypatch.setattr(archive.time, "time", lambda: end)
    with pytest.raises(ValueError, match="holdout_sealed"):
        archive.read_bucket(root, bucket="prospective_holdout", as_of=end, strategy_sha256="b"*64)
    rows = archive.read_bucket(root, bucket="prospective_holdout", as_of=end, strategy_sha256="a"*64)
    assert len(rows) == 1 and rows[0]["acquisition_kind"] == "historical_backfill"


def test_freeze_cannot_change_or_be_added_after_holdout_start(root, monkeypatch):
    archive.freeze_strategy(root, strategy_sha256="a"*64)
    with pytest.raises(ValueError, match="already_frozen"):
        archive.freeze_strategy(root, strategy_sha256="b"*64)
    monkeypatch.setattr(archive.time, "time", lambda: NOW+600)
    with pytest.raises(ValueError):
        archive.freeze_strategy(root, strategy_sha256="c"*64)


@pytest.mark.parametrize("table", ["manifest", "batches", "captures", "observations", "strategy_freeze"])
def test_tables_reject_updates_and_deletes(root, table):
    append(root)
    archive.freeze_strategy(root, strategy_sha256="a"*64)
    with sqlite3.connect(root / "archive.sqlite3") as db:
        for query in (f"UPDATE {table} SET body=body", f"DELETE FROM {table}"):
            with pytest.raises(sqlite3.IntegrityError, match="append_only"):
                db.execute(query)


def test_tampered_bytes_or_schema_rejected(root):
    append(root)
    with sqlite3.connect(root / "archive.sqlite3") as db:
        db.execute("DROP TRIGGER captures_no_update")
        db.execute("UPDATE captures SET body=?", (b"tampered",))
    with pytest.raises(ValueError, match="schema_or_integrity"):
        archive.verify_archive(root)


def test_concurrent_writer_fails_closed(root):
    with sqlite3.connect(root / "archive.sqlite3") as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="busy"):
            append(root)
        db.rollback()
    assert archive.verify_archive(root)["record_count"] == 0


@pytest.mark.parametrize("problem", ["missing", "empty", "symlink", "permissions", "unexpected"])
def test_invalid_storage_not_repaired(root, tmp_path, problem):
    path = root / "archive.sqlite3"
    if problem == "missing":
        path.unlink()
    elif problem == "empty":
        path.write_bytes(b"")
    elif problem == "symlink":
        path.rename(tmp_path / "elsewhere")
        path.symlink_to(tmp_path / "elsewhere")
    elif problem == "permissions":
        path.chmod(0o644)
    else:
        (root / "unexpected").write_text("retained")
    with pytest.raises(ValueError):
        archive.verify_archive(root)


def test_receipt_clock_rounds_up_and_rejects_invalid():
    assert archive.receipt_time(lambda: NOW+0.001) == NOW+1
    for value in (True, float("nan"), float("inf"), 0, -1, "123"):
        with pytest.raises(ValueError, match="clock_invalid"):
            archive.receipt_time(lambda: value)


def test_bucket_membership_excludes_bar_straddling_window_start(root, monkeypatch):
    contract = archive.load_contract(root)
    opening = contract["windows"]["prospective_holdout"]["start"]
    monkeypatch.setattr(archive.time, "time", lambda: opening)
    result = append(root, event=opening, received=opening)
    assert result["bucket_record_counts"]["prospective_holdout"] == 0
    assert result["bucket_record_counts"]["outside_study"] == 1
    assert archive.bucket_for(contract, opening, 300) == "outside_study"
    assert archive.bucket_for(contract, opening, 0) == "prospective_holdout"


def test_calendar_receipt_binds_to_raw_capture_and_import_visibility(root):
    row, cap = fixture()
    calendar = {"start_date": "2026-09-13", "end_date": "2026-09-13", "sessions": [],
                "source_id": "alpaca-paper-calendar-v2", "raw_capture_sha256": row["raw_capture_sha256"],
                "received_at": NOW-1}
    with pytest.raises(ValueError, match="calendar_capture_time_mismatch"):
        archive.append_backfill(root, records=[row], captures=[cap], calendar_coverage=[calendar])
    cap["received_at"] = NOW-100
    row["available_time"] = row["ingestion_time"] = NOW-100
    calendar["received_at"] = NOW-100
    archive.append_backfill(root, records=[row], captures=[cap], calendar_coverage=[calendar])
    with archive._connection(root) as db:
        metadata = archive._scan(db, NOW)["calendar_coverage"][0]
    assert metadata["received_at"] == NOW-100
    assert metadata["ingestion_time"] == NOW


def test_append_cannot_commit_archive_over_size_bound(root, monkeypatch):
    before = bytes_at(root)
    monkeypatch.setattr(archive, "MAX_DB_BYTES", (root / "archive.sqlite3").stat().st_size+1)
    row, cap = fixture()
    cap["body"] = _encoded({"bounded_fixture": "x"*100000})
    row["raw_capture_sha256"] = _sha(cap["body"])
    with pytest.raises(ValueError, match="size_bound_exceeded"):
        archive.append_backfill(root, records=[row], captures=[cap])
    assert bytes_at(root) == before
    assert archive.verify_archive(root)["record_count"] == 0


def test_collector_transport_fixture_stays_test_through_durable_append(root):
    from atlas.research.data_collector import HTTPResponse
    def get(url, headers, **limits):
        body = ([[NOW-600, 100, 100, 100, 100, 1]] if "/candles?" in url else
                {"sequence": 1, "bids": [["100", "1", 1]], "asks": [["101", "1", 1]]})
        return HTTPResponse(200, _encoded(body))
    result = archive.acquire_once(root, provider="coinbase", start=NOW-600, end=NOW-300,
                                  transport=get, clock=lambda: NOW)
    assert result["collection_complete"] is True
    assert result["acquisition_kinds"]["test"] == 2
    assert result["acquisition_kinds"]["prospective_observation"] == 0
    assert result["captures_retained"] == 2


@pytest.mark.parametrize("failure", ["malformed_candles", "malformed_book", "transport"])
def test_failed_collection_retains_only_safe_raw_captures(root, failure):
    from atlas.research.data_collector import HTTPResponse
    calls = []
    def get(url, headers, **limits):
        calls.append(url)
        if failure == "transport":
            raise ConnectionError("private remote diagnostic")
        if failure == "malformed_candles" or "/book?" in url:
            return HTTPResponse(200, b"not valid JSON with private remote text")
        return HTTPResponse(200, _encoded([[NOW-600, 100, 100, 100, 100, 1]]))
    result = archive.acquire_once(root, provider="coinbase", start=NOW-600, end=NOW-300,
                                  transport=get, clock=lambda: NOW)
    assert result["collection_complete"] is False
    assert result["record_count"] == result["appended_records"] == 0
    assert result["captures_retained"] == {"transport": 0, "malformed_candles": 1, "malformed_book": 2}[failure]
    assert "private remote" not in json.dumps(result)
    assert result["batch_count"] == (0 if failure == "transport" else 1)
    assert archive.verify_archive(root)["integrity_verified"] is True
