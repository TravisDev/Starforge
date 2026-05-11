"""Stub nemoclaw runtime.

A long-lived container that's intended to host one AI-agent team member from
Starforge. It receives an agent snapshot at startup (env var or mounted file),
exposes /healthz so the orchestrator can wait for readiness, and stubs out
/invoke until Phase C wires up the actual LLM dispatch.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI

SNAPSHOT_ENV = "AGENT_SNAPSHOT_JSON"
SNAPSHOT_FILE_ENV = "AGENT_SNAPSHOT_FILE"
DEFAULT_SNAPSHOT_PATH = "/run/agent-snapshot.json"


def _load_snapshot() -> Optional[dict[str, Any]]:
    raw = os.environ.get(SNAPSHOT_ENV)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[nemoclaw] {SNAPSHOT_ENV} is not valid JSON: {e}", file=sys.stderr)
            return None
    path = os.environ.get(SNAPSHOT_FILE_ENV, DEFAULT_SNAPSHOT_PATH)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[nemoclaw] could not read {path}: {e}", file=sys.stderr)
            return None
    return None


SNAPSHOT = _load_snapshot()
LOADED_AT = datetime.now(timezone.utc).isoformat()

if SNAPSHOT:
    _name = (SNAPSHOT.get("config") or {}).get("agent", {}).get("name", "unknown")
    _type = SNAPSHOT.get("agent_type", "?")
    print(f"[nemoclaw] loaded agent name={_name!r} type={_type!r}", file=sys.stderr)
else:
    print(
        f"[nemoclaw] WARNING: no snapshot loaded. Set {SNAPSHOT_ENV} or mount "
        f"a file at {DEFAULT_SNAPSHOT_PATH} (override with {SNAPSHOT_FILE_ENV}).",
        file=sys.stderr,
    )

app = FastAPI(title="nemoclaw (stub)", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "loaded_at": LOADED_AT,
        "has_snapshot": SNAPSHOT is not None,
        "agent_type": (SNAPSHOT or {}).get("agent_type"),
    }


@app.get("/agent")
def get_agent() -> dict[str, Any]:
    if not SNAPSHOT:
        return {"error": "no agent snapshot loaded"}
    return SNAPSHOT


@app.post("/invoke")
def invoke() -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "invocation not yet implemented in this stub — Phase C work",
    }
