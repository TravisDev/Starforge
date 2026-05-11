#!/usr/bin/env bash
# Run the pytest unit-test suite. Installs test deps first (idempotent — pip is fast on no-ops).
set -e
cd "$(dirname "$0")/.."

if ! python -c "import pytest" 2>/dev/null; then
  echo "installing test deps..."
  pip install -q -r tests/requirements.txt
fi

python -m pytest "$@"
