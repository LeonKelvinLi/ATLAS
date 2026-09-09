"""Verdict-day guards. forward_eval runs exactly ONCE, so a crash in it is
unrecoverable - these pin failure modes an audit actually found.
"""

import json
import pickle
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def test_load_bundle_warning_branch_does_not_raise_nameerror(
        monkeypatch, tmp_path, capsys):
    """meta-h3.pkl carries no data_end_ts, so load_bundle's 'cannot verify'
    warning branch executes on EVERY run. It referenced sys.stderr while the
    module never imported sys, raising NameError - and main() caught only
    SystemExit, so it would have killed the leak-immune paper track too.
    """
    import forward_eval

    # Exercise the metadata branch with our own fixture, without unpickling
    # private model artifacts or requiring a collected forward window.
    expected = {"meta": {"trained_on": "synthetic test fixture"}}
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "meta-h3.pkl").write_bytes(pickle.dumps(expected))
    monkeypatch.setattr(forward_eval, "PROJECT_DIR", tmp_path)
    name, bundle = forward_eval.load_bundle("meta-h3.pkl", 1784678400)
    assert name == "meta-h3.pkl"
    assert bundle == expected
    warning = capsys.readouterr().err
    assert "post-cutoff guard could NOT verify this bundle" in warning
    assert "synthetic test fixture" in warning


def test_forward_eval_imports_sys_for_its_stderr_writes():
    source = (PROJECT_DIR / "forward_eval.py").read_text(encoding="utf-8")
    if "sys.stderr" in source:
        assert "import sys" in source, (
            "forward_eval writes to sys.stderr but does not import sys")


def test_candles_track_failure_cannot_kill_the_paper_track():
    """main() must contain ANY candles-track exception, not just SystemExit:
    the paper track is independent evidence and immune to hindsight."""
    source = (PROJECT_DIR / "forward_eval.py").read_text(encoding="utf-8")
    assert "except (SystemExit, Exception)" in source, (
        "a candles-track failure is not broadly contained")


OPERATIONAL_MODULES = [
    "forward_eval.py", "live_h2_runner.py", "mission_control.py",
    "settle_missing.py", "probe_execution.py", "probe_order_record.py",
    "selfcheck.py", "watchdog.py", "catchup_gaps.py", "extra_collect.py",
    "backup_state.py", "status.py", "dashboard.py",
]


def test_no_undefined_names():
    """Catch the whole CLASS of the sys-import bug: pylint's undefined-variable
    check over every operational module. A NameError on a rarely-taken branch
    is exactly the kind of defect that only shows up when it matters."""
    out = subprocess.run(
        [sys.executable, "-m", "pylint", "--disable=all",
         "--enable=E0602,E0601", "--score=n", "--persistent=n",
         "--output-format=json", *OPERATIONAL_MODULES],
        cwd=PROJECT_DIR, capture_output=True, text=True, timeout=300)
    # One process amortizes import analysis across all modules. E1101 was
    # deliberately ignored for C-extension false positives; leave it disabled
    # instead of spending time computing diagnostics that cannot fail a test.
    # Pylint uses a bitmask: 2 means error diagnostics. Missing pylint,
    # crashes, fatal errors and invalid invocations must not silently pass.
    assert out.returncode in (0, 2), (
        f"pylint did not complete (exit {out.returncode}):\n"
        f"{out.stderr}\n{out.stdout}")
    messages = json.loads(out.stdout)
    assert isinstance(messages, list), "pylint did not return a diagnostic list"
    if out.returncode == 2:
        assert messages, "pylint failed without producing diagnostic evidence"
    assert not messages, "pylint errors:\n" + json.dumps(messages, indent=2)
