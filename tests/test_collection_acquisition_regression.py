"""Offline builtin-origin regressions; every archive and credential is synthetic.

The real transport and credential reader are replaced, rather than passing an
injected transport/clock to acquisition, so historical/builtin groups remain
distinct. No provider connection or real credential is used.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from atlas.collection import archive
from atlas.collection.primitives import HTTPResponse, _encoded
from atlas.research import data_archive as store
from atlas.research import data_collector
from atlas.research.data_contract_v1 import default_contract


NOW = int(datetime(2026, 9, 15, 12, 31, 26, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def mixed_origin_archive(tmp_path, monkeypatch):
    wall = [NOW - 3600]
    monkeypatch.setattr(store.time, "time", lambda: wall[0])
    root = tmp_path.resolve() / "synthetic-generation"
    archive.init_collection_archive(root, default_contract("synthetic-clock-regression",
        NOW - 180 * 86400, wall[0]))
    wall[0] = NOW

    def getter(url, headers, **limits):
        if "/calendar?" in url:
            body = [{"date": "2026-09-15", "open": "09:30", "close": "16:00"}]
        elif "/bars?" in url:
            body = {"bars": {}, "next_page_token": None}
        else:
            assert "/quotes/latest?" in url
            body = {"quotes": {
                symbol: {"t": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                    "bp": 100, "ap": 101, "bs": 1, "as": 1}
                for symbol, stamp in (("SPY", NOW - 1), ("USO", NOW - 600))}}
        return HTTPResponse(200, _encoded(body))

    monkeypatch.setattr(data_collector, "alpaca_get", getter)
    monkeypatch.setattr(data_collector, "_credentials", lambda _path:
        SimpleNamespace(api_key="SYNTHETIC", api_secret="SYNTHETIC"))

    def acquire():
        end = NOW // 300 * 300
        return archive.acquire_collection_once(root, provider="alpaca", start=end - 900,
            end=end, key_file=str(root.parent / "synthetic-key.json"))

    return root, wall, acquire


def test_later_group_validates_after_first_group_crosses_wall_clock_second(
        mixed_origin_archive, monkeypatch):
    root, wall, acquire = mixed_origin_archive
    original = store._append
    calls = []

    def delayed_append(*args, **kwargs):
        calls.append(kwargs["origin"])
        if len(calls) == 1:
            # Acquisition classified both groups at NOW. Work preparing the
            # first append crosses a second before its durable receipt time.
            wall[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_append", delayed_append)
    result = acquire()
    assert result["collection_complete"] is True
    assert result["appended_records"] == 2
    assert calls == ["builtin_get", "historical_import"]
    verified = archive.verify_collection_archive(root)
    assert verified["integrity_verified"] is True
    assert verified["record_count"] == 2
    assert verified["acquisition_kinds"] == {
        "prospective_observation": 1, "historical_backfill": 1, "test": 0}
    assert verified["qualification_established"] is False


def test_backwards_clock_after_first_commit_is_still_refused(
        mixed_origin_archive, monkeypatch):
    root, wall, acquire = mixed_origin_archive
    original = store._append
    calls = []

    def regressing_append(*args, **kwargs):
        calls.append(kwargs["origin"])
        wall[0] = NOW + 1
        result = original(*args, **kwargs)
        wall[0] = NOW
        return result

    monkeypatch.setattr(store, "_append", regressing_append)
    with pytest.raises(store.DataArchiveError, match="archive_batch_chain_invalid"):
        acquire()
    assert calls == ["builtin_get"]
    wall[0] = NOW + 1
    verified = archive.verify_collection_archive(root, recover=True)
    assert verified["record_count"] == 1
    assert verified["integrity_verified"] is True


def test_restart_after_first_group_commit_preserves_lineage_and_deduplicates(
        mixed_origin_archive, monkeypatch):
    root, wall, acquire = mixed_origin_archive
    original = store._append

    class Interrupted(BaseException):
        pass

    def interrupted_append(*args, **kwargs):
        wall[0] += 1
        original(*args, **kwargs)
        raise Interrupted

    monkeypatch.setattr(store, "_append", interrupted_append)
    with pytest.raises(Interrupted):
        acquire()
    recovered = archive.verify_collection_archive(root, recover=True)
    assert recovered["record_count"] == 1
    monkeypatch.setattr(store, "_append", original)
    result = acquire()
    assert result["collection_complete"] is True
    assert result["appended_records"] == 1
    assert result["unchanged_observations"] == 1
    verified = archive.verify_collection_archive(root)
    assert verified["record_count"] == 2
    assert verified["integrity_verified"] is True
    with store._connection(root / "days" / archive._day(NOW)) as db:
        records = store._scan(db, wall[0])["records"]
    assert {row["symbol"] for row in records} == {"SPY", "USO"}
    assert all(row["revision"] == 0 and row["previous_observation_hash"] is None
               for row in records)
