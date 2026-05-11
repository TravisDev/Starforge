# tests/ — unit tests + dev-loop scripts

Two kinds of things live here:

1. **Unit tests** (`test_*.py`) — pytest suite covering app behavior. Run with `bash tests/run-tests.sh`. These should pass before any change is committed.
2. **Dev-loop scripts** (`*.sh`) — wrappers around recurring diagnostic / lifecycle commands so they live in the repo and can be granted blanket permission rather than re-prompting on every invocation.

## Unit tests

```bash
# one-time: install pytest
pip install -r tests/requirements.txt

# run the suite
bash tests/run-tests.sh
```

The fixtures in `conftest.py` set `STARFORGE_DATA_DIR` to a fresh temp directory **before** importing the app, so tests never touch your real `board.db` / `secret.key`. The TestClient runs in-process — no need for the dev server to be running.

Current coverage:
- `test_agent_types.py` — registry scan, endpoint auth, team-member validation against the registry, schema migration check.

When you add a feature, add a `test_<feature>.py` alongside it. Verify pass with `bash tests/run-tests.sh` before committing.

## Dev-loop scripts

All scripts assume they're invoked from anywhere — they `cd` to the project
root themselves. Run them via `bash tests/<name>.sh`.

| Script              | What it does                                                                 |
|---------------------|------------------------------------------------------------------------------|
| `run-tests.sh`      | Run the pytest suite (installs `tests/requirements.txt` first if needed).    |
| `dev-start.sh`      | Start uvicorn on :8000 in the background; wait up to 30 s for `/healthz`.    |
| `dev-stop.sh`       | Stop uvicorn (saved PID, falling back to whatever's bound to TCP 8000).      |
| `dev-restart.sh`    | `dev-stop.sh` + `dev-start.sh`. Use after Python or schema changes.          |
| `health.sh`         | `curl /healthz`. Quick liveness check.                                       |
| `inspect-db.sh`     | Pretty-print SQLite row counts + key rows (projects, users, team, tasks).    |
| `smoke.sh`          | Full end-to-end smoke: import → DB inspect → restart → endpoint checks.      |
| `open.sh [URL]`     | Open Chrome to the URL (defaults to `http://localhost:8000`).                |
| `docker-rebuild.sh` | Rebuild the `starforge:latest` image from the local Dockerfile.              |

## Files written by the scripts

`dev-start.sh` writes `tests/.uvicorn.pid` and `tests/.uvicorn.log`. Both are
git-ignored.

## Adding a new dev-loop command

If you find yourself running the same command three times in a session, drop
it in here as a script. Cheaper than re-typing and easier to grant blanket
permission for `bash tests/*.sh` in the agent harness.
