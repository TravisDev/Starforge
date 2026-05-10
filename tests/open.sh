#!/usr/bin/env bash
# Open the local dev server in Chrome (Windows-only via `start`).
URL="${1:-http://localhost:8000}"
if command -v start > /dev/null 2>&1; then
  start chrome "$URL"
elif command -v cmd.exe > /dev/null 2>&1; then
  cmd.exe //c start chrome "$URL"
else
  echo "Open this URL manually: $URL"
fi
