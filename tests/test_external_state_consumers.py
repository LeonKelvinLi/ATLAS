"""Server paths must work outside the checkout, without starting any runtime."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap


PROJECT_DIR = Path(__file__).resolve().parent.parent


def test_external_state_readers_and_log_writers_in_fresh_process(tmp_path):
    # URI punctuation catches SQLite's file: URI escaping; nesting catches
    # writers that create logs/ but assume the state volume already exists.
    state = tmp_path / "external state #%" / "nested"
    models = tmp_path / "frozen models"
    env_file = tmp_path / "config" / "atlas.env"
    env_file.parent.mkdir()
    env_file.write_text("ATLAS_PATH_TEST_SENTINEL=external-config\n")
    env = os.environ.copy()
    env.update({
        "ATLAS_STATE_DIR": str(state),
        "ATLAS_MODEL_DIR": str(models),
        "ATLAS_ENV_FILE": str(env_file),
        "PYTHONPATH": str(PROJECT_DIR),
    })
    env.pop("ATLAS_BACKUP_DIR", None)
    env.pop("ATLAS_PATH_TEST_SENTINEL", None)
    script = textwrap.dedent("""
        import os
        import pickle
        from pathlib import Path
        import socket
        import sqlite3

        def no_network(*args, **kwargs):
            raise AssertionError("path checks must not contact a network")

        socket.socket.connect = no_network
        socket.socket.connect_ex = no_network

        import catchup_gaps
        import extra_collect
        import feature_cache
        import health_report
        import mission_control as mc
        import probe_execution
        import probe_order_record
        import quote_recorder
        import research_payout_landscape
        import research_universe_profile
        import run_once
        import server
        import settle_missing
        import shadow_eval
        import tail_calibration
        import runtime_paths as paths

        state = Path(os.environ["ATLAS_STATE_DIR"])
        logs = state / "logs"
        models = Path(os.environ["ATLAS_MODEL_DIR"])
        assert not state.exists(), "imports must not initialize live state"
        assert paths.PROJECT_DIR != state
        assert mc.MARKET_DB == state / "market.duckdb"
        assert mc.JOURNAL_DB == state / "journal.db"
        assert feature_cache.CACHE_DIR == state / "cache" / "features"
        assert mc.MODELS_DIR == models
        assert server.ENV_FILE == run_once.ENV_FILE == paths.ENV_FILE
        assert os.environ["ATLAS_PATH_TEST_SENTINEL"] == "external-config"
        assert all(path.is_relative_to(logs) for path in [
            mc.HEARTBEAT_PATH, mc.SIGNALS_PATH, mc.SUPERVISOR_LOG,
            catchup_gaps.LOG, extra_collect.LOG, health_report.LOG_PATH,
            health_report.HEALTH_PATH, probe_execution.OUT_PATH,
            probe_order_record.OUT_DIR, probe_order_record.SIGNAL_LOG,
            quote_recorder.log_path("EUR/USD"),
            research_payout_landscape.OUT_PATH,
            research_universe_profile.OUT_PATH, settle_missing.LOG_PATH,
            shadow_eval.CROSS_PATH,
        ])

        # Invoke only filesystem helpers, never main(), app/server runners,
        # collectors, broker clients, trading logic, or forward evaluation.
        catchup_gaps.log("offline path test")
        extra_collect._log("offline external collection log test")
        assert "offline path test" in (logs / "catchup.log").read_text()
        assert "offline external" in (logs / "extra_collect.log").read_text()
        mc.SIGNALS_PATH.write_text('{"asset":"fixture"}\\n')
        assert mc.read_jsonl(mc.SIGNALS_PATH) == [{"asset": "fixture"}]
        shadow_eval.CROSS_PATH.write_text('{"ts":123}\\n')
        assert shadow_eval.load_cross_section() == [{"ts": 123}]
        quote_recorder.log_path("EURUSD").write_text('{"ts":123,"q":1.25}\\n')
        assert quote_recorder.read_quotes("EURUSD", 100, 200) == [(123, 1.25)]

        import duckdb
        db = duckdb.connect(str(mc.MARKET_DB))
        db.execute("CREATE TABLE candles (to_ts BIGINT)")
        db.execute("INSERT INTO candles VALUES (123)")
        db.execute("CREATE TABLE payout_snapshots (ts_epoch BIGINT)")
        db.close()
        assert mc.candle_freshness()["latest_to_ts"] == 123
        journal = sqlite3.connect(mc.JOURNAL_DB)
        journal.execute("CREATE TABLE runs (id INTEGER)")
        journal.execute("INSERT INTO runs VALUES (1)")
        journal.commit()
        journal.close()
        assert mc.journal_counts() == {"exists": True, "runs": 1}

        models.mkdir()
        with (models / "h2-fixture.pkl").open("wb") as fh:
            pickle.dump({"meta": {"assets": ["fixture"]}}, fh)
        assert mc.frozen_universe() == {"fixture"}
        assert not (state / "models").exists()
        print("external state verified")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "external state verified" in result.stdout
