"""Offline campaign planning CLI checks; no strategy evaluation or providers."""

import json
import os
from pathlib import Path
import socket

import pytest

import atlas.research.multiasset_lab as lab
import scripts.multiasset_research as cli
from atlas.research.campaign_preflight import DAY
from atlas.research.data_adapters import archive_historical_bundle
from test_multiasset_data_adapters import NOW, normalize


@pytest.fixture(autouse=True)
def no_network_or_evaluation(monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("planning must not use network, signals, evaluation or output writers")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, refuse)
    for target, name in ((cli, "run_campaign"), (lab, "write_json"),
                         (lab, "run_campaign"), (lab, "evaluate"), (lab, "signals_for"),
                         (lab.ReplayEngine, "__init__"), (lab.MultiassetReplay, "__init__")):
        monkeypatch.setattr(target, name, refuse)
    monkeypatch.setattr(cli.time, "time", lambda: NOW + 100)
    yield
    assert attempts == [], "a rejected action still violates planning-only behavior"


@pytest.fixture
def archive(tmp_path):
    target = tmp_path / "generated-archive"
    archive_historical_bundle(normalize(), target)
    return target


def plan_file(tmp_path, plan=None, *, name="plan.json"):
    path = tmp_path / name
    path.write_text(json.dumps(lab.default_plan() if plan is None else plan), encoding="utf-8")
    return path


def read_success(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def assert_cli_error(capsys, args):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error:" in captured.err


def files_at(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_default_synthetic_preflight_emits_only_planning_receipt(tmp_path, capsys):
    before = files_at(tmp_path)
    assert cli.main(["--synthetic", "--preflight"]) == 0
    output = read_success(capsys)
    result = output["preflight"]
    assert output["mode"] == "planning_only"
    assert output["evidence_class"] == "SYNTHETIC"
    assert output["actual_completed_trades"] is None
    assert output["actual_orders_submitted"] == 0
    assert output["real_money_allocated"] == "0"
    assert output["fold_count"] == len(output["folds"])
    assert output["instruments"]
    assert result["maximum_total_replays"] == 3733
    assert result["research_purpose"] == "diagnostic"
    assert result["splits"]["train"]["elapsed_seconds"] == 2 * DAY
    assert result["splits"]["validation"]["elapsed_seconds"] == 2 * DAY
    assert result["splits"]["test"]["elapsed_seconds"] == 2 * DAY
    assert result["qualification_established"] is False
    assert result["live_approved"] is False
    assert files_at(tmp_path) == before


def test_explicit_qualification_plan_is_preserved_and_cannot_establish_qualification(tmp_path, capsys):
    plan = lab.default_plan(train_days=60, validation_days=60, test_days=60, max_folds=1)
    plan.update(research_purpose="qualification", max_replay_evaluations=5000)
    source = plan_file(tmp_path, plan)
    original = source.read_bytes()
    assert cli.main(["--synthetic", "--preflight", "--plan", str(source)]) == 0
    output = read_success(capsys)
    result = output["preflight"]
    assert result["research_purpose"] == "qualification"
    assert result["maximum_total_replays"] == 1245
    assert result["warnings"] == []
    assert result["splits"]["test"]["elapsed_seconds"] == 60 * DAY
    assert result["splits"]["test"]["actual_completed_trades"] is None
    assert result["qualification_established"] is False
    assert output["qualification_established"] is False
    assert output["plan_hash"] == lab.digest(plan)
    assert output["fold_count"] == 0
    assert source.read_bytes() == original
    assert list(tmp_path.iterdir()) == [source]


def test_plan_compatibility_allows_omitted_optional_purpose_and_budget(tmp_path, capsys):
    plan = lab.default_plan()
    del plan["research_purpose"], plan["max_replay_evaluations"]
    source = plan_file(tmp_path, plan)
    assert cli.main(["--synthetic", "--preflight", "--plan", str(source)]) == 0
    result = read_success(capsys)["preflight"]
    assert result["research_purpose"] == "diagnostic"
    assert result["max_replay_evaluations"] == 5000
    assert result["splits"]["train"]["elapsed_seconds"] == 10 * DAY


@pytest.mark.parametrize("option", ["--train-days", "--validation-days", "--test-days", "--max-folds"])
def test_plan_and_legacy_override_are_mutually_exclusive(tmp_path, capsys, monkeypatch, option):
    source = plan_file(tmp_path)
    def no_read(*args, **kwargs):
        raise AssertionError("conflicting CLI options must refuse before plan loading")
    monkeypatch.setattr(cli, "load_plan", no_read)
    assert_cli_error(capsys, ["--synthetic", "--preflight", "--plan", str(source), option, "1"])


@pytest.mark.parametrize("option", ["--train-days", "--validation-days", "--test-days", "--max-folds", "--synthetic-days"])
def test_explicit_zero_is_not_replaced_by_default(tmp_path, capsys, option):
    assert_cli_error(capsys, ["--synthetic", "--preflight", option, "0"])
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("args", [
    ["--synthetic"],
    ["--synthetic", "--preflight", "--assume-archive-close"],
    ["--archive", "/not-read", "--preflight", "--synthetic-days", "12"],
    ["--synthetic", "--archive", "/not-read", "--preflight"],
    ["--preflight"],
])
def test_invalid_option_combinations_refuse_before_any_data_read(capsys, monkeypatch, args):
    def no_read(*args, **kwargs):
        raise AssertionError("invalid CLI arguments must refuse before data access")
    monkeypatch.setattr(cli, "verify_historical_archive", no_read)
    monkeypatch.setattr(cli, "synthetic_datasets", no_read)
    assert_cli_error(capsys, args)


def test_output_directory_is_forbidden_for_preflight_and_not_created(tmp_path, capsys):
    target = tmp_path / "must-not-exist"
    assert_cli_error(capsys, ["--synthetic", "--preflight", "--output-dir", str(target)])
    assert not target.exists()


@pytest.mark.parametrize("mutation", [
    lambda p: p.pop("seed"),
    lambda p: p.update(unrecognized_field="refuse"),
    lambda p: p.update(live_approved=True),
    lambda p: p.update(initial_capital_usd="1000"),
    lambda p: p.update(max_replay_evaluations=1),
    lambda p: p.update(train_seconds=0),
])
@pytest.mark.parametrize("preflight", [True, False])
def test_invalid_explicit_plan_refuses_before_archive_or_output(tmp_path, capsys, monkeypatch, mutation, preflight):
    plan = lab.default_plan()
    mutation(plan)
    source = plan_file(tmp_path, plan)
    target = tmp_path / "must-not-exist"
    def no_read(*args, **kwargs):
        raise AssertionError("invalid plan must refuse before archive verification")
    monkeypatch.setattr(cli, "verify_historical_archive", no_read)
    mode = ["--preflight"] if preflight else ["--output-dir", str(target)]
    assert_cli_error(capsys, ["--archive", str(tmp_path / "not-read"), "--plan", str(source), *mode])
    assert not target.exists()


@pytest.mark.parametrize("body", [
    b'{"version":"first","version":"second"}',
    b'{"seed":NaN}', b'{"seed":Infinity}', b'{"seed":-Infinity}',
    b'{"nested":{"x":1,"x":2}}', b'[]', b'null', b'{invalid}', b'\xff',
    b' ' * (256 * 1024 + 1),
])
def test_strict_json_refuses_before_archive_access(tmp_path, capsys, monkeypatch, body):
    source = tmp_path / "bad-plan.json"
    source.write_bytes(body)
    def no_read(*args, **kwargs):
        raise AssertionError("invalid JSON plan must refuse before archive read")
    monkeypatch.setattr(cli, "verify_historical_archive", no_read)
    assert_cli_error(capsys, ["--archive", "/not-read", "--preflight", "--plan", str(source)])
    assert source.read_bytes() == body


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_keys_in_an_otherwise_valid_plan_are_not_silently_overwritten(tmp_path, capsys, nested):
    plan = lab.default_plan()
    body = json.dumps(plan)
    if nested:
        original = '"min_daily_observations": 60'
        assert original in body
        body = body.replace(original, original + ', "min_daily_observations": 60', 1)
    else:
        body = body[:-1] + ', "seed": 1729}'
    source = tmp_path / "duplicated.json"
    source.write_text(body)
    assert_cli_error(capsys, ["--synthetic", "--preflight", "--plan", str(source)])


def test_plan_file_exact_size_limit_accepts_then_one_extra_byte_refuses(tmp_path, capsys):
    plan = lab.default_plan()
    body = json.dumps(plan).encode()
    limit = 256 * 1024
    assert len(body) < limit
    source = tmp_path / "bounded.json"
    source.write_bytes(body + b" " * (limit - len(body)))
    assert cli.load_plan(source) == plan
    source.write_bytes(source.read_bytes() + b" ")
    assert_cli_error(capsys, ["--synthetic", "--preflight", "--plan", str(source)])


@pytest.mark.parametrize("kind", ["relative", "repository", "symlink", "parent_symlink", "directory", "fifo", "missing"])
def test_plan_path_must_be_external_regular_non_symlink_file(tmp_path, capsys, kind):
    regular = plan_file(tmp_path)
    if kind == "relative":
        source = Path("relative-plan.json")
    elif kind == "repository":
        source = Path(cli.__file__).resolve()
    elif kind == "symlink":
        source = tmp_path / "linked-plan.json"
        source.symlink_to(regular)
    elif kind == "parent_symlink":
        directory = tmp_path / "real-directory"
        directory.mkdir()
        regular.rename(directory / regular.name)
        link = tmp_path / "linked-directory"
        link.symlink_to(directory, target_is_directory=True)
        source = link / regular.name
    elif kind == "directory":
        source = tmp_path / "directory"
        source.mkdir()
    elif kind == "fifo":
        source = tmp_path / "fifo"
        os.mkfifo(source)
    else:
        source = tmp_path / "missing.json"
    assert_cli_error(capsys, ["--synthetic", "--preflight", "--plan", str(source)])


def test_default_archive_preflight_verifies_generated_capture_without_mutation(archive, tmp_path, capsys):
    before = files_at(tmp_path)
    assert cli.main(["--archive", str(archive), "--preflight"]) == 0
    output = read_success(capsys)
    result = output["preflight"]
    assert result["maximum_total_replays"] == 3733
    assert result["splits"]["train"]["elapsed_seconds"] == 10 * DAY
    assert result["splits"]["validation"]["elapsed_seconds"] == 5 * DAY
    assert result["qualification_established"] is False
    assert output["archive_integrity"]["integrity_verified"] is True
    assert output["archive_integrity"]["provider_authenticity_verified"] is False
    assert output["fold_count"] == 0
    assert output["actual_completed_trades"] is None
    assert files_at(tmp_path) == before


@pytest.mark.parametrize("name", ["raw-000.json", "dataset.json", "receipt.json"])
def test_archive_tamper_refuses_without_output(archive, tmp_path, capsys, name):
    target = archive / name
    if name == "receipt.json":
        target.write_text('{"not":"an archive receipt"}')
    else:
        target.write_bytes(target.read_bytes() + b" ")
    before = files_at(tmp_path)
    assert_cli_error(capsys, ["--archive", str(archive), "--preflight"])
    assert files_at(tmp_path) == before


def test_archive_dataset_replaced_after_verification_is_rejected(archive, capsys, monkeypatch):
    verify = cli.verify_historical_archive
    called = []
    def verify_then_replace(path, **kwargs):
        receipt = verify(path, **kwargs)
        called.append(True)
        dataset_path = Path(path) / "dataset.json"
        dataset = json.loads(dataset_path.read_text())
        dataset["bars"][0]["close"] = "102"
        dataset_path.write_text(json.dumps(dataset))
        return receipt
    monkeypatch.setattr(cli, "verify_historical_archive", verify_then_replace)
    assert_cli_error(capsys, ["--archive", str(archive), "--preflight"])
    assert called == [True]


def test_bare_dataset_is_not_an_archive(tmp_path, capsys):
    source = tmp_path / "bare-dataset"
    source.mkdir()
    (source / "dataset.json").write_text(json.dumps(normalize().dataset))
    before = files_at(tmp_path)
    assert_cli_error(capsys, ["--archive", str(source), "--preflight"])
    assert files_at(tmp_path) == before
