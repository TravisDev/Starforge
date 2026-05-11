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
STARFORGE_MEMBER_ID = os.environ.get("STARFORGE_MEMBER_ID", "")

# Task-mode tool loop limits
TOOL_LOOP_MAX_ITER = 12
TOOL_HTTP_TIMEOUT = 10.0
TOOL_HTTP_BODY_PREVIEW = 800

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

async def _llm_completion(
    *, messages: list[dict[str, Any]], provider: str, model: str,
    provider_endpoint: str,
) -> tuple[str, Optional[int], Optional[int]]:
    """One round-trip to the configured LLM. Returns (text, tokens_in, tokens_out)."""
    if provider == "anthropic":
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set on this container")
        import anthropic  # type: ignore
        # Anthropic expects system as a top-level param, not in messages
        system_msgs = [m["content"] for m in messages if m["role"] == "system"]
        non_system = [m for m in messages if m["role"] != "system"]
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = await asyncio.to_thread(
            client.messages.create,
            model=model,
            max_tokens=1024,
            system="\n\n".join(system_msgs) if system_msgs else "",
            messages=non_system,
        )
        text = "\n".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", None) == "text"
        ).strip()
        usage = getattr(resp, "usage", None)
        return (
            text,
            getattr(usage, "input_tokens", None) if usage else None,
            getattr(usage, "output_tokens", None) if usage else None,
        )
    if provider in ("openai", "ollama"):
        from openai import OpenAI  # type: ignore
        base_url = provider_endpoint or None
        api_key = os.environ.get("OPENAI_API_KEY", "unused")
        client = OpenAI(api_key=api_key or "unused", base_url=base_url) if base_url else OpenAI(api_key=api_key)
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=model,
            max_tokens=1024,
            messages=messages,
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        usage = getattr(resp, "usage", None)
        return (
            text,
            getattr(usage, "prompt_tokens", None) if usage else None,
            getattr(usage, "completion_tokens", None) if usage else None,
        )
    raise RuntimeError(f"unknown provider: {provider!r}")


_TOOL_INSTRUCTIONS = """
You have been assigned a task. Use the tools below to investigate and report back.

RESPOND WITH EXACTLY ONE JSON OBJECT PER TURN, NO OTHER TEXT, NO MARKDOWN FENCES.

Available tools:
{"tool": "http_get", "url": "..."}                          — fetch a URL
{"tool": "set_task_status", "status": "in_progress"}        — also: "under_review", "done"
{"tool": "add_comment", "body": "..."}                      — post a comment with your findings
{"tool": "finish"}                                          — end the run

REQUIRED WORKFLOW:
1. First turn: call set_task_status with status="in_progress"
2. Investigation turns: call http_get (or other tools) to gather evidence
3. When you have findings: call add_comment with a clear, evidence-backed summary
4. Then: call set_task_status with status="under_review"
5. Then: call finish

After each tool call I will tell you the result; then respond with the next tool call.
DO NOT WRITE PROSE. ONLY ONE JSON OBJECT PER RESPONSE.
""".strip()


def _parse_tool_call(text: str) -> Optional[dict[str, Any]]:
    """Pull the first JSON object containing a 'tool' field out of the LLM response."""
    if not text:
        return None
    # Strip markdown fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove ``` ... ``` wrappers
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    # Try direct parse first
    for candidate in (cleaned, _extract_first_json_object(cleaned)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and "tool" in obj:
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _extract_first_json_object(text: str) -> Optional[str]:
    """Best-effort: find the first {...} that's a balanced JSON object."""
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start: i + 1]
    return None


async def _tool_http_get(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=TOOL_HTTP_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url)
        body = r.text[:TOOL_HTTP_BODY_PREVIEW]
        truncated = "" if len(r.text) <= TOOL_HTTP_BODY_PREVIEW else f"\n[truncated, total {len(r.text)} bytes]"
        return f"HTTP {r.status_code} from {url}\nheaders: {dict(r.headers)}\nbody:\n{body}{truncated}"
    except httpx.TimeoutException:
        return f"HTTP timeout (>{TOOL_HTTP_TIMEOUT}s) fetching {url}"
    except Exception as e:  # noqa: BLE001
        return f"HTTP error fetching {url}: {e}"


async def _tool_task_action(
    *, action_type: str, task_id: int, callback_url: str, callback_token: str,
    status: Optional[str] = None, body: Optional[str] = None,
) -> str:
    if not callback_url or not STARFORGE_MEMBER_ID:
        return f"error: cannot perform {action_type} — missing callback_url or member id"
    payload = {
        "agent_member_id": int(STARFORGE_MEMBER_ID),
        "task_id": task_id,
        "actions": [
            {"type": action_type, **({"status": status} if status else {}),
                                  **({"body": body} if body else {})}
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{callback_url.rstrip('/')}/api/agents/task-actions",
                json=payload,
                headers={"Authorization": f"Bearer {callback_token}"},
            )
        if r.status_code >= 400:
            return f"task-action error: HTTP {r.status_code} {r.text[:200]}"
        return f"ok: {action_type} applied"
    except Exception as e:  # noqa: BLE001
        return f"task-action error: {e}"


async def _run_agent(
    *, run_id: str, inputs: dict[str, Any],
    callback_url: str, callback_token: str,
) -> None:
    """Tool-using loop when invoked with a task_id, otherwise single-shot Q+A."""
    try:
        agent_block = (SNAPSHOT or {}).get("config", {}).get("agent", {}) or {}
        system_prompt = (SNAPSHOT or {}).get("system_prompt", "") or agent_block.get("system_prompt", "") or ""
        if isinstance(system_prompt, dict):
            system_prompt = json.dumps(system_prompt)
        model = agent_block.get("model", "claude-sonnet-4-6")
        provider = (agent_block.get("provider") or "anthropic").lower()
        provider_endpoint = agent_block.get("provider_endpoint", "")

        task_id = inputs.get("task_id")
        is_task_mode = bool(task_id)

        if not is_task_mode:
            # Manual run: legacy single-shot behavior
            user_content = json.dumps(inputs, indent=2)
            text, t_in, t_out = await _llm_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                provider=provider, model=model, provider_endpoint=provider_endpoint,
            )
            result: dict[str, Any] = {
                "status": "succeeded", "output": text,
                "tokens_in": t_in, "tokens_out": t_out,
            }
        else:
            # Task mode: tool-using loop. Two sub-modes based on inputs.mode:
            #   "comment_reply" → someone @-mentioned you in a comment. Answer
            #     the latest message in a comment, then finish. DO NOT change
            #     status — this is a discussion, not the main investigation.
            #   default → full task investigation (in_progress → tools → comment
            #     → under_review → finish).
            mode = inputs.get("mode") or "investigation"
            prior = inputs.get("prior_comments") or []

            if mode == "comment_reply":
                triggering = inputs.get("triggering_comment") or {}
                triggering_body = (triggering.get("body") or "").strip()
                triggering_author = triggering.get("author_name", "the user")
                # Most-recent-last ordering: put the question last so it dominates
                # the model's attention. Keep the thread tight; minimize ceremony.
                intro_parts = [
                    f"You are in a chat on task #{task_id} (\"{inputs.get('task_title', '')}\").",
                    "Earlier conversation:",
                ]
                if not prior:
                    intro_parts.append("  (no prior messages)")
                else:
                    # Skip the last entry — that IS the triggering message, we'll spotlight it.
                    for c in prior[:-1] if prior else []:
                        kind = c.get("author_kind", "user")
                        name = c.get("author_name", "?")
                        body = (c.get("body") or "").strip()
                        intro_parts.append(f"  [{kind}: {name}] {body}")
                intro_parts.append("")
                intro_parts.append(
                    f">>> {triggering_author} just said to you: {triggering_body!r}"
                )
                intro_parts.append("")
                intro_parts.append(
                    "Respond to what THEY ACTUALLY SAID. Address their specific words.\n"
                    "- If they're questioning your prior reasoning, re-examine it honestly. "
                    "Acknowledge uncertainty if you have it.\n"
                    "- If they're asking a meta question (\"are you reading this?\", "
                    "\"are you sure?\"), answer it directly first, then engage with substance.\n"
                    "- If they're suggesting something (e.g. \"could chrome be the problem?\"), "
                    "actually evaluate that suggestion — don't just repeat what you've already said.\n"
                    "- Use http_get only if it materially helps answer THIS specific question.\n"
                    "- DO NOT call set_task_status. This is a chat, not a workflow step.\n"
                    "- Keep it brief and conversational. Match their tone.\n"
                    "- Output: one add_comment call with your reply, then finish. That's it."
                )
                task_intro = "\n".join(intro_parts)
            else:
                intro_parts = [
                    f"You have been assigned task #{task_id}:",
                    f"Title: {inputs.get('task_title', '')}",
                    f"Description: {inputs.get('task_description', '')}",
                ]
                if prior:
                    intro_parts.append("")
                    intro_parts.append(
                        f"PRIOR COMMENT HISTORY on this task ({len(prior)} entries) — "
                        "this is a re-try. Read carefully; incorporate the feedback "
                        "below into your fresh investigation."
                    )
                    for c in prior:
                        kind = c.get("author_kind", "user")
                        name = c.get("author_name", "?")
                        body = (c.get("body") or "").strip()
                        when = (c.get("created_at") or "")[:19]
                        intro_parts.append(f"  · [{kind}: {name} @ {when}] {body}")
                    intro_parts.append("")
                    intro_parts.append(
                        "If a previous run's findings appear above, do NOT just repeat them. "
                        "Look for what the human reviewer questioned, what evidence is missing, "
                        "or what new angle to try."
                    )
                intro_parts.append("")
                intro_parts.append("Start by setting status to in_progress.")
                task_intro = "\n".join(intro_parts)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt + "\n\n" + _TOOL_INSTRUCTIONS},
                {"role": "user", "content": task_intro},
            ]
            total_in = 0
            total_out = 0
            outputs: list[str] = []
            finished = False
            for i in range(TOOL_LOOP_MAX_ITER):
                text, t_in, t_out = await _llm_completion(
                    messages=messages, provider=provider, model=model,
                    provider_endpoint=provider_endpoint,
                )
                if t_in: total_in += t_in
                if t_out: total_out += t_out
                messages.append({"role": "assistant", "content": text})
                tool = _parse_tool_call(text)
                if not tool:
                    outputs.append(f"[iter {i+1}] non-tool response: {text[:300]}")
                    # Nudge the model back to JSON
                    messages.append({"role": "user", "content":
                        "Your response was not a single JSON tool call. "
                        "Respond with EXACTLY one JSON object like "
                        '{"tool": "...", ...} and nothing else.'})
                    continue
                name = tool.get("tool")
                outputs.append(f"[iter {i+1}] {name}: {json.dumps(tool)[:200]}")
                if name == "finish":
                    finished = True
                    break
                elif name == "http_get":
                    url = tool.get("url", "")
                    res = await _tool_http_get(url)
                elif name == "set_task_status":
                    res = await _tool_task_action(
                        action_type="set_status", task_id=task_id,
                        callback_url=callback_url, callback_token=callback_token,
                        status=tool.get("status"),
                    )
                elif name == "add_comment":
                    res = await _tool_task_action(
                        action_type="comment", task_id=task_id,
                        callback_url=callback_url, callback_token=callback_token,
                        body=tool.get("body"),
                    )
                else:
                    res = f"unknown tool: {name!r}"
                messages.append({"role": "user", "content": f"Tool result: {res}"})
            result = {
                "status": "succeeded" if finished else "succeeded",
                "output": "\n".join(outputs) + ("\n[max iterations reached]" if not finished else ""),
                "tokens_in": total_in or None,
                "tokens_out": total_out or None,
            }
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
