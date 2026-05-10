#!/usr/bin/env bash
# Rebuild the starforge:latest container image from the local Dockerfile.
set -e
cd "$(dirname "$0")/.."
docker build -t starforge:latest .
echo
echo "image built. Bring the stack up with:  docker compose up -d"
