"""Immutable lineage, atomic claims, corruption and holdout boundaries offline."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import sqlite3
import stat

import pytest

from atlas.research.store import HoldoutAccessError, ResearchConflict, ResearchStore, ResearchStoreError, content_hash


@pytest.fixture
def manifest():
    return {"schema_version": 1, "campaign": "synthetic-momentum", "evidence_kind": "SYNTHETIC",
            "source_identity": {"git_commit": "a" * 40, "source_tree_sha256": "b" * 64,
                                "lock_sha256": "c" * 64, "runtime": {"python": "test-only"}},
            "dataset": {"content_sha256": "d" * 64, "partition_sha256": "e" * 64, "role": "development"},
            "strategy": {"strategy_id": "momentum", "version": "1", "implementation_sha256": "f" * 64,
                         "parameters": {"lookback": 20}},
            "features": {"version": "1", "definitions_sha256": "1" * 64},
            "execution": {"version": "1", "implementation_sha256": "2" * 64,
                          "assumptions": {"fee_bps": 5, "slippage_bps": 10, "latency_bars": 1}},
            "split": {"training": [1, 10], "validation": [11, 15], "test": [16, 20]},
            "seed": 0, "evaluated_variant_count": 3}


def test_reserve_complete_hash_replay_and_immutable_result(tmp_path, manifest):
    with ResearchStore(tmp_path / "store", clock=lambda: 100) as store:
        first = store.reserve(manifest, "request-one")
        replay = store.reserve(manifest, "request-one")
        assert replay["replayed"] is True and first["experiment_id"] == replay["experiment_id"]
        assert first["manifest_hash"] == content_hash(manifest) and first["artifacts"] is None
        result = store.complete(first["experiment_id"], {"metrics": {"net_pnl": -5}, "evaluated_variant_count": 3})
        assert result["status"] == "completed" and result["result_hash"]
        assert store.complete(first["experiment_id"], result["artifacts"]) == result
        with pytest.raises(ResearchConflict):
            store.complete(first["experiment_id"], {"metrics": {"net_pnl": 10}})
        with pytest.raises(ResearchConflict):
            store.fail(first["experiment_id"], "replace_failure")
        assert store.verify(first["experiment_id"])["reproduced"] is False
    with ResearchStore(tmp_path / "store") as reopened:
        assert reopened.get(first["experiment_id"])["artifacts"]["metrics"]["net_pnl"] == -5
        assert reopened.counts(manifest["campaign"])["evaluated_variants"] == 3


def test_every_reserved_and_failed_attempt_stays_in_search_denominator(tmp_path, manifest):
    with ResearchStore(tmp_path / "store") as store:
        failed = store.reserve(manifest, "failed")
        store.fail(failed["experiment_id"], "invalid_market_data")
        store.reserve(manifest, "unfinished")
        counts = store.counts(manifest["campaign"])
        assert counts["attempts"] == 2 and counts["failed"] == 1 and counts["reserved"] == 1
        assert counts["planned_variants"] == 6 and counts["evaluated_variants"] is None
        assert counts["known_evaluated_variants"] == 0 and counts["unknown_evaluated_attempts"] == 2
        with pytest.raises(ResearchConflict):
            store.complete(failed["experiment_id"], {"metrics": {}})
        changed = deepcopy(manifest)
        changed["seed"] = 10
        with pytest.raises(ResearchConflict):
            store.reserve(changed, "failed")


def test_concurrent_connections_claim_one_request_exactly_once(tmp_path, manifest):
    root = tmp_path / "store"
    with ResearchStore(root):
        pass
    def reserve(_):
        with ResearchStore(root) as store:
            return store.reserve(manifest, "concurrent")
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(reserve, range(4)))
    assert len({row["experiment_id"] for row in results}) == 1
    assert sum(not row["replayed"] for row in results) == 1
    with ResearchStore(root) as store:
        assert store.counts(manifest["campaign"])["attempts"] == 1


@pytest.mark.parametrize("field", ["source_identity", "dataset", "features", "strategy", "execution", "split"])
def test_missing_lineage_refuses_before_attempt(tmp_path, manifest, field):
    del manifest[field]
    with ResearchStore(tmp_path / "store") as store:
        with pytest.raises(ResearchStoreError):
            store.reserve(manifest, "incomplete")
        assert store.list() == []


@pytest.mark.parametrize("change", ["short_hash", "nan", "secret", "huge", "boolean_seed", "sealed_role"])
def test_untrusted_lineage_values_cannot_create_experiment(tmp_path, manifest, change):
    if change == "short_hash":
        manifest["source_identity"]["lock_sha256"] = "a" * 16
    elif change == "nan":
        manifest["strategy"]["parameters"]["threshold"] = float("nan")
    elif change == "secret":
        manifest["source_identity"]["authorization"] = "private-fixture"
    elif change == "huge":
        manifest["seed"] = 10 ** 101
    elif change == "boolean_seed":
        manifest["seed"] = True
    else:
        manifest["dataset"]["role"] = "sealed_holdout"
    with ResearchStore(tmp_path / "store") as store:
        with pytest.raises(ResearchStoreError):
            store.reserve(manifest, "invalid")
        assert store.list() == []


@pytest.mark.parametrize("identifier", ["../escape", "exp_../../x", "ｅxp_" + "a" * 32, "exp_" + "g" * 32])
def test_experiment_identifiers_cannot_be_paths_or_unicode_lookalikes(tmp_path, identifier):
    with ResearchStore(tmp_path / "store") as store:
        with pytest.raises(ResearchStoreError):
            store.get(identifier)


def test_mutating_inputs_or_returned_objects_does_not_change_manifest(tmp_path, manifest):
    original = deepcopy(manifest)
    with ResearchStore(tmp_path / "store") as store:
        record = store.reserve(manifest, "copy-test")
        manifest["seed"] = 99
        record["manifest"]["seed"] = 100
        assert store.get(record["experiment_id"])["manifest"] == original


@pytest.mark.parametrize("damage", ["empty", "garbage", "schema", "manifest", "result"])
def test_existing_damaged_research_state_is_never_reset(tmp_path, manifest, damage):
    root = tmp_path / "store"
    with ResearchStore(root) as store:
        record = store.reserve(manifest, "damage-test")
        store.complete(record["experiment_id"], {"metrics": {"net_return": -1}})
    path = root / "research.db"
    if damage in {"empty", "garbage"}:
        path.write_bytes(b"" if damage == "empty" else b"invalid database")
    else:
        with sqlite3.connect(path) as connection:
            if damage == "schema":
                connection.execute("DROP TABLE holdouts")
            elif damage == "manifest":
                connection.execute("UPDATE attempts SET manifest='{}'")
            else:
                connection.execute("UPDATE results SET artifacts='{}'")
    before = path.read_bytes()
    with pytest.raises(ResearchStoreError):
        ResearchStore(root)
    assert path.exists() and (path.read_bytes() == before if damage in {"empty", "garbage"} else path.stat().st_size > 0)


def test_failed_directory_sync_blocks_instance_and_requires_reopen(tmp_path, manifest, monkeypatch):
    root = tmp_path / "store"
    with ResearchStore(root) as store:
        def fail(_):
            raise OSError("private filesystem details")
        monkeypatch.setattr(store, "_sync", fail)
        with pytest.raises(ResearchStoreError, match="durability"):
            store.reserve(manifest, "uncertain-commit")
        with pytest.raises(ResearchStoreError, match="unavailable"):
            store.list()
    with ResearchStore(root) as restored:
        assert restored.reserve(manifest, "uncertain-commit")["replayed"] is True


def test_holdout_aliases_repeated_claims_and_plan_edits_are_blocked(tmp_path, manifest):
    dataset = manifest["dataset"]
    args = dataset["content_sha256"], dataset["partition_sha256"], "3" * 64
    with ResearchStore(tmp_path / "store") as store:
        assert store.seal_holdout(*args)["state"] == "sealed"
        with pytest.raises(HoldoutAccessError):
            store.reserve(manifest, "accidental-holdout")
        with pytest.raises(HoldoutAccessError):
            store.assert_development_access(dataset["content_sha256"], "4" * 64)
        with pytest.raises(ResearchConflict):
            store.seal_holdout(*args[:2], "5" * 64)
        assert store.claim_holdout(*args, "operator-claim")["state"] == "claimed"
        with pytest.raises(HoldoutAccessError):
            store.claim_holdout(*args, "operator-claim")
        with pytest.raises(HoldoutAccessError):
            store.consume_holdout(*args[:2], "wrong-claim", "6" * 64)
        assert store.consume_holdout(*args[:2], "operator-claim", "6" * 64)["state"] == "consumed"
        with pytest.raises(HoldoutAccessError):
            store.claim_holdout(*args, "new-claim")


def test_explored_data_cannot_be_relabelled_as_fresh_holdout(tmp_path, manifest):
    with ResearchStore(tmp_path / "store") as store:
        store.reserve(manifest, "explored")
        with pytest.raises(HoldoutAccessError, match="previously researched"):
            store.seal_holdout(manifest["dataset"]["content_sha256"], "8" * 64, "9" * 64)


def test_holdout_claim_survives_restart_and_has_no_reset_api(tmp_path, manifest):
    args = manifest["dataset"]["content_sha256"], manifest["dataset"]["partition_sha256"], "3" * 64
    with ResearchStore(tmp_path / "store") as store:
        store.seal_holdout(*args)
        store.claim_holdout(*args, "one-claim")
    with ResearchStore(tmp_path / "store") as reopened:
        with pytest.raises(HoldoutAccessError):
            reopened.claim_holdout(*args, "second-claim")
        assert not hasattr(reopened, "unseal_holdout")


def test_holdout_overlap_is_blocked_when_content_hash_changes_and_disjoint_observations_pass(tmp_path, manifest):
    with ResearchStore(tmp_path / "store") as store:
        store.seal_holdout("a" * 64, "b" * 64, "c" * 64, observation_keys=["1" * 64, "2" * 64])
        manifest["dataset"]["observation_keys"] = ["2" * 64, "3" * 64]
        with pytest.raises(HoldoutAccessError, match="overlap"):
            store.reserve(manifest, "overlap-under-new-content-hash")
        manifest["dataset"]["observation_keys"] = ["3" * 64, "4" * 64]
        assert store.reserve(manifest, "disjoint")["status"] == "reserved"


def test_incomplete_holdout_observation_coverage_fails_closed(tmp_path):
    with ResearchStore(tmp_path / "store") as store:
        store.seal_holdout("a" * 64, "b" * 64, "c" * 64)
        with pytest.raises(HoldoutAccessError, match="coverage"):
            store.assert_development_access("d" * 64, "e" * 64, observation_keys=["1" * 64])


def test_export_is_hash_verified_readonly_and_never_overwrites_changes(tmp_path, manifest):
    with ResearchStore(tmp_path / "store") as store:
        record = store.reserve(manifest, "export")
        store.complete(record["experiment_id"], {"metrics": {"net_pnl": -1}, "equity_curve": [], "trades": []})
        destination = store.export(record["experiment_id"])
        assert destination == tmp_path / "store/results" / record["experiment_id"]
        assert json.loads((destination / "metrics.json").read_text())["net_pnl"] == -1
        assert stat.S_IMODE((destination / "manifest.json").stat().st_mode) == 0o400
        assert store.export(record["experiment_id"]) == destination
        (destination / "metrics.json").chmod(0o600)
        (destination / "metrics.json").write_text('{"net_pnl":100000}')
        with pytest.raises(ResearchConflict, match="differs"):
            store.export(record["experiment_id"])
        assert "100000" in (destination / "metrics.json").read_text()


def test_store_and_export_symlinks_are_rejected(tmp_path, manifest):
    root = tmp_path / "store"
    with ResearchStore(root) as store:
        record = store.reserve(manifest, "export")
        store.complete(record["experiment_id"], {"metrics": {}})
        link = tmp_path / "symlink-export"
        link.symlink_to(tmp_path / "other")
        with pytest.raises(ResearchStoreError):
            store.export(record["experiment_id"], link)
    linked_root = tmp_path / "linked-store"
    linked_root.symlink_to(root)
    with pytest.raises(ResearchStoreError):
        ResearchStore(linked_root)


@pytest.mark.parametrize("damage", ["observation", "state", "deleted_parent"])
def test_holdout_tampering_cannot_reopen_or_hide_rows_in_an_already_open_store(tmp_path, damage):
    args = "a" * 64, "b" * 64, "c" * 64
    with ResearchStore(tmp_path / "store") as store:
        store.seal_holdout(*args, observation_keys=["1" * 64])
        store.claim_holdout(*args, "first-claim")
        with sqlite3.connect(store.path) as connection:
            if damage == "observation":
                connection.execute("UPDATE protected_observations SET observation_key=?", ("2" * 64,))
            elif damage == "deleted_parent":
                connection.execute("DELETE FROM holdouts")
            else:
                connection.execute("UPDATE holdouts SET state='sealed',claim_id=NULL")
        with pytest.raises(ResearchStoreError, match="corrupt"):
            if damage in {"observation", "deleted_parent"}:
                store.assert_development_access("d" * 64, "e" * 64, observation_keys=["1" * 64])
            else:
                store.claim_holdout(*args, "second-claim")
        with pytest.raises(ResearchStoreError, match="unavailable"):
            store.list()


def test_result_directory_symlink_cannot_redirect_default_export(tmp_path, manifest):
    with ResearchStore(tmp_path / "store") as store:
        record = store.reserve(manifest, "redirect-export")
        store.complete(record["experiment_id"], {"metrics": {}})
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (store.root / "results").symlink_to(elsewhere)
        with pytest.raises(ResearchStoreError, match="symlink"):
            store.export(record["experiment_id"])
        assert list(elsewhere.iterdir()) == []
