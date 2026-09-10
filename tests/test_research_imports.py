"""Generated provider-shaped fixtures only; no broker or market evaluation."""

import hashlib
import json
import os
import socket

import pytest

from atlas.config import ConfigError
from atlas.research.imports import convert_coinbase_candles, import_coinbase_candles, verify_import_archive
from atlas.research.market import DatasetError, load_dataset


START = 1735689600
CAPTURE = START + 10000
PARAMETERS = {"product_id": "BTC-USD", "granularity": 60, "captured_at": CAPTURE,
              "source_kind": "test", "now": CAPTURE}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline importer must not use network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def rows(count=5):
    return [[START + i * 60, 99, 102, 100, 101, 10] for i in range(count)]


def raw(value=None):
    return json.dumps(rows() if value is None else value).encode()


def convert(value=None, **changes):
    return convert_coinbase_candles(raw(value), **(PARAMETERS | changes))


def test_default_capture_only_archive_has_no_historical_availability_claim():
    result = convert()
    dataset, receipt, quality = (result[k] for k in ("dataset", "receipt", "quality"))
    assert dataset["availability_basis"] == "unknown"
    assert dataset["revision_policy"] == "latest_only"
    assert dataset["volume_unit"] == "unknown"
    assert {r["information_available_time"] for r in dataset["bars"]} == {CAPTURE}
    assert {r["ingestion_time"] for r in dataset["bars"]} == {CAPTURE}
    assert quality["evidence_class"] == "TEST"
    assert quality["provider_authenticity_verified"] is quality["alpha_validation_eligible"] is False
    assert {"historical_availability_not_established", "point_in_time_revision_history_not_established",
            "volume_unit_unknown_no_fill_capacity_assumption"} <= set(quality["blockers"])
    assert receipt["raw_sha256"] == hashlib.sha256(raw()).hexdigest()
    assert "not proof of original" in receipt["revision_zero_meaning"]
    assert receipt["capture_time_basis"] == "operator_declared_not_independently_verified"
    assert dataset["source"]["content_sha256"] == receipt["raw_sha256"]


@pytest.mark.parametrize("kind,evidence", [("real", "HISTORICAL_UNVERIFIED"), ("unknown", "HISTORICAL_UNVERIFIED"),
                                           ("test", "TEST"), ("synthetic", "SYNTHETIC")])
def test_explicit_assumptions_never_authenticate_provider(kind, evidence):
    result = convert(source_kind=kind, assumed_publication_lag_seconds=60, volume_unit="base_asset")
    assert result["dataset"]["availability_basis"] == "assumed"
    assert result["dataset"]["bars"][0]["information_available_time"] == START + 120
    assert result["quality"]["evidence_class"] == evidence
    assert result["quality"]["alpha_validation_eligible"] is False
    assert "historical_availability_not_established" in result["quality"]["blockers"]
    assert "assumes_base_asset" in result["receipt"]["volume_policy"]


def test_exact_decimal_values_avoid_float_round_trip():
    content = ("[[%s,99.123456789012345678901234567890123456,102,100,"
               "101.000000000000000000000000000000000001,1.234567890123456789e-10]]" % START).encode()
    result = convert_coinbase_candles(content, **PARAMETERS)
    bar = result["dataset"]["bars"][0]
    assert bar["low"] == "99.123456789012345678901234567890123456"
    assert bar["close"] == "101.000000000000000000000000000000000001"
    assert bar["volume"] == "0.0000000001234567890123456789"


def test_descending_input_has_explicit_reversal_and_identical_canonical_prices():
    ascending = convert()
    descending = convert(list(reversed(rows())))
    assert descending["receipt"]["input_order"] == "descending"
    assert descending["receipt"]["included_raw_row_indices"] == [4, 3, 2, 1, 0]
    assert "reverse_descending_rows" in descending["receipt"]["transformations"]
    assert descending["dataset"]["bars"] == ascending["dataset"]["bars"]
    assert descending["receipt"]["raw_sha256"] != ascending["receipt"]["raw_sha256"]


def test_explicit_window_records_every_excluded_row_and_keeps_boundary_end():
    result = convert(request_start=START + 60, request_end=START + 240)
    assert [b["event_time"] for b in result["dataset"]["bars"]] == [START + 120, START + 180, START + 240]
    assert result["receipt"]["included_raw_row_indices"] == [1, 2, 3]
    assert result["receipt"]["excluded_rows"] == [
        {"raw_row_index": 0, "opening_time": START, "reasons": ["opens_before_requested_start"]},
        {"raw_row_index": 4, "opening_time": START + 240, "reasons": ["closes_after_requested_end"]}]


def test_incomplete_candle_refuses_unless_explicitly_outside_requested_window():
    with pytest.raises(DatasetError, match="incomplete"):
        convert(captured_at=START + 270)
    result = convert(captured_at=START + 270, request_start=START, request_end=START + 240)
    assert result["receipt"]["retained_row_count"] == 4
    assert result["receipt"]["excluded_rows"][0]["raw_row_index"] == 4


def test_gap_remains_visible_and_is_never_filled():
    value = rows(); del value[2]
    result = convert(value)
    assert len(result["dataset"]["bars"]) == 4
    assert result["quality"]["gaps"][0]["missing_bars"] == 1
    assert "missing_expected_bars" in result["quality"]["blockers"]


def test_requested_window_detects_missing_edges_without_interior_gaps():
    result = convert(rows()[1:-1], request_start=START, request_end=START + 300)
    coverage = result["receipt"]["requested_window_coverage"]
    assert coverage["expected_aligned_buckets"] == 5
    assert coverage["retained_buckets"] == 3
    assert coverage["missing_leading_buckets"] == coverage["missing_trailing_buckets"] == 1
    assert coverage["missing_interior_buckets"] == 0
    assert coverage["missing_buckets"] == 2
    assert coverage["coverage_bounds_complete"] is coverage["all_expected_buckets_present"] is False
    assert not result["quality"]["gaps"]
    assert "requested_window_incomplete" in result["quality"]["blockers"]
    assert result["quality"]["requested_window_coverage"] == coverage
    assert "absent 2" in result["dataset"]["description"]


def test_requested_window_separates_missing_interior_from_complete_bounds():
    sample = rows(); del sample[2]
    result = convert(sample, request_start=START, request_end=START + 300)
    coverage = result["receipt"]["requested_window_coverage"]
    assert coverage["coverage_bounds_complete"] is True
    assert coverage["all_expected_buckets_present"] is False
    assert coverage["missing_interior_buckets"] == 1
    assert "requested_window_incomplete" in result["quality"]["blockers"]
    complete = convert(request_start=START, request_end=START + 300)
    assert complete["receipt"]["requested_window_coverage"]["all_expected_buckets_present"] is True
    assert "requested_window_incomplete" not in complete["quality"]["blockers"]


@pytest.mark.parametrize("value", [[], {}, {"message": "rate limit"}, rows(301),
                                   [[START, 99, 102, 100, 101]], [[START, 99, 102, 100, 101, 10, 11]],
                                   [rows()[0], rows()[0]], [rows()[1], rows()[0], rows()[2]]])
def test_invalid_response_shapes_duplicate_and_mixed_order_refuse(value):
    with pytest.raises(DatasetError):
        convert(value)


@pytest.mark.parametrize("index,value", [(0, True), (0, START + 1), (0, 1.5), (0, -1),
                                        (1, -1), (2, 100), (3, 0), (4, True), (4, "101"),
                                        (4, float("nan")), (4, float("inf")), (5, -1)])
def test_invalid_raw_fields_refuse_even_in_excluded_rows(index, value):
    sample = rows(); sample[0][index] = value
    with pytest.raises(DatasetError):
        convert(sample, request_start=START + 60, request_end=START + 300)


@pytest.mark.parametrize("changes", [{"product_id": "BTC-USDT"}, {"product_id": "USD-USD"},
                                    {"product_id": "btc-usd"}, {"product_id": "BTC-USD/candles"},
                                    {"granularity": True}, {"granularity": 30}, {"captured_at": CAPTURE + 1},
                                    {"captured_at": True}, {"request_start": START},
                                    {"request_start": START + 60, "request_end": START},
                                    {"request_start": START, "request_end": CAPTURE + 1},
                                    {"request_start": START + 1, "request_end": START + 300},
                                    {"request_start": START, "request_end": START + 299},
                                    {"assumed_publication_lag_seconds": True}, {"assumed_publication_lag_seconds": -1},
                                    {"assumed_publication_lag_seconds": 86401}, {"volume_unit": "shares"},
                                    {"source_kind": "verified"}])
def test_invalid_explicit_metadata_refuses(changes):
    with pytest.raises(DatasetError):
        convert(**changes)


def test_assumed_publication_must_have_occurred_by_capture():
    with pytest.raises(DatasetError, match="publication time"):
        convert(captured_at=START + 300, assumed_publication_lag_seconds=1)


def test_midnight_close_belongs_to_prior_utc_session():
    result = convert([[START - 60, 99, 102, 100, 101, 10]])
    assert result["dataset"]["bars"][0]["event_time"] == START
    assert result["dataset"]["bars"][0]["session_date"] == "2024-12-31"


def test_declared_url_is_bound_to_product_interval_and_window():
    url = ("https://api.exchange.coinbase.com/products/BTC-USD/candles?"
           "start=2025-01-01T00:00:00Z&end=2025-01-01T00:05:00%2B00:00&granularity=60")
    result = convert(request_start=START, request_end=START + 300, request_url=url)
    assert result["receipt"]["parameters"]["request_url"] == url
    assert result["dataset"]["source"]["url"] == url
    invalid = [url.replace("BTC-USD", "ETH-USD"), url.replace("granularity=60", "granularity=300"),
               url + "&granularity=60", url + "&extra=1", url + "#fragment", url.replace("https:", "http:"),
               url.replace("api.exchange.coinbase.com", "other.example"),
               url.replace("00:05:00", "00:06:00"), url.replace("00:00:00Z", "00:00:00")]
    for candidate in invalid:
        with pytest.raises(DatasetError, match="URL"):
            convert(request_start=START, request_end=START + 300, request_url=candidate)


@pytest.mark.parametrize("value", [b"", b"[", b"\xff", b'{"x": 1, "x": 2}', b'[[0, 1e999999999999999999999, 2, 1, 1, 1]]'])
def test_malformed_or_unbounded_json_numbers_refuse(value):
    with pytest.raises(DatasetError):
        convert_coinbase_candles(value, **PARAMETERS)


def test_raw_size_bound(monkeypatch):
    monkeypatch.setattr("atlas.research.imports.MAX_RAW_BYTES", 10)
    with pytest.raises(DatasetError):
        convert()


def archive(tmp_path):
    source = tmp_path / "response.json"; source.write_bytes(raw())
    destination = tmp_path / "archive"
    result = import_coinbase_candles(input_path=source, output_dir=destination, **PARAMETERS)
    return source, destination, result


def test_archive_round_trip_retains_exact_raw_and_reloadable_schema(tmp_path):
    source, destination, result = archive(tmp_path)
    assert (destination / "raw.json").read_bytes() == source.read_bytes()
    assert result["integrity_verified"] is True
    assert result["experiment_run"] is result["trade_submitted"] is False
    assert set(result["paths"]) == {"raw.json", "dataset.json", "quality.json", "receipt.json"}
    assert load_dataset(destination / "dataset.json", now=CAPTURE)["evidence_class"] == "TEST"
    verified = verify_import_archive(destination, now=CAPTURE + 100)
    assert verified["integrity_verified"] is True
    assert verified["provider_authenticity_verified"] is False
    assert verified["retained_row_count"] == 5
    with pytest.raises(DatasetError, match="overwrite"):
        import_coinbase_candles(input_path=source, output_dir=destination, **PARAMETERS)
    assert (destination / "raw.json").read_bytes() == source.read_bytes()


@pytest.mark.parametrize("filename", ["raw.json", "dataset.json", "quality.json"])
def test_archive_tampering_fails_integrity(tmp_path, filename):
    _, destination, _ = archive(tmp_path)
    with (destination / filename).open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(DatasetError, match="digest"):
        verify_import_archive(destination, now=CAPTURE)


def test_forged_manifest_hash_cannot_disguise_changed_conversion(tmp_path):
    _, destination, _ = archive(tmp_path)
    dataset = destination / "dataset.json"
    data = json.loads(dataset.read_bytes()); data["bars"][0]["close"] = "100.5"
    dataset.write_text(json.dumps(data))
    receipt = json.loads((destination / "receipt.json").read_bytes())
    content = dataset.read_bytes()
    receipt["files"]["dataset.json"] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    (destination / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(DatasetError, match="conversion"):
        verify_import_archive(destination, now=CAPTURE)


def test_receipt_assumption_tamper_is_detected(tmp_path):
    _, destination, _ = archive(tmp_path)
    receipt = json.loads((destination / "receipt.json").read_bytes())
    receipt["provider_authenticity_verified"] = True
    (destination / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(DatasetError, match="receipt integrity"):
        verify_import_archive(destination, now=CAPTURE)


def test_invalid_input_never_creates_archive(tmp_path):
    source = tmp_path / "bad.json"; source.write_bytes(b"[]")
    destination = tmp_path / "archive"
    with pytest.raises(DatasetError):
        import_coinbase_candles(input_path=source, output_dir=destination, **PARAMETERS)
    assert not destination.exists()


def test_symlinks_special_files_and_repository_destinations_refuse(tmp_path):
    source = tmp_path / "raw.json"; source.write_bytes(raw())
    linked = tmp_path / "linked.json"; linked.symlink_to(source)
    with pytest.raises(DatasetError, match="symlink"):
        import_coinbase_candles(input_path=linked, output_dir=tmp_path / "one", **PARAMETERS)
    directory_link = tmp_path / "linked-directory"; directory_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DatasetError, match="symlink"):
        import_coinbase_candles(input_path=source, output_dir=directory_link / "two", **PARAMETERS)
    fifo = tmp_path / "fifo"; os.mkfifo(fifo)
    with pytest.raises(DatasetError, match="regular file"):
        import_coinbase_candles(input_path=fifo, output_dir=tmp_path / "three", **PARAMETERS)
    with pytest.raises((DatasetError, ConfigError), match="outside the repository"):
        import_coinbase_candles(input_path=source, output_dir=__file__ + ".archive", **PARAMETERS)
    assert not any((tmp_path / n).exists() for n in ("one", "two", "three"))


def test_failed_write_removes_only_new_incomplete_archive(tmp_path, monkeypatch):
    source = tmp_path / "raw.json"; source.write_bytes(raw())
    def fail(*args):
        raise OSError("synthetic fsync failure")
    monkeypatch.setattr("atlas.research.imports.os.fsync", fail)
    destination = tmp_path / "archive"
    with pytest.raises(OSError, match="synthetic"):
        import_coinbase_candles(input_path=source, output_dir=destination, **PARAMETERS)
    assert not destination.exists()
    assert source.read_bytes() == raw()
