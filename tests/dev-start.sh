#!/usr/bin/env bash
# Start the native uvicorn dev server in the background and wait for /healthz.
# Logs to tests/.uvicorn.log; PID stored in tests/.uvicorn.pid for dev-stop.sh.
set -e
cd "$(dirname "$0")/.."

if curl -fsS http://localhost:8000/healthz > /dev/null 2>&1; then
  echo "uvicorn already healthy on :8000 — nothing to do"
  exit 0
fi

mkdir -p tests
nohup python -m uvicorn app:app --port 8000 > tests/.uvicorn.log 2>&1 &
echo $! > tests/.uvicorn.pid
disown 2>/dev/null || true

for _ in $(seq 1 30); do
  if curl -fsS http://localhost:8000/healthz > /dev/null 2>&1; then
    echo "uvicorn ready (pid $(cat tests/.uvicorn.pid))"
    exit 0
  fi
  sleep 1
done

echo "ERROR: uvicorn did not become healthy within 30s — see tests/.uvicorn.log" >&2
tail -20 tests/.uvicorn.log >&2 || true
exit 1
