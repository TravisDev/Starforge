# tests/ — dev-loop scripts

Wrappers around the diagnostic and dev-cycle commands the team runs over and
over. Codifying them here keeps the dev loop consistent and avoids per-command
permission prompts in the agent harness.

All scripts assume they're invoked from anywhere — they `cd` to the project
root themselves. Run them via `bash tests/<name>.sh`.

| Script              | What it does                                                                 |
|---------------------|------------------------------------------------------------------------------|
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
