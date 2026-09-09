"""The public first-run path must work without local data or broker access."""

import json
import socket

import pandas as pd
import pytest

from demo import SOURCE, run_demo


def test_offline_demo_is_reproducible_and_does_not_touch_working_state(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("offline demo attempted network access")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.chdir(tmp_path)
    # Catch accidental use of conventional production paths.
    for name in ("market.duckdb", "journal.db", "research_registry.jsonl", "experiments.jsonl", ".env"):
        (tmp_path / name).write_bytes(b"do not touch\n")
    out = tmp_path / "demo"
    first = run_demo(out, rows=1200)
    second = run_demo(out, rows=1200)
    assert first == second
    assert first["data_source"] == SOURCE
    assert all(first["checks"].values())
    assert first["manifest"]["stake"] == 0.0
    assert len(first["folds"]) == 3
    assert len(pd.read_parquet(out / "candles.parquet")) == 1200
    assert json.loads((out / "report.json").read_text()) == first
    for path in tmp_path.iterdir():
        if path != out:
            assert path.read_bytes() == b"do not touch\n"


def test_demo_refuses_unrelated_output(tmp_path):
    important = tmp_path / "notes.txt"
    important.write_text("keep")
    with pytest.raises(ValueError, match="unrelated"):
        run_demo(tmp_path)
    assert important.read_text() == "keep"


def test_demo_explains_insufficient_input(tmp_path):
    with pytest.raises(ValueError, match="at least 1200"):
        run_demo(tmp_path, rows=100)
    assert not list(tmp_path.iterdir())
