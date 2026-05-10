#!/usr/bin/env bash
# Stop + start the native uvicorn dev server.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/dev-stop.sh"
sleep 1
"$DIR/dev-start.sh"
