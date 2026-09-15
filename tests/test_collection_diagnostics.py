"""Fatal collector receipts retain useful causes without reflecting secret data."""

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from atlas.collection.config import CollectionError
from atlas.research.data_archive import DataArchiveError
from atlas.research.data_contract_v1 import DataContractError


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("collector_diagnostics_under_test", ROOT / "scripts/atlas_collect.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
SECRET = "SENTINEL_PRIVATE_KEY_https://secret.example/response?token=secret"


def fail(exc):
    def operation(*args, **kwargs):
        raise exc
    return operation


def receipt(monkeypatch, capfd, exc):
    monkeypatch.setattr(cli, "load_config", lambda _: {})
    monkeypatch.setattr(cli, "run", fail(exc))
    assert cli.main(["once", "--config", "/not-read"]) == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert SECRET not in captured.err
    assert "Traceback" not in captured.err
    result = json.loads(captured.err)
    assert result["status"] == "REFUSED"
    assert result["error"] == "collector_operation_failed"
    assert result["execution_enabled"] is False
    assert result["event"] == "collector_fatal"
    assert result["stage"] == "run"
    assert any(row["source"] == "scripts/atlas_collect.py" for row in result["frames"])
    assert all(set(row) == {"source", "line"} for row in result["frames"])
    return result


def test_real_invalid_config_keeps_owned_cause_and_stage(tmp_path, monkeypatch, capfd):
    path = tmp_path / "config.json"
    path.write_text(SECRET)
    path.chmod(0o600)
    monkeypatch.setattr(cli, "run", lambda *a, **k: pytest.fail("invalid config cannot collect"))
    assert cli.main(["once", "--config", str(path)]) == 1
    captured = capfd.readouterr()
    result = json.loads(captured.err)
    assert result["stage"] == "load_config"
    assert result["cause"] == "collector_json_invalid"
    assert result["exception_type"] == "CollectionError"
    assert any(row["source"] == "atlas/collection/config.py" for row in result["frames"])
    assert str(tmp_path) not in captured.err and SECRET not in captured.err


@pytest.mark.parametrize("error_type,label", [
    (RuntimeError, "RuntimeError"), (OSError, "OSError"), (KeyError, "KeyError"),
    (TypeError, "TypeError"), (sqlite3.OperationalError, "sqlite3.OperationalError"),
    (DataArchiveError, "DataArchiveError"), (DataContractError, "DataContractError"),
])
def test_unexpected_failures_have_static_types_without_messages(monkeypatch, capfd, error_type, label):
    exc = error_type(SECRET)
    exc.add_note(SECRET)
    result = receipt(monkeypatch, capfd, exc)
    assert result["exception_type"] == label
    assert result["cause"] == "collector_unexpected_exception"


@pytest.mark.parametrize("value,expected", [
    ("collector_disk_reserve_exhausted", "collector_disk_reserve_exhausted"),
    ("collector_clock_regressed", "collector_clock_regressed"),
    (SECRET, "collector_error_unclassified"),
    ("collector_secret_token_value", "collector_error_unclassified"),
])
def test_only_exact_allowlisted_collection_causes_are_reflected(monkeypatch, capfd, value, expected):
    assert receipt(monkeypatch, capfd, CollectionError(value))["cause"] == expected


@pytest.mark.parametrize("cause", [
    "archive_batch_chain_invalid", "archive_clock_regressed",
    "archive_storage_unavailable_or_busy", "archive_observation_revision_or_hash_invalid",
    "archive_raw_capture_integrity_invalid",
])
def test_known_archive_causes_are_retained(monkeypatch, capfd, cause):
    result = receipt(monkeypatch, capfd, DataArchiveError(cause))
    assert result["cause"] == cause and result["exception_type"] == "DataArchiveError"


def test_unknown_archive_cause_looks_like_code_but_is_not_reflected(monkeypatch, capfd):
    cause = "archive_secret_token_value"
    result = receipt(monkeypatch, capfd, DataArchiveError(cause))
    assert result["cause"] == "collector_unexpected_exception"
    assert cause not in json.dumps(result)


def test_hostile_exception_subclass_accessors_and_names_are_never_used(monkeypatch, capfd):
    calls = []
    def forbidden(*args):
        calls.append(True)
        raise AssertionError(SECRET)
    hostile = type(SECRET, (CollectionError,), {
        "__str__": forbidden, "__repr__": forbidden, "__getattribute__": forbidden,
        "__module__": SECRET,
    })
    result = receipt(monkeypatch, capfd, hostile(SECRET))
    assert result["exception_type"] == "Exception"
    assert result["cause"] == "collector_unexpected_exception"
    assert calls == []


def test_string_subclass_cannot_smuggle_or_execute_a_cause(monkeypatch, capfd):
    class HostileString(str):
        def __hash__(self):
            pytest.fail("untrusted cause hashing")
        def __eq__(self, other):
            pytest.fail("untrusted cause comparison")
    result = receipt(monkeypatch, capfd, CollectionError(HostileString("collector_clock_regressed")))
    assert result["cause"] == "collector_error_unclassified"


@pytest.mark.parametrize("filename", [SECRET, str(ROOT / "atlas/collection/runtime.py")])
def test_dynamic_filename_and_function_name_are_never_reflected(filename):
    namespace = {}
    exec(compile("def SENTINEL_DYNAMIC_FUNCTION():\n    raise RuntimeError(secret)\n", filename, "exec"), namespace)
    namespace["secret"] = SECRET
    try:
        namespace["SENTINEL_DYNAMIC_FUNCTION"]()
    except RuntimeError as exc:
        result = cli._fatal_payload(exc, "run")
    encoded = json.dumps(result)
    assert SECRET not in encoded and "SENTINEL_DYNAMIC_FUNCTION" not in encoded
    assert all(row["source"] in cli._SOURCE_LABELS.values() for row in result["frames"])
    if filename == SECRET:
        assert result["frames"] == []


def test_cleanup_failure_keeps_safe_original_context_even_when_suppressed(monkeypatch, capfd):
    def run(*args, **kwargs):
        try:
            raise CollectionError("collector_clock_regressed")
        finally:
            raise OSError(SECRET) from None
    monkeypatch.setattr(cli, "load_config", lambda _: {})
    monkeypatch.setattr(cli, "run", run)
    assert cli.main(["run", "--config", "/not-read"]) == 1
    captured = capfd.readouterr()
    result = json.loads(captured.err)
    assert result["exception_type"] == "OSError"
    assert result["exception_chain"][0]["relation"] == "context"
    assert result["exception_chain"][0]["cause"] == "collector_clock_regressed"
    assert SECRET not in captured.err


def test_explicit_cause_long_chain_and_cycle_are_bounded():
    errors = [RuntimeError(SECRET) for _ in range(20)]
    for current, previous in zip(errors, errors[1:]):
        current.__cause__ = previous
    result = cli._fatal_payload(errors[0], "run")
    assert len(result["exception_chain"]) == 2
    assert all(row["relation"] == "cause" for row in result["exception_chain"])
    assert result["exception_chain_truncated"] is True
    errors[1].__cause__ = errors[0]
    result = cli._fatal_payload(errors[0], "run")
    assert len(result["exception_chain"]) == 1
    assert result["exception_chain_truncated"] is True
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("depth", [20, 160])
def test_deep_owned_stack_is_bounded_and_marks_truncation(depth):
    namespace = {"secret": SECRET}
    exec(compile("def nested(n):\n    if n: return nested(n-1)\n    raise RuntimeError(secret)\n",
                 str(ROOT / "atlas/collection/runtime.py"), "exec"), namespace)
    try:
        namespace["nested"](depth)
    except RuntimeError as exc:
        result = cli._fatal_payload(exc, "run")
    assert len(result["frames"]) == 8
    assert result["frames_truncated"] is True
    if depth == 20:
        assert result["frames"][-1]["line"] == 3
    assert len(json.dumps(result)) < 8192 and SECRET not in json.dumps(result)


def test_short_writes_and_interruption_produce_one_complete_receipt(monkeypatch):
    parts, attempts = [], []
    def write(fd, content):
        attempts.append(fd)
        if len(attempts) == 1:
            raise InterruptedError(SECRET)
        count = min(80, len(content))
        parts.append(content[:count])
        return count
    monkeypatch.setattr(cli.os, "write", write)
    cli._emit_fatal(CollectionError("collector_clock_regressed"), "run")
    assert set(attempts) == {2}
    assert json.loads(b"".join(parts))["cause"] == "collector_clock_regressed"
    assert len(attempts) <= 32


def test_stderr_failure_falls_back_to_stdout_without_exception_text(monkeypatch):
    writes = []
    def write(fd, content):
        if fd == 2:
            raise OSError(SECRET)
        writes.append(content)
        return len(content)
    monkeypatch.setattr(cli.os, "write", write)
    cli._emit_fatal(RuntimeError(SECRET), "run")
    assert json.loads(b"".join(writes))["exception_type"] == "RuntimeError"
    assert SECRET.encode() not in b"".join(writes)


@pytest.mark.parametrize("mode", ["raise", "zero", "interrupt"])
def test_all_unavailable_sinks_leave_nonzero_exit_with_bounded_attempts(monkeypatch, tmp_path, mode):
    calls = []
    def write(fd, content):
        calls.append(fd)
        if mode == "zero":
            return 0
        if mode == "interrupt":
            raise InterruptedError(SECRET)
        raise RuntimeError(SECRET)
    monkeypatch.setattr(cli.os, "write", write)
    monkeypatch.setattr(cli, "load_config", fail(RuntimeError(SECRET)))
    before = set(tmp_path.iterdir())
    assert cli.main(["once", "--config", str(tmp_path / "not-read")]) == 1
    assert set(calls) == {1, 2} and len(calls) <= 64
    assert set(tmp_path.iterdir()) == before


def test_diagnostic_failure_uses_static_fallback(monkeypatch):
    writes = []
    monkeypatch.setattr(cli, "_fatal_payload", fail(RuntimeError(SECRET)))
    monkeypatch.setattr(cli.os, "write", lambda fd, content: writes.append(content) or len(content))
    cli._emit_fatal(RuntimeError(SECRET), "run")
    assert json.loads(b"".join(writes))["cause"] == "collector_diagnostics_unavailable"
    assert SECRET.encode() not in b"".join(writes)


@pytest.mark.parametrize("stage", ["init", "write_result"])
def test_init_and_result_output_failures_keep_their_stage(stage, monkeypatch, capfd):
    monkeypatch.setattr(cli, "load_config", lambda _: {})
    if stage == "init":
        monkeypatch.setattr(cli, "init_runtime", fail(RuntimeError(SECRET)))
    else:
        # A non-serializable result fails before it can reach the output stream.
        monkeypatch.setattr(cli, "init_runtime", lambda _: {"invalid": object()})
    assert cli.main(["init", "--config", "/not-read"]) == 1
    captured = capfd.readouterr()
    assert json.loads(captured.err)["stage"] == stage
    assert SECRET not in captured.err and captured.out == ""


@pytest.mark.parametrize("command", ["init", "once", "run"])
def test_success_output_remains_compatible(command, monkeypatch, capfd):
    expected = {"execution_enabled": False, "unchanged": True}
    monkeypatch.setattr(cli, "load_config", lambda _: {})
    monkeypatch.setattr(cli, "run", lambda *a, **k: expected)
    monkeypatch.setattr(cli, "init_runtime", lambda *a, **k: expected)
    assert cli.main([command, "--config", "/not-read"]) == 0
    captured = capfd.readouterr()
    assert json.loads(captured.out) == expected and captured.err == ""


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_control_flow_base_exceptions_are_not_converted(error_type, monkeypatch):
    monkeypatch.setattr(cli, "load_config", fail(error_type()))
    with pytest.raises(error_type):
        cli.main(["once", "--config", "/not-read"])
