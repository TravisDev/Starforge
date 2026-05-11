# nemoclaw/

Stub runtime container for Starforge AI-agent team members.

> **Status: C.1 — real Claude invocation, no tools or guardrails yet.**
> /invoke now calls the Claude API with the agent's system prompt + your
> inputs and posts the result back to Starforge. Tools (C.3) and NeMo
> Guardrails enforcement (C.2) are next.

## How it fits

Starforge's per-team-member runtime model:

1. Operator creates a team member of type `ai_agent` bound to a slug under
   `../agents/<slug>/` (e.g. `network-engineer`).
2. Starforge resolves the agent snapshot from `./agents/` (already
   implemented — see `app.py:resolve_agent_snapshot`).
3. Starforge's Docker adapter (Phase B.2) pulls this image and starts a
   container, passing the snapshot in via `AGENT_SNAPSHOT_JSON`.
4. The container stays running for as long as the team member exists.
   Restart/stop/remove follow the member's lifecycle.
5. Invocations (Phase C) will be HTTP calls into this container's `/invoke`.

## Inputs

Snapshot can be passed two ways:

- **`AGENT_SNAPSHOT_JSON` env var** — full JSON inline. Easiest for spawning
  from another service.
- **`AGENT_SNAPSHOT_FILE` env var** — path to a mounted JSON file. Default:
  `/run/agent-snapshot.json`. Useful when the snapshot is too large for
  command-line / env-var practical limits.

The snapshot shape is whatever `Starforge.resolve_agent_snapshot()` produces.

## Endpoints

| Method | Path              | What it does                                                             |
|--------|-------------------|--------------------------------------------------------------------------|
| GET    | `/healthz`        | Liveness. Returns `{ok, loaded_at, has_snapshot, agent_type, anthropic_key_present}`. |
| GET    | `/agent`          | The loaded snapshot as JSON (or an error if none was provided).          |
| POST   | `/invoke`         | Accepts `{run_id, callback_url, callback_token, inputs, snapshot}`. Returns 202 immediately and runs Claude in the background; POSTs the final result to `{callback_url}/api/agent-runs/{run_id}/result`. |
| DELETE | `/runs/{run_id}`  | Best-effort cancel of an in-flight run.                                  |

All write endpoints require `Authorization: Bearer {STARFORGE_CALLBACK_TOKEN}` if that env var is set.

## Required env vars for C.1

| Var | Purpose |
|---|---|
| `AGENT_SNAPSHOT_JSON` *or* `AGENT_SNAPSHOT_FILE` | The agent snapshot the container holds. |
| `ANTHROPIC_API_KEY` | Used to call the Claude API. |
| `STARFORGE_CALLBACK_TOKEN` | Shared bearer token (per-project). Validates incoming /invoke and is forwarded as Authorization on the result callback. |
| `STARFORGE_CALLBACK_URL` | (Optional fallback) — usually overridden per-run by the `callback_url` field in the request body. |

## Building

```bash
cd nemoclaw
docker build -t starforge-nemoclaw:dev .
```

## Running manually

```bash
docker run --rm -p 8080:8080 \
  -e AGENT_SNAPSHOT_JSON='{"agent_type":"test","config":{"agent":{"name":"demo"}}}' \
  starforge-nemoclaw:dev

# in another shell
curl http://localhost:8080/healthz
curl http://localhost:8080/agent
```

## Phase C — what gets added later

- Real LLM client (Anthropic SDK) wired to the agent's `model` field
- NeMo Guardrails integration for the rails defined in `guardrails`
- Tool registry (loads named tools, runs them under action-rail gates)
- Memory backend (loads named memory stores, persists per-container)
- Real `/invoke` that takes inputs, runs the agent, returns structured output
- `/runs/{id}` for run history / streaming output
