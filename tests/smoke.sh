#!/usr/bin/env bash
# End-to-end smoke test: app imports cleanly, schema migrations apply,
# server boots, /healthz passes, /api/projects rejects unauthenticated requests.
# Use this after schema or routing changes.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/.."

echo "[1/5] importing app..."
python -c "import app; print('  imports OK (schema migrations ran)')"

echo "[2/5] inspecting db..."
"$DIR/inspect-db.sh" | sed 's/^/  /'

echo "[3/5] restarting server..."
"$DIR/dev-restart.sh" | sed 's/^/  /'

echo "[4/5] /healthz..."
curl -fsS http://localhost:8000/healthz | sed 's/^/  /'
echo

echo "[5/5] /api/projects unauth check..."
code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/projects)
echo "  /api/projects (no cookie) -> HTTP $code"
if [ "$code" != "401" ]; then
  echo "FAIL: expected 401, got $code" >&2
  exit 1
fi

echo
echo "smoke OK"
