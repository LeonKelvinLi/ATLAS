"""Synthetic intake checks; no provider, credentials or research archive access."""
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
from urllib.parse import parse_qs, urlsplit
import pytest

from atlas.alpaca_paper_preflight import HTTPResponse
from atlas.ledger import digest
from atlas.paper import intake as module
from atlas.paper.intake import PaperIntake
from atlas.paper.models import PaperError, decision
from atlas.paper.risk import PaperRiskPolicy
from atlas.paper.strategy import strategy_identity
from atlas.research import data_archive as archive
from atlas.research.multiasset_strategies import normalize_spec

OPEN = int(datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc).timestamp())
SPEC = normalize_spec({"family": "equity_mean_reversion", "variant": "zscore", "lookback": 5})
POLICY = PaperRiskPolicy(allowed_strategy_ids=(SPEC["strategy_id"],))

class Clock:
    def __init__(self): self.at = OPEN
    def __call__(self): return self.at

class Transport:
    def __init__(self, clock):
        self.clock, self.calls, self.prices, self.omit = clock, [], {}, set()
        self.fail_path = None
        self.calendar = [{"date": "2026-09-15", "open": "09:30", "close": "16:00"}]
    def __call__(self, url, headers, **options):
        assert not any(key.startswith("APCA-") for key in headers)
        assert options == {"timeout_seconds": 10, "max_response_bytes": 2_000_000}
        parsed = urlsplit(url)
        self.calls.append(url)
        if parsed.path == self.fail_path:
            return HTTPResponse(403, b'{"message":"secret remote body"}')
        if parsed.path == "/v2/calendar":
            body = self.calendar
        elif parsed.path == "/v2/stocks/bars":
            query = parse_qs(parsed.query)
            start, end = (int(datetime.fromisoformat(query[name][0].replace("Z", "+00:00")).timestamp())
                          for name in ("start", "end"))
            body = {"bars": {"SPY": [
                {"t": datetime.fromtimestamp(at, timezone.utc).isoformat(), "o": price,
                 "h": price + .05, "l": price - .05, "c": price, "v": 100}
                for at, price in sorted(self.prices.items()) if start <= at < end and at not in self.omit]},
                "next_page_token": None}
        else:
            assert parsed.path == "/v2/stocks/quotes/latest"
            body = {"quotes": {}}
        return HTTPResponse(200, json.dumps(body, separators=(",", ":")).encode())

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tests cannot use credentials, brokers or network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(module.collector, "_credentials", refuse)
    monkeypatch.setattr(module.collector, "alpaca_get", refuse)

@pytest.fixture
def stream(tmp_path):
    clock = Clock()
    transport = Transport(clock)
    intake = PaperIntake.testing(tmp_path / "paper-input", clock=clock, transport=transport,
        generation_id="paper-fixture", spec=SPEC, account_fingerprint="a" * 64, risk_policy=POLICY)
    return intake, clock, transport

def populate(stream, prices=(100, 101, 99, 101, 99, 100, 98)):
    intake, clock, transport = stream
    for index, price in enumerate(prices):
        for offset in range(3):
            opening = OPEN + (index * 3 + offset) * 300
            transport.prices[opening] = price
            clock.at = opening + 300 + .1
            assert intake.acquire()["status"] == "ACQUIRED"
    clock.at += 1.1
    return intake.registration()["registration_sha256"]

def project(stream, **changes):
    intake = stream[0]
    params = {"symbol": "SPY", "quantity": ".09", "limit_price": "98", "held_quantity": "0",
              "expected_registration_sha256": intake.registration()["registration_sha256"]}
    return intake.project(**(params | changes))

def test_owned_registration_is_offline_and_binds_sources(tmp_path):
    intake = PaperIntake.register(tmp_path / "owned-input", generation_id="owned-new",
        spec=SPEC, account_fingerprint="a" * 64, risk_policy=POLICY)
    reg = intake.registration()
    assert reg["role"] == "PAPER_OBSERVATION"
    assert reg["implementation"]["strategy"]["strategy_sha256"] == strategy_identity(SPEC)["strategy_sha256"]
    assert reg["implementation"]["strategy_sha256"] != strategy_identity(SPEC)["strategy_sha256"]
    assert set(reg["implementation"]["sources"]) == set(module.SOURCE_FILES)
    assert reg["registration_sha256"] == digest({k: v for k, v in reg.items() if k != "registration_sha256"})

def test_injection_registration_cannot_be_promoted_or_downgraded(stream, tmp_path):
    intake, clock, transport = stream
    with pytest.raises(PaperError, match="source_changed"):
        PaperIntake(intake._root)
    owned = PaperIntake.register(tmp_path / "owned", generation_id="owned", spec=SPEC,
        account_fingerprint="a" * 64, risk_policy=POLICY)
    with pytest.raises(PaperError, match="source_changed"):
        PaperIntake.testing(owned._root, clock=clock, transport=transport)

def test_registration_requires_new_isolated_state(stream, tmp_path):
    intake, clock, transport = stream
    with pytest.raises(PaperError, match="initialization_requires_empty"):
        PaperIntake.testing(intake._root, clock=clock, transport=transport,
            generation_id="other", spec=SPEC, account_fingerprint="a" * 64, risk_policy=POLICY)
    with pytest.raises(PaperError, match="isolated_state"):
        PaperIntake.register(tmp_path / "atlas-collector-private", generation_id="other", spec=SPEC,
            account_fingerprint="a" * 64, risk_policy=POLICY)
    researcher = tmp_path / "research"
    archive.init_archive(researcher, module.default_contract("research", OPEN, OPEN), clock=clock)
    with pytest.raises(PaperError, match="layout_invalid"):
        PaperIntake.testing(researcher, clock=clock, transport=transport)

@pytest.mark.parametrize("risk", [None, {}, PaperRiskPolicy()])
def test_requires_reviewed_policy(tmp_path, risk):
    with pytest.raises(PaperError):
        PaperIntake.register(tmp_path / "invalid", generation_id="invalid", spec=SPEC,
            account_fingerprint="a" * 64, risk_policy=risk)
    assert not (tmp_path / "invalid").exists()

def test_wait_complete_bar_uses_no_get(stream):
    assert stream[0].acquire() == {"status": "WAIT_COMPLETE_BAR", "orders_submitted": 0}
    assert stream[2].calls == []

def test_owned_acquisition_refuses_old_key_before_network(tmp_path):
    intake = PaperIntake.register(tmp_path / "owned", generation_id="owned", spec=SPEC,
        account_fingerprint="a" * 64, risk_policy=POLICY)
    with pytest.raises(ValueError, match="legacy_pilot_credentials_protected"):
        intake.acquire(key_file=str(Path.home() / ".config/atlas/alpaca-paper/paper-keys.json"))

def test_injection_never_reads_credentials(stream):
    with pytest.raises(PaperError, match="test_credentials_refused"):
        stream[0].acquire(key_file="/private/no-read.json")

def test_bounded_fixed_gets_and_test_lineage(stream):
    intake, _, transport = stream
    populate(stream)
    with archive._connection(intake._root / "observations") as db:
        state = intake._scan(db, intake.registration())
    assert len(state["records"]) == 21
    assert {row["acquisition_kind"] for row in state["records"]} == {"test"}
    for url in transport.calls:
        parsed = urlsplit(url)
        assert parsed.scheme == "https"
        assert parsed.hostname in {"paper-api.alpaca.markets", "data.alpaca.markets"}
        assert parsed.path in {"/v2/calendar", "/v2/stocks/bars", "/v2/stocks/quotes/latest"}
        if parsed.path == "/v2/stocks/bars":
            query = parse_qs(parsed.query)
            start, end = (datetime.fromisoformat(query[key][0].replace("Z", "+00:00")) for key in ("start", "end"))
            assert (end - start).total_seconds() <= 900

def test_candidate_exact_contract_and_durable_receipt(stream):
    populate(stream)
    result = project(stream)
    assert result["status"] == "CANDIDATE"
    assert decision(result["candidate"]) == result["candidate"]
    assert result["candidate"]["side"] == "buy"
    assert result["candidate"]["signal_time"] == OPEN + 6301
    assert result["candidate"]["expires_at"] == OPEN + 6601
    assert result["source_signal"]["strategy_id"] == SPEC["strategy_id"]
    assert result["evidence_class"] == "TEST"
    for field in ("paper_candidate_eligible", "provider_authenticity_verified",
                  "prospective_qualified", "execution_authorized", "live_enabled"):
        assert result[field] is False
    saved = module._read_json(stream[0]._root / "receipts" / result["receipt_name"])
    assert result["receipt_sha256"] == digest(saved)
    assert saved["candidate"] == result["candidate"]

def test_restart_and_unchanged_capture_preserve_original_candidate(stream):
    populate(stream)
    original = project(stream)
    intake, clock, transport = stream
    clock.at += 1
    intake.acquire()
    clock.at += 1
    reopened = PaperIntake.testing(intake._root, clock=clock, transport=transport)
    again = project((reopened, clock, transport))
    assert again["replayed"] is True
    assert again["candidate"] == original["candidate"]
    assert again["receipt_sha256"] == original["receipt_sha256"]

@pytest.mark.parametrize("changes", [{"quantity": ".08"}, {"limit_price": "97.99"}, {"ttl_seconds": 200}])
def test_existing_signal_cannot_change_terms(stream, changes):
    populate(stream)
    project(stream)
    with pytest.raises(PaperError, match="signal_terms_changed"):
        project(stream, **changes)

def test_projection_cannot_refresh_expired_signal(stream):
    populate(stream)
    original = project(stream)
    stream[1].at = original["candidate"]["expires_at"]
    result = project(stream)
    assert result["reason"] == "original_signal_stale" and result["candidate"] is None

@pytest.mark.parametrize("ttl", [0, 301, -1, True, 1.2])
def test_ttl_caps(stream, ttl):
    with pytest.raises(PaperError, match="ttl_invalid"):
        project(stream, ttl_seconds=ttl)

def test_binding_refuses_before_read(stream, monkeypatch):
    monkeypatch.setattr(archive, "_connection", lambda *a, **k: pytest.fail("payload read"))
    with pytest.raises(PaperError, match="registration_mismatch"):
        project(stream, expected_registration_sha256="b" * 64)

def test_source_change_refuses(stream, monkeypatch):
    original = module._identity
    monkeypatch.setattr(module, "_identity", lambda spec: original(spec) | {"strategy_sha256": "b" * 64})
    with pytest.raises(PaperError, match="source_changed"):
        stream[0].registration()

def test_elapsed_window_refuses_before_payload(stream, monkeypatch):
    intake, clock, _ = stream
    reg = intake.registration()
    clock.at = reg["contract"]["windows"]["research"]["end_exclusive"]
    monkeypatch.setattr(archive, "_connection", lambda *a, **k: pytest.fail("payload read"))
    with pytest.raises(PaperError, match="window_elapsed"):
        intake.project(symbol="SPY", quantity=".09", limit_price="98", held_quantity="0",
                       expected_registration_sha256=reg["registration_sha256"])

def test_clock_rewind_refuses(stream):
    stream[1].at = OPEN - 1
    with pytest.raises(PaperError, match="clock_rewind"):
        stream[0].registration()

def test_insufficient_features_cannot_sell(stream):
    populate(stream, (100,) * 5)
    result = project(stream, held_quantity=".09")
    assert result["reason"] == "features_or_warmup_unavailable"
    assert result["candidate"] is None

def test_gap_cannot_sell(stream):
    stream[2].omit.add(OPEN + 5100)
    populate(stream)
    result = project(stream, held_quantity=".09")
    assert result["reason"] == "features_or_warmup_unavailable"
    assert result["candidate"] is None

def test_already_long_no_action(stream):
    populate(stream)
    assert project(stream, held_quantity=".09")["reason"] == "target_already_satisfied"

def test_late_revision_keeps_predecessor_and_lineage(stream):
    populate(stream)
    original = project(stream)
    intake, clock, transport = stream
    transport.prices[OPEN + 5400] = 97
    clock.at += 10
    intake.acquire()
    clock.at += 1.1
    later = project(stream)
    with archive._connection(intake._root / "observations") as db:
        state = intake._scan(db, intake.registration())
    revisions = [row for row in state["records"] if row["event_time"] == OPEN + 5700]
    assert [row["revision"] for row in revisions] == [0, 1]
    assert revisions[1]["previous_observation_hash"] == archive._sha(archive._encoded(revisions[0]))
    assert {row["acquisition_kind"] for row in revisions} == {"test"}
    if later["status"] == "CANDIDATE":
        assert later["source_signal"]["metadata"]["feature_event_time"] == OPEN + 6300
        assert later["candidate"]["signal_time"] >= original["candidate"]["signal_time"]

def test_failed_read_preserves_capture_without_partial_bars(stream):
    intake, clock, transport = stream
    clock.at = OPEN + 301
    transport.prices[OPEN] = 100
    transport.fail_path = "/v2/stocks/bars"
    result = intake.acquire()
    assert result["status"] == "ACQUISITION_FAILED" and result["appended_records"] == 0
    assert result["captures_retained"] == 1
    assert "secret remote body" not in json.dumps(result)
    with archive._connection(intake._root / "observations") as db:
        state = intake._scan(db, intake.registration())
    assert state["batch_count"] == 1 and state["records"] == []

def test_persistence_failure_never_returns_candidate(stream, monkeypatch):
    populate(stream)
    def fail(*a, **k): raise OSError("fixture disk full")
    monkeypatch.setattr(module, "_new_json", fail)
    with pytest.raises(OSError):
        project(stream)
    assert list((stream[0]._root / "receipts").iterdir()) == []

def test_receipt_capacity_fails_closed(stream, monkeypatch):
    monkeypatch.setattr(module, "MAX_RECEIPTS", 0)
    with pytest.raises(PaperError, match="receipt_capacity"):
        project(stream)

def test_symbol_binding(stream):
    with pytest.raises(PaperError, match="symbol_not_selected"):
        project(stream, symbol="GLD")

def test_unexpected_receipt_refused(stream):
    (stream[0]._root / "receipts" / "untrusted.json").write_text("{}")
    with pytest.raises(PaperError, match="receipt_layout_invalid"):
        project(stream)


@pytest.mark.parametrize("field,value", [
    ("paper_candidate_eligible", True), ("execution_authorized", True), ("live_enabled", True),
    ("provider_authenticity_verified", True), ("prospective_qualified", True),
    ("evidence_class", "PROSPECTIVE_OBSERVATION_UNVERIFIED"),
    ("account_fingerprint", "b" * 64), ("registration_sha256", "b" * 64),
    ("source_path", "owned_alpaca_https_get")])
def test_saved_receipt_cannot_promote_or_change_binding(stream, field, value):
    populate(stream)
    original = project(stream)
    path = stream[0]._root / "receipts" / original["receipt_name"]
    saved = module._read_json(path)
    saved[field] = value
    path.write_text(module.canonical_json(saved))
    with pytest.raises(PaperError, match="receipt_binding_invalid"):
        project(stream)

def test_receipt_size_bound_is_symmetric(tmp_path):
    path = tmp_path / "oversized.json"
    with pytest.raises(PaperError, match="metadata_bound"):
        module._new_json(path, {"value": "x" * 262144})
    assert not path.exists()

@pytest.mark.parametrize("kind", ["foreign", "frozen"])
def test_archive_header_and_freeze_refused_before_payload(stream, monkeypatch, kind):
    intake = stream[0]
    reg = intake.registration()
    header = {"archive_version": 1, "contract": reg["contract"], "clock_basis": "injected_test_clock"}
    if kind == "foreign":
        header = {**header, "contract": {**reg["contract"], "generation_id": "foreign"}}
    raw = archive._encoded(header)
    class Query:
        def fetchall(self): return [(1, raw, archive._sha(raw))]
        def fetchone(self): return (int(kind == "frozen"),)
    class DB:
        def execute(self, sql): return Query()
    monkeypatch.setattr(archive, "_scan", lambda *a, **k: pytest.fail("foreign or frozen payload scanned"))
    with pytest.raises(PaperError, match="archive_binding_invalid"):
        intake._scan(DB(), reg)

def test_ceil_ingestion_is_not_visible_before_real_second(stream):
    populate(stream)
    stream[1].at = OPEN + 6300.5
    result = project(stream)
    assert result["bar_version_count"] == 20
    assert result["candidate"] is None
    stream[1].at = OPEN + 6301.1
    assert project(stream)["status"] == "CANDIDATE"

def append_bundle(stream, *, zero_atr=False, include_calendar=True):
    intake, clock, transport = stream
    prices = (100, 101, 99, 101, 99, 100, 98)
    for index, price in enumerate(prices):
        for offset in range(3):
            transport.prices[OPEN + (index * 3 + offset) * 300] = price
    clock.at = OPEN + 6300.1
    reg = intake.registration()
    bundle = module.collector.collect_once(reg["contract"], provider="alpaca", start=OPEN,
        end=OPEN + 6300, transport=transport, clock=clock)
    if zero_atr:
        for row in bundle["records"]:
            if row["record_type"] == "bar":
                row["payload"].update(open="100", high="100", low="100", close="100")
    archive._append(intake._root / "observations", records=bundle["records"], captures=bundle["captures"],
        calendar_coverage=bundle["calendar_coverage"] if include_calendar else [], origin="test",
        provenance="explicit synthetic fixture", clock=clock)
    return bundle

def test_zero_atr_is_no_action_even_while_holding(stream):
    append_bundle(stream, zero_atr=True)
    stream[1].at += 1.1
    result = project(stream, held_quantity=".09")
    assert result["reason"] == "features_or_warmup_unavailable"
    assert result["candidate"] is None

def test_future_calendar_ingestion_excluded_then_usable_without_rewriting_receipts(stream):
    bundle = append_bundle(stream, include_calendar=False)
    intake, clock, _ = stream
    clock.at += 1.1
    assert project(stream)["reason"] == "calendar_coverage_unavailable"
    archive._append(intake._root / "observations", records=[], captures=bundle["captures"],
        calendar_coverage=bundle["calendar_coverage"], origin="test", provenance="synthetic delayed calendar", clock=clock)
    assert project(stream)["reason"] == "calendar_coverage_unavailable"
    clock.at += 1.1
    candidate = project(stream)
    assert candidate["status"] == "CANDIDATE"
    assert candidate["candidate"]["signal_time"] == OPEN + 6302
    with archive._connection(intake._root / "observations") as db:
        state = intake._scan(db, intake.registration())
    assert {row["available_time"] for row in state["records"]} == {OPEN + 6301}
    assert {row["ingestion_time"] for row in state["records"]} == {OPEN + 6301}
    clock.at += 10
    archive._append(intake._root / "observations", records=[], captures=bundle["captures"],
        calendar_coverage=bundle["calendar_coverage"], origin="test", provenance="repeated calendar", clock=clock)
    clock.at += 1.1
    assert project(stream)["candidate"] == candidate["candidate"]

def test_contradictory_calendar_is_rejected_in_archive_before_projection(stream):
    bundle = append_bundle(stream)
    intake, clock, _ = stream
    clock.at += 1.1
    altered = json.loads(json.dumps(bundle["calendar_coverage"]))
    altered[0]["sessions"][0]["close_time"] -= 3 * 3600
    with pytest.raises(ValueError, match="contradictory"):
        archive._append(intake._root / "observations", records=[], captures=bundle["captures"],
            calendar_coverage=altered, origin="test", provenance="synthetic contradiction", clock=clock)

def test_changed_latest_bar_produces_new_canonical_signal(stream):
    populate(stream)
    first = project(stream)
    intake, clock, transport = stream
    transport.prices[OPEN + 6000] = 97
    clock.at += 10
    intake.acquire()
    clock.at += 1.1
    new = project(stream)
    assert new["status"] == "CANDIDATE"
    assert new["candidate"]["signal_id"] != first["candidate"]["signal_id"]
    assert new["candidate"]["signal_time"] > first["candidate"]["signal_time"]
    assert new["source_signal"]["metadata"]["feature_event_time"] == OPEN + 6300

def test_historical_calendar_cannot_enter_owned_projection(stream, monkeypatch):
    populate(stream)
    intake, clock, _ = stream
    reg = intake.registration()
    # Explicitly test the owned projection's calendar-origin filter with a
    # synthetic snapshot: no actual source or account is used or authenticated.
    original_locked, original_scan = intake._locked, intake._scan
    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def __iter__(self):
            for (body,) in self.cursor:
                batch = archive._decode(body)
                batch["origin"] = "historical_import"
                yield (archive._encoded(batch),)
    class DB:
        def __init__(self, db): self.db = db
        def execute(self, sql, *args):
            cursor = self.db.execute(sql, *args)
            return Cursor(cursor) if sql == "SELECT body FROM batches ORDER BY id" else cursor
    from contextlib import contextmanager
    original_connection = archive._connection
    @contextmanager
    def connection(*args, **kwargs):
        with original_connection(*args, **kwargs) as db:
            yield DB(db)
    def scan(db, registration):
        state = original_scan(db, registration)
        for row in state["records"]:
            row["acquisition_kind"] = "prospective_observation"
        return state
    monkeypatch.setattr(intake, "_registration", lambda: reg)
    monkeypatch.setattr(intake, "_scan", scan)
    monkeypatch.setattr(archive, "_connection", connection)
    # _scan uses the real TEST archive before this owned-mode filter; restore
    # testing temporarily there to retain honest archive fixture validation.
    def owned_scan(db, registration):
        intake._testing = True
        try: return scan(db, registration)
        finally: intake._testing = False
    monkeypatch.setattr(intake, "_scan", owned_scan)
    intake._testing = False
    result = project(stream)
    assert result["reason"] == "calendar_coverage_unavailable"
    assert result["paper_candidate_eligible"] is False
    assert result["calendar_lineage_counts"] == {"historical_import": 21}


def test_closed_session_refuses_even_fresh_final_bar(stream):
    intake, clock, transport = stream
    closing = OPEN + 23400
    for index, price in enumerate((100, 101, 99, 101, 99, 100, 98)):
        for offset in range(3):
            transport.prices[closing - 6300 + (index * 3 + offset) * 300] = price
    clock.at = closing + .1
    bundle = module.collector.collect_once(intake.registration()["contract"], provider="alpaca",
        start=closing - 6300, end=closing, transport=transport, clock=clock)
    archive._append(intake._root / "observations", records=bundle["records"], captures=bundle["captures"],
        calendar_coverage=bundle["calendar_coverage"], origin="test", provenance="synthetic close", clock=clock)
    clock.at += 1.1
    result = project(stream)
    assert result["reason"] == "regular_session_closed"
    assert result["candidate"] is None

def test_acquisition_report_derives_classes_from_actual_archive_lineage(stream, monkeypatch):
    intake, clock, transport = stream
    clock.at = OPEN + 301
    transport.prices[OPEN] = 100
    original = archive._append
    def appended(*args, **kwargs):
        result = original(*args, **kwargs)
        # Simulated archive summary: the reporting code must not substitute its
        # runtime role for the actual admitted lineage supplied by the store.
        return {**result, "acquisition_kinds": {"test": 0, "historical_backfill": 1, "prospective_observation": 0}}
    monkeypatch.setattr(archive, "_append", appended)
    result = intake.acquire()
    assert result["evidence_classes"] == ["HISTORICAL_UNVERIFIED"]
    assert result["lineage_scope"] == "archive_after_acquisition"
    assert result["lineage_counts"]["historical_backfill"] == 1

def test_metadata_source_arrays_hash_without_large_json_object_limit():
    rows = [{"item": "x" * 100} for _ in range(module.MAX_INPUT_BARS)]
    assert len(module._sequence_digest(rows)) == 64
    assert module._sequence_digest(rows[:-1]) != module._sequence_digest(rows)


@pytest.mark.parametrize("field,value", [
    ("archive_head_sha256", "b" * 64), ("as_of", OPEN + 6299),
    ("observed_at", OPEN + 6299.9), ("bar_version_count", 1),
    ("lineage_counts", {"prospective_observation": 21}),
    ("calendar_lineage_counts", {"builtin_get": 21})])
def test_saved_snapshot_metadata_must_reconstruct_from_archive(stream, field, value):
    populate(stream)
    original = project(stream)
    path = stream[0]._root / "receipts" / original["receipt_name"]
    saved = module._read_json(path)
    saved[field] = value
    path.write_text(module.canonical_json(saved))
    with pytest.raises(PaperError, match="receipt_evidence_invalid"):
        project(stream)

def test_self_consistent_forged_evidence_digest_cannot_replay(stream):
    populate(stream)
    original = project(stream)
    path = stream[0]._root / "receipts" / original["receipt_name"]
    saved = module._read_json(path)
    saved["evidence"] = {"registration_sha256": saved["registration_sha256"]}
    saved["candidate"]["data_sha256"] = digest(saved["evidence"])
    path.write_text(module.canonical_json(saved))
    with pytest.raises(PaperError, match="receipt_evidence_invalid"):
        project(stream)


def test_signal_that_expires_during_projection_is_no_action(stream, monkeypatch):
    populate(stream)
    original = module.signals_for
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        stream[1].at += 301
        return result
    monkeypatch.setattr(module, "signals_for", slow)
    result = project(stream)
    assert result["reason"] == "original_signal_stale"
    assert result["candidate"] is None


def test_candidate_expiring_during_receipt_commit_is_not_returned(stream, monkeypatch):
    populate(stream)
    original = module._new_json
    def slow(path, value):
        original(path, value)
        stream[1].at += 301
    monkeypatch.setattr(module, "_new_json", slow)
    with pytest.raises(PaperError, match="expired_during_persistence"):
        project(stream)
    assert len(list((stream[0]._root / "receipts").iterdir())) == 1


def test_older_aggregate_revision_does_not_refresh_latest_signal(stream):
    populate(stream)
    original = project(stream)
    intake, clock, _ = stream
    with archive._connection(intake._root / "observations") as db:
        state = intake._scan(db, intake.registration())
    row = json.loads(json.dumps(state["records"][0]))
    clock.at += 10
    raw = archive._encoded({"fixture": "older correction"})
    row["payload"].update(open="100.1", high="100.15", low="100.05", close="100.1")
    row.update(available_time=int(clock.at) + 1, ingestion_time=int(clock.at) + 1,
               raw_capture_sha256=archive._sha(raw))
    capture = {"role": "bars", "request_url": "https://data.alpaca.markets/v2/stocks/bars",
               "received_at": int(clock.at) + 1, "body": raw}
    archive._append(intake._root / "observations", records=[row], captures=[capture],
        calendar_coverage=[], origin="test", provenance="explicit older correction fixture", clock=clock)
    clock.at += 1.1
    replay = project(stream)
    assert replay["candidate"] == original["candidate"]
    assert replay["source_signal"] == original["source_signal"]
    assert replay["receipt_sha256"] == original["receipt_sha256"]
