#!/usr/bin/env bash
# Stop the native uvicorn dev server. Tries the saved PID first, falls back
# to anything bound to TCP 8000 (Windows: PowerShell Get-NetTCPConnection).
cd "$(dirname "$0")/.."

stopped=0

if [ -f tests/.uvicorn.pid ]; then
  pid=$(cat tests/.uvicorn.pid 2>/dev/null || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null && echo "stopped pid $pid (saved)" && stopped=1
  fi
  rm -f tests/.uvicorn.pid
fi

# Fallback / belt-and-suspenders: kill anything still listening on 8000
if command -v powershell.exe > /dev/null 2>&1 || command -v powershell > /dev/null 2>&1; then
  powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force -ErrorAction SilentlyContinue }" > /dev/null 2>&1 || true
elif command -v lsof > /dev/null 2>&1; then
  lsof -ti :8000 | xargs -r kill 2>/dev/null || true
fi

if [ "$stopped" -eq 0 ]; then
  echo "stopped (via port 8000 lookup)"
fi
