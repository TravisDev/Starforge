# nemoclaw/

Stub runtime container for Starforge AI-agent team members.

> **Status: stub.** Receives an agent snapshot at startup, exposes `/healthz`,
> doesn't yet do any LLM invocation. The real invocation path is Phase C work.
> This stub exists so Phase B (container lifecycle) has something concrete to
> spawn and test against.

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

| Method | Path        | What it does                                                       |
|--------|-------------|--------------------------------------------------------------------|
| GET    | `/healthz`  | Liveness. Returns `{ok, loaded_at, has_snapshot, agent_type}`.     |
| GET    | `/agent`    | The loaded snapshot as JSON (or an error if none was provided).    |
| POST   | `/invoke`   | Stub — returns "not yet implemented." Phase C wires the real path. |

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
