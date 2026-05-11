"""Tool implementations + dispatch for the nemoclaw agent loop.

Each tool here corresponds to an entry in `agents/tools.yaml` over in the
Starforge repo — that file is the contract; this module is the
implementation. The split keeps runner.py focused on the LLM loop and
container lifecycle, and lets new tools land in one place with a clear
interface.

Contract:
- A tool is `async def tool_<name>(ctx: ToolContext, **params) -> str`
- The string return value is fed back into the LLM as the tool result
- Tools registered in `TOOL_REGISTRY` are dispatched by name from the loop
- `finish` is special — handled inline by the loop, not registered here
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx

log = logging.getLogger("nemoclaw.tools")

# Limits
HTTP_TIMEOUT = 10.0
HTTP_BODY_PREVIEW = 800
CALLBACK_TIMEOUT = 10.0
AGENT_TYPE_TIMEOUT = 15.0


@dataclass
class ToolContext:
    """Runtime data every tool may need. Built once per agent loop and
    threaded through each dispatch."""
    task_id: Optional[int]
    callback_url: str
    callback_token: str
    member_id: str        # str because it comes straight from env
    mode: str             # "investigation" | "comment_reply"


# Tools that are explicitly disabled in certain modes. The dispatch layer
# returns an instructive error string back to the LLM rather than running
# the call — this is what stops a chat-mode agent from clobbering task state.
MODE_BLOCKED: dict[str, set[str]] = {
    "comment_reply": {"set_task_status"},
}


# ---------- Individual tool implementations ----------

async def tool_http_get(ctx: ToolContext, url: str = "", **_: Any) -> str:
    if not url:
        return "http_get error: missing url"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url)
        body = r.text[:HTTP_BODY_PREVIEW]
        truncated = (
            "" if len(r.text) <= HTTP_BODY_PREVIEW
            else f"\n[truncated, total {len(r.text)} bytes]"
        )
        return (
            f"HTTP {r.status_code} from {url}\n"
            f"headers: {dict(r.headers)}\n"
            f"body:\n{body}{truncated}"
        )
    except httpx.TimeoutException:
        return f"HTTP timeout (>{HTTP_TIMEOUT}s) fetching {url}"
    except Exception as e:  # noqa: BLE001
        return f"HTTP error fetching {url}: {e}"


async def tool_set_task_status(ctx: ToolContext, status: Optional[str] = None, **_: Any) -> str:
    if not status:
        return "set_task_status error: missing status"
    return await _starforge_task_action(ctx, action_type="set_status", status=status)


async def tool_add_comment(ctx: ToolContext, body: Optional[str] = None, **_: Any) -> str:
    if not body:
        return "add_comment error: missing body"
    return await _starforge_task_action(ctx, action_type="comment", body=body)


async def tool_create_agent_type(
    ctx: ToolContext, spec: Optional[dict[str, Any]] = None, **_: Any
) -> str:
    """Forward a spec from the agent-builder to Starforge's /api/agent-types.
    Creates a draft; an admin must Activate before it goes live."""
    if not ctx.callback_url or not ctx.member_id:
        return "error: cannot draft agent type — missing callback_url or member id"
    if not isinstance(spec, dict):
        return f"error: spec must be an object, got {type(spec).__name__}"
    try:
        async with httpx.AsyncClient(timeout=AGENT_TYPE_TIMEOUT) as client:
            r = await client.post(
                f"{ctx.callback_url.rstrip('/')}/api/agent-types"
                f"?created_by_member_id={ctx.member_id}",
                json=spec,
                headers={"Authorization": f"Bearer {ctx.callback_token}"},
            )
        if r.status_code >= 400:
            return f"create_agent_type error: HTTP {r.status_code} {r.text[:300]}"
        return (
            f"ok: agent type '{spec.get('slug', '?')}' drafted. "
            "An admin must activate it before it appears in the team-member dropdown."
        )
    except Exception as e:  # noqa: BLE001
        return f"create_agent_type error: {e}"


# ---------- Shared callback to Starforge for task-bound actions ----------

async def _starforge_task_action(
    ctx: ToolContext, *, action_type: str, **payload_fields: Any
) -> str:
    if not ctx.callback_url or not ctx.member_id or ctx.task_id is None:
        return f"error: cannot perform {action_type} — missing callback_url, member id, or task id"
    payload = {
        "agent_member_id": int(ctx.member_id),
        "task_id": ctx.task_id,
        "actions": [{
            "type": action_type,
            **{k: v for k, v in payload_fields.items() if v is not None},
        }],
    }
    try:
        async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT) as client:
            r = await client.post(
                f"{ctx.callback_url.rstrip('/')}/api/agents/task-actions",
                json=payload,
                headers={"Authorization": f"Bearer {ctx.callback_token}"},
            )
        if r.status_code >= 400:
            return f"task-action error: HTTP {r.status_code} {r.text[:200]}"
        return f"ok: {action_type} applied"
    except Exception as e:  # noqa: BLE001
        return f"task-action error: {e}"


# ---------- Registry + dispatch ----------

ToolFn = Callable[..., Awaitable[str]]

TOOL_REGISTRY: dict[str, ToolFn] = {
    "http_get": tool_http_get,
    "set_task_status": tool_set_task_status,
    "add_comment": tool_add_comment,
    "create_agent_type": tool_create_agent_type,
}


async def execute_tool(
    tool: dict[str, Any], ctx: ToolContext
) -> tuple[str, bool]:
    """Run the tool described by `tool` (an object the LLM emitted, e.g.
    `{"tool": "http_get", "url": "..."}`). Returns `(result_string, is_finish)`.

    `finish` is special: it's not in the registry, and we signal back to the
    loop via the bool so the loop can break out cleanly.
    """
    name = tool.get("tool")
    if name == "finish":
        return "", True

    if name in MODE_BLOCKED.get(ctx.mode, set()):
        # Refuse and tell the LLM why — see the comment_reply guard above
        return (
            f"{name} is NOT AVAILABLE in {ctx.mode} mode. "
            "The task status will stay exactly where it is. "
            "Just call add_comment with your reply, then finish."
        ), False

    fn = TOOL_REGISTRY.get(name or "")
    if fn is None:
        return f"unknown tool: {name!r}", False

    params = {k: v for k, v in tool.items() if k != "tool"}
    try:
        return await fn(ctx, **params), False
    except TypeError as e:
        # Wrong-shaped params from the LLM (e.g. unexpected keyword)
        return f"{name} error: bad parameters: {e}", False


# ---------- LLM-output parsing ----------

def parse_tool_call(text: str) -> Optional[dict[str, Any]]:
    """Pull the first JSON object containing a 'tool' field out of an LLM
    response. Tolerates ```json fences and surrounding prose."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
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


# ---------- System-prompt addendum ----------
#
# Appended to the agent's system_prompt at the start of a tool-using run so
# the LLM knows the tool format and the required workflow. Keep this in sync
# with the registry above when adding tools.

TOOL_INSTRUCTIONS = """
You have been assigned a task. Use the tools below to investigate and report back.

RESPOND WITH EXACTLY ONE JSON OBJECT PER TURN, NO OTHER TEXT, NO MARKDOWN FENCES.

Available tools:
{"tool": "http_get", "url": "..."}                          — fetch a URL
{"tool": "set_task_status", "status": "in_progress"}        — also: "under_review", "done"
{"tool": "add_comment", "body": "..."}                      — post a comment with your findings
{"tool": "create_agent_type", "spec": {...}}                — (meta-agents only) draft a new agent type
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
