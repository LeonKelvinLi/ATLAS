"""Explicit initialization or foreground data collection; no execution options."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from atlas.collection.config import CollectionError, load_config
from atlas.collection.archive import CollectionArchiveError
from atlas.collection.primitives import CollectionInputError
from atlas.collection.runtime import init_runtime, run
from atlas.research.data_archive import DataArchiveError
from atlas.research.data_collector import DataCollectorError
from atlas.research.data_contract_v1 import DataContractError


# These are owned source labels, never filenames or function names supplied by
# an exception. An exact filename match is a location hint, not code attestation.
_SOURCE_LABELS = {str(SOURCE / name): name for name in (
    "scripts/atlas_collect.py", "atlas/collection/config.py",
    "atlas/collection/state.py", "atlas/collection/runtime.py",
    "atlas/collection/archive.py", "atlas/collection/primitives.py",
    "atlas/research/data_contract_v1.py", "atlas/research/data_archive.py",
    "atlas/research/data_collector.py", "atlas/research/data_coverage.py",
)}
_COLLECTION_CAUSES = frozenset((
    "collector_execution_environment_refused", "collector_path_invalid",
    "collector_symlink_refused", "collector_state_inside_checkout_refused",
    "collector_private_path_required", "collector_json_invalid",
    "collector_config_fields_invalid", "collector_execution_configuration_refused",
    "collector_generation_invalid", "collector_archive_layout_invalid",
    "collector_research_start_invalid", "collector_providers_invalid",
    "collector_secret_inside_state_refused", "collector_explicit_alpaca_data_key_required",
    "collector_configuration_bound_invalid", "collector_clock_invalid",
    "collector_lock_unavailable", "collector_state_size_bound_exceeded",
    "collector_state_unavailable_or_corrupt",
    "collector_initialization_failed_retained_for_inspection",
    "collector_state_metadata_invalid", "collector_provider_state_missing",
    "collector_state_integrity_failed",
    "collector_configuration_changed_requires_explicit_migration",
    "collector_provider_configuration_mismatch", "collector_restore_marker_invalid",
    "collector_initialization_requires_new_state_directory",
    "collector_result_metadata_missing_or_invalid", "collector_result_calendar_invalid",
    "collector_result_timestamp_invalid", "collector_clock_regressed",
    "collector_cycle_bound_invalid", "collector_injected_acquirer_requires_test_clock",
    "collector_incomplete_restore_refused", "collector_test_clock_on_production_state_refused",
    "collector_archive_registration_mismatch", "collector_disk_reserve_exhausted",
))
_ARCHIVE_CAUSES = frozenset((
    "archive_batch_chain_invalid", "archive_clock_regressed",
    "archive_storage_unavailable_or_busy", "archive_observation_revision_or_hash_invalid",
    "archive_raw_capture_integrity_invalid",
))
# Exact identities avoid reflecting custom class names or invoking subclass
# accessors. Unknown exception classes still receive a useful source location.
_EXCEPTION_LABELS = (
    (CollectionError, "CollectionError"), (ValueError, "ValueError"),
    (CollectionArchiveError, "CollectionArchiveError"),
    (CollectionInputError, "CollectionInputError"), (DataArchiveError, "DataArchiveError"),
    (DataCollectorError, "DataCollectorError"), (DataContractError, "DataContractError"),
    (OSError, "OSError"), (KeyError, "KeyError"), (TypeError, "TypeError"),
    (RuntimeError, "RuntimeError"), (AssertionError, "AssertionError"),
    (OverflowError, "OverflowError"), (ZeroDivisionError, "ZeroDivisionError"),
    (RecursionError, "RecursionError"), (MemoryError, "MemoryError"),
    (FileNotFoundError, "FileNotFoundError"), (PermissionError, "PermissionError"),
    (TimeoutError, "TimeoutError"), (BrokenPipeError, "BrokenPipeError"),
    (sqlite3.Error, "sqlite3.Error"), (sqlite3.DatabaseError, "sqlite3.DatabaseError"),
    (sqlite3.OperationalError, "sqlite3.OperationalError"),
    (sqlite3.IntegrityError, "sqlite3.IntegrityError"),
)
_FALLBACK = (b'{"cause":"collector_diagnostics_unavailable",'
             b'"diagnostic_version":1,"error":"collector_operation_failed",'
             b'"event":"collector_fatal","execution_enabled":false,"status":"REFUSED"}\n')


def _safe_exception(exc):
    kind = type(exc)
    label = next((label for known, label in _EXCEPTION_LABELS if kind is known), "Exception")
    cause = "collector_unexpected_exception"
    if kind is CollectionError or kind is DataArchiveError:
        allowed = _COLLECTION_CAUSES if kind is CollectionError else _ARCHIVE_CAUSES
        unknown = "collector_error_unclassified" if kind is CollectionError else cause
        args = BaseException.args.__get__(exc, BaseException)
        cause = (args[0] if type(args) is tuple and len(args) == 1
                 and type(args[0]) is str and args[0] in allowed else unknown)
    frames, truncated = [], False
    trace = BaseException.__traceback__.__get__(exc, BaseException)
    for _ in range(128):
        if trace is None:
            break
        filename, line = trace.tb_frame.f_code.co_filename, trace.tb_lineno
        if type(filename) is str and filename in _SOURCE_LABELS and type(line) is int and 1 <= line <= 1000000:
            frames.append({"source": _SOURCE_LABELS[filename], "line": line})
            truncated = truncated or len(frames) > 8
            frames = frames[-8:]
        trace = trace.tb_next
    return {"cause": cause, "exception_type": label, "frames": frames,
            "frames_truncated": truncated or trace is not None}


def _fatal_payload(exc, stage):
    result = {"status": "REFUSED", "error": "collector_operation_failed",
              "event": "collector_fatal", "execution_enabled": False,
              "diagnostic_version": 1,
              "stage": stage if type(stage) is str and stage in (
                  "load_config", "init", "run", "write_result") else "unknown",
              **_safe_exception(exc), "exception_chain": [],
              "exception_chain_truncated": False}
    seen = {id(exc)}
    # Cleanup errors can mask the collection fault, including with `from None`.
    # Preserve only safe summaries, never Python's formatted exception chain.
    for _ in range(2):
        previous = BaseException.__cause__.__get__(exc, BaseException)
        relation = "cause"
        if previous is None:
            previous = BaseException.__context__.__get__(exc, BaseException)
            relation = "context"
        if previous is None:
            break
        if id(previous) in seen:
            result["exception_chain_truncated"] = True
            break
        seen.add(id(previous))
        result["exception_chain"].append({"relation": relation, **_safe_exception(previous)})
        exc = previous
    else:
        previous = BaseException.__cause__.__get__(exc, BaseException)
        if previous is None:
            previous = BaseException.__context__.__get__(exc, BaseException)
        result["exception_chain_truncated"] = previous is not None
    return result


def _emit_fatal(exc, stage):
    try:
        content = (json.dumps(_fatal_payload(exc, stage), sort_keys=True,
                              separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
        if len(content) > 8192:
            content = _FALLBACK
    except Exception:
        content = _FALLBACK
    # Both descriptors normally reach journald. Bypass Python stream wrappers,
    # bound retries, and never create files in possibly broken collector state.
    # If both sinks fail, exit remains nonzero; log retention cannot be promised.
    for descriptor in (2, 1):
        offset = 0
        for _ in range(32):
            try:
                written = os.write(descriptor, content[offset:])
                if type(written) is not int or not 0 < written <= len(content) - offset:
                    break
                offset += written
                if offset == len(content):
                    return
            except InterruptedError:
                continue
            except Exception:
                break


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "once", "run"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    stage = "load_config"
    try:
        config = load_config(args.config)
        stage = "init" if args.command == "init" else "run"
        result = init_runtime(config) if args.command == "init" else run(config, once=args.command == "once")
        stage = "write_result"
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        _emit_fatal(exc, stage)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
