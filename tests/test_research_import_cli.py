"""Offline importer CLI integration using explicit, generated external files."""

import json
import socket
import sys
from types import ModuleType

import pytest

import atlas.__main__ as entrypoint
import atlas.config as config
from atlas.brokers.paper import PaperBroker
from atlas.brokers.robinhood_agentic import RobinhoodAgentic
from atlas.brokers.robinhood_crypto import RobinhoodCrypto
from atlas.research.market import load_dataset
from atlas.service import AtlasService


OPEN_TIME = 1735689600
CAPTURED_AT = OPEN_TIME + 600


@pytest.fixture(autouse=True)
def no_network_research_or_broker_execution(monkeypatch):
    attempted = []

    def refuse(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("an import must not contact a service, evaluate research, or execute orders")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, refuse)
    lab = ModuleType("atlas.research.lab")
    for name in ("run_research", "evaluate", "reproduce", "synthetic_demo"):
        setattr(lab, name, refuse)
    monkeypatch.setitem(sys.modules, "atlas.research.lab", lab)
    monkeypatch.setattr(entrypoint, "build_service", refuse)
    monkeypatch.setattr(config, "build_service", refuse)
    monkeypatch.setattr(AtlasService, "__init__", refuse)
    for broker in (PaperBroker, RobinhoodAgentic, RobinhoodCrypto):
        monkeypatch.setattr(broker, "submit_order", refuse)
        monkeypatch.setattr(broker, "cancel_order", refuse)
    yield
    assert not attempted, "even failed imports must remain offline and never run research or orders"


@pytest.fixture
def raw_export(tmp_path):
    path = tmp_path / "coinbase-response.json"
    # Coinbase's six-column response order is time, low, high, open, close, volume.
    # Descending order exercises explicit import ordering without missing candles.
    rows = [[OPEN_TIME + 120, 100, 105, 102, 104, 3],
            [OPEN_TIME + 60, 99, 104, 101, 102, 2],
            [OPEN_TIME, 98, 103, 100, 101, 1]]
    path.write_text(json.dumps(rows) + "\n", encoding="utf-8")
    return path


def import_args(raw_export, archive, *extra):
    return ["research", "import-coinbase", "--input", str(raw_export),
            "--output", str(archive), "--product-id", "BTC-USD",
            "--granularity", "60", "--captured-at", str(CAPTURED_AT),
            *extra]


def read_success(capsys):
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


def test_cli_archives_explicit_coinbase_export_and_verifies_without_services(raw_export, tmp_path, capsys):
    archive = tmp_path / "imported"
    assert entrypoint.main(import_args(raw_export, archive, "--source-kind", "test")) == 0
    imported = read_success(capsys)
    assert archive.is_dir()
    assert (archive / "raw.json").read_bytes() == raw_export.read_bytes()
    assert (archive / "receipt.json").is_file()
    assert (archive / "dataset.json").is_file()
    assert (archive / "quality.json").is_file()
    assert imported["trade_submitted"] is False
    assert imported["experiment_run"] is False
    assert imported["integrity_verified"] is True
    assert entrypoint.main(["research", "verify-import", "--archive", str(archive)]) == 0
    verified = read_success(capsys)
    assert verified["integrity_verified"] is True
    assert verified["raw_sha256"] == imported["raw_sha256"]
    assert verified["evidence_class"] == "TEST"
    assert verified["retained_row_count"] == 3
    assert verified["provider_authenticity_verified"] is False


def test_cli_default_availability_and_volume_remain_unknown_for_test_export(raw_export, tmp_path, capsys):
    archive = tmp_path / "unknown-availability"
    assert entrypoint.main(import_args(raw_export, archive, "--source-kind", "test")) == 0
    read_success(capsys)
    dataset = load_dataset(archive / "dataset.json", now=CAPTURED_AT)
    assert dataset["source"]["kind"] == "test"
    assert dataset["evidence_class"] == "TEST"
    assert dataset["availability_basis"] == "unknown"
    assert dataset["volume_unit"] == "unknown"
    assert {bar["information_available_time"] for bar in dataset["bars"]} == {CAPTURED_AT}
    assert {bar["ingestion_time"] for bar in dataset["bars"]} == {CAPTURED_AT}
    assert [bar["event_time"] for bar in dataset["bars"]] == [OPEN_TIME + 60, OPEN_TIME + 120, OPEN_TIME + 180]
    assert dataset["bars"][0]["open"] == "100"
    assert dataset["bars"][0]["low"] == "98"
    assert dataset["bars"][0]["high"] == "103"
    assert dataset["bars"][0]["close"] == "101"
    assert "historical_availability_not_established" in dataset["quality"]["blockers"]
    assert "volume_unit_unknown_no_fill_capacity_assumption" in dataset["quality"]["blockers"]
    assert dataset["quality"]["alpha_validation_eligible"] is False


def test_cli_explicit_publication_lag_and_volume_do_not_authenticate_declared_real_export(raw_export, tmp_path, capsys):
    archive = tmp_path / "assumed-availability"
    assert entrypoint.main(import_args(raw_export, archive, "--source-kind", "real",
                                      "--assumed-publication-lag-seconds", "5",
                                      "--volume-unit", "base_asset")) == 0
    read_success(capsys)
    dataset = load_dataset(archive / "dataset.json", now=CAPTURED_AT)
    assert dataset["availability_basis"] == "assumed"
    assert dataset["volume_unit"] == "base_asset"
    assert all(bar["information_available_time"] == bar["event_time"] + 5 for bar in dataset["bars"])
    assert {bar["ingestion_time"] for bar in dataset["bars"]} == {CAPTURED_AT}
    assert dataset["evidence_class"] == "HISTORICAL_UNVERIFIED"
    assert dataset["quality"]["provider_authenticity_verified"] is False
    assert dataset["quality"]["alpha_validation_eligible"] is False
    assert "historical_availability_not_established" in dataset["quality"]["blockers"]
    assert "point_in_time_revision_history_not_established" in dataset["quality"]["blockers"]


def test_cli_refuses_existing_archive_without_overwriting(raw_export, tmp_path, capsys):
    archive = tmp_path / "already-exists"
    archive.mkdir()
    marker = archive / "keep.txt"
    marker.write_text("existing archive content", encoding="utf-8")
    assert entrypoint.main(import_args(raw_export, archive, "--source-kind", "test")) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["operation_completed"] is False
    assert marker.read_text(encoding="utf-8") == "existing archive content"
    assert list(archive.iterdir()) == [marker]


def test_cli_verification_rejects_corrupt_raw_bytes(raw_export, tmp_path, capsys):
    archive = tmp_path / "corrupted"
    assert entrypoint.main(import_args(raw_export, archive, "--source-kind", "test")) == 0
    read_success(capsys)
    raw = archive / "raw.json"
    raw.write_bytes(raw.read_bytes() + b" ")
    assert entrypoint.main(["research", "verify-import", "--archive", str(archive)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["operation_completed"] is False
