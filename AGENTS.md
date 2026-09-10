# Development scope

ATLAS will run on a server. This checkout is for development and offline
verification. Do not start application workers, broker sessions, dashboards,
startup jobs, or host services during this work. Deployment has not been
requested. Use fake clients and processes for lifecycle tests.

Read `docs/server-readiness.md` for the current implementation and remaining
host work. Historical host and Git instructions in `CLAUDE.md` describe a
previous setup; current work stays on the local development branch unless
the user requests publication or deployment.

The frozen hypotheses and evaluation rules in `FORWARD_TEST.md` govern
research. Do not run the one-time evaluator, change its criteria, retrain
frozen models, or rewrite historical research records as part of engineering
checks. `FINDINGS.md` records the research history.

Use `git ls-files` for the tracked inventory: historical model pickles and
`journal.db` are tracked even though they match `.gitignore`.

Verify with `uv sync --locked --group dev`, then
`uv run --locked python -m pytest -q -ra`. Private-data smoke tests require
an explicitly selected dataset. Run only the synthetic `demo.py` example
when demonstrating the pipeline locally.
