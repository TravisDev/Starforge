"""Nemoclaw runtime — one container per Starforge AI-agent team member.

Phase C.1: real Claude invocation. No tools, no guardrails enforcement yet —
those land in C.2 / C.3. The container holds the agent snapshot at startup,
accepts /invoke calls, runs Claude in a background task, and POSTs results
back to Starforge via callback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Header
from pydantic import BaseModel

# ---------- Snapshot bootstrap (env or mounted file) ----------

SNAPSHOT_ENV = "AGENT_SNAPSHOT_JSON"
SNAPSHOT_FILE_ENV = "AGENT_SNAPSHOT_FILE"
DEFAULT_SNAPSHOT_PATH = "/run/agent-snapshot.json"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
STARFORGE_CALLBACK_TOKEN = os.environ.get("STARFORGE_CALLBACK_TOKEN", "")
STARFORGE_CALLBACK_URL_FALLBACK = os.environ.get("STARFORGE_CALLBACK_URL", "")

logging.basicConfig(level=logging.INFO, format="[nemoclaw] %(message)s")
log = logging.getLogger("nemoclaw")


def _load_snapshot() -> Optional[dict[str, Any]]:
    raw = os.environ.get(SNAPSHOT_ENV)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.error("%s is not valid JSON: %s", SNAPSHOT_ENV, e)
            return None
    path = os.environ.get(SNAPSHOT_FILE_ENV, DEFAULT_SNAPSHOT_PATH)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.error("could not read %s: %s", path, e)
            return None
    return None


SNAPSHOT = _load_snapshot()
LOADED_AT = datetime.now(timezone.utc).isoformat()

if SNAPSHOT:
    _name = (SNAPSHOT.get("config") or {}).get("agent", {}).get("name", "unknown")
    _type = SNAPSHOT.get("agent_type", "?")
    log.info("loaded agent name=%r type=%r", _name, _type)
else:
    log.warning("no snapshot loaded; /invoke will fail until one is provided")

# ---------- App ----------

app = FastAPI(title="nemoclaw", version="0.2.0")

# Track in-flight runs so /runs/{id} cancellation can interrupt them
_active_runs: dict[str, asyncio.Task] = {}


def _check_callback_token(authorization: Optional[str]) -> None:
    """Reject invocations not bearing the shared per-project token."""
    if not STARFORGE_CALLBACK_TOKEN:
        # No token configured → accept any caller. Acceptable for fully-private
        # docker networks; not safe if the container is reachable elsewhere.
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    if authorization[len("Bearer "):] != STARFORGE_CALLBACK_TOKEN:
        raise HTTPException(401, "bad bearer token")


class InvokeRequest(BaseModel):
    run_id: str
    callback_url: Optional[str] = None
    callback_token: Optional[str] = None
    inputs: dict[str, Any] = {}
    snapshot: dict[str, Any] = {}  # not used yet; lets per-run overrides land later


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "loaded_at": LOADED_AT,
        "has_snapshot": SNAPSHOT is not None,
        "agent_type": (SNAPSHOT or {}).get("agent_type"),
        "anthropic_key_present": bool(ANTHROPIC_API_KEY),
    }


@app.get("/agent")
def get_agent() -> dict[str, Any]:
    if not SNAPSHOT:
        return {"error": "no agent snapshot loaded"}
    return SNAPSHOT


@app.post("/invoke", status_code=202)
async def invoke(
    body: InvokeRequest,
    background: BackgroundTasks,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _check_callback_token(authorization)
    if SNAPSHOT is None:
        raise HTTPException(400, "no agent snapshot — container started without one")
    # Provider-specific key validation happens inside _run_agent; we only
    # gate here for anthropic to fail fast at request time.
    provider = ((SNAPSHOT.get("config") or {}).get("agent", {}).get("provider") or "anthropic").lower()
    if provider == "anthropic" and not ANTHROPIC_API_KEY:
        raise HTTPException(400, "ANTHROPIC_API_KEY not configured on this container")

    callback_url = body.callback_url or STARFORGE_CALLBACK_URL_FALLBACK
    callback_token = body.callback_token or STARFORGE_CALLBACK_TOKEN
    if not callback_url:
        raise HTTPException(400, "no callback_url provided")

    task = asyncio.create_task(_run_agent(
        run_id=body.run_id,
        inputs=body.inputs,
        callback_url=callback_url,
        callback_token=callback_token,
    ))
    _active_runs[body.run_id] = task
    return {"ok": True, "run_id": body.run_id, "status": "running"}


@app.delete("/runs/{run_id}", status_code=200)
async def cancel_run(
    run_id: str,
    authorization: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    _check_callback_token(authorization)
    task = _active_runs.get(run_id)
    if not task or task.done():
        return {"ok": True, "already_finished": True}
    task.cancel()
    return {"ok": True}


# ---------- Claude invocation ----------

async def _call_anthropic(*, model: str, system_prompt: str, user_content: str) -> dict[str, Any]:
    import anthropic  # type: ignore
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = await asyncio.to_thread(
        client.messages.create,
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "\n".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    usage = getattr(resp, "usage", None)
    return {
        "output": text,
        "tokens_in": getattr(usage, "input_tokens", None) if usage else None,
        "tokens_out": getattr(usage, "output_tokens", None) if usage else None,
    }


async def _call_openai_compatible(
    *, model: str, system_prompt: str, user_content: str,
    base_url: Optional[str], api_key: str,
) -> dict[str, Any]:
    """Works for OpenAI itself and any OpenAI-compatible endpoint (Ollama, vLLM, etc.).

    Ollama at http://host:11434/v1/chat/completions speaks this wire format,
    so the same code path covers local + cloud."""
    from openai import OpenAI  # type: ignore
    # Ollama doesn't validate api_key but the SDK insists on one being non-empty
    client = OpenAI(api_key=api_key or "unused", base_url=base_url) if base_url else OpenAI(api_key=api_key)
    resp = await asyncio.to_thread(
        client.chat.completions.create,
        model=model,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    usage = getattr(resp, "usage", None)
    return {
        "output": text,
        "tokens_in": getattr(usage, "prompt_tokens", None) if usage else None,
        "tokens_out": getattr(usage, "completion_tokens", None) if usage else None,
    }


async def _run_agent(
    *, run_id: str, inputs: dict[str, Any],
    callback_url: str, callback_token: str,
) -> None:
    """Background task: build messages from snapshot + inputs, call the configured
    LLM provider, POST result back to Starforge."""
    try:
        agent_block = (SNAPSHOT or {}).get("config", {}).get("agent", {}) or {}
        system_prompt = (SNAPSHOT or {}).get("system_prompt", "") or agent_block.get("system_prompt", "") or ""
        if isinstance(system_prompt, dict):
            # Defensive — shouldn't happen post-resolver
            system_prompt = json.dumps(system_prompt)
        model = agent_block.get("model", "claude-sonnet-4-6")
        provider = (agent_block.get("provider") or "anthropic").lower()
        provider_endpoint = agent_block.get("provider_endpoint", "")

        # For C.1 we frame the run as a single user turn containing the inputs.
        user_content = json.dumps(inputs, indent=2)

        if provider == "anthropic":
            if not ANTHROPIC_API_KEY:
                raise RuntimeError("ANTHROPIC_API_KEY is not set on this container")
            llm_result = await _call_anthropic(
                model=model, system_prompt=system_prompt, user_content=user_content,
            )
        elif provider in ("openai", "ollama"):
            base_url = provider_endpoint or None
            api_key = os.environ.get(
                "OPENAI_API_KEY",
                ANTHROPIC_API_KEY,  # not used by Ollama but the SDK needs something
            )
            llm_result = await _call_openai_compatible(
                model=model, system_prompt=system_prompt, user_content=user_content,
                base_url=base_url, api_key=api_key,
            )
        else:
            raise RuntimeError(
                f"unknown provider: {provider!r} (expected anthropic | openai | ollama)"
            )

        result: dict[str, Any] = {"status": "succeeded", **llm_result}
    except asyncio.CancelledError:
        result = {"status": "failed", "error": "cancelled"}
    except Exception as e:  # noqa: BLE001
        log.exception("run %s failed", run_id)
        result = {"status": "failed", "error": str(e)}

    # Callback to Starforge with the result. Best-effort retry once.
    headers = {"Authorization": f"Bearer {callback_token}"} if callback_token else {}
    url = f"{callback_url.rstrip('/')}/api/agent-runs/{run_id}/result"
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(url, json=result, headers=headers)
            if r.status_code < 400:
                break
            log.warning("callback returned %s; attempt %s body=%s", r.status_code, attempt, r.text[:200])
        except Exception as e:  # noqa: BLE001
            log.warning("callback attempt %s failed: %s", attempt, e)
        await asyncio.sleep(1.0)

    _active_runs.pop(run_id, None)


if __name__ == "__main__":
    # Useful when running bare-metal for testing; the Docker image uses uvicorn
    # via the CMD line.
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)  # noqa: S104
