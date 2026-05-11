# Starforge — project orientation for Claude Code

A self-hosted alternative to Jira/OpenProject built around an AI-agent
team. FastAPI + SQLite + vanilla JS, no build step, runs on either
localhost or under Docker Compose.

GitHub: https://github.com/TravisDev/Starforge

## Read this first

- This file is auto-loaded into every conversation in this repo. Treat
  it as durable working memory for the project.
- The brand name is **Starforge**, never "Agent Board" (the on-disk
  folder is still `agent-board` for historical path reasons — don't
  rename it).
- The runtime sidecar is **nemoclaw** (NeMo Guardrails + Claude /
  Ollama). Each AI team member gets its own long-lived nemoclaw
  container provisioned on demand. Control plane (this Python app) and
  data plane (nemoclaw containers) are intentionally separate.

## Architecture in one paragraph

Tasks live in a project. Projects own team members (humans + AI
agents). Each AI agent is bound to an **agent type** defined under
`./agents/<slug>/`. When you assign a task to a running AI member, a
trigger fires a run; the run dispatches to that member's nemoclaw
container, which enters a tool-using LLM loop, calls back into
Starforge via bearer-token-authed endpoints to set status / post
comments / draft new agent types, then exits via the `finish` tool.

## File layout (what to grep where)

| Path | What's in it |
|------|--------------|
| `app.py` | Every FastAPI route, schema init, run dispatch, trigger logic |
| `auth.py` | Argon2id passwords, AES-256-GCM for secrets at rest, session mgmt, `current_user` / `current_admin` deps |
| `oidc.py` | OIDC discovery, PKCE, JWKS, ID-token validation |
| `runtime_adapter.py` | Abstract `RuntimeAdapter` + dataclasses |
| `runtime_docker.py` | Real Docker SDK adapter (uses `npipe` on Windows) |
| `runtime_fake.py` | In-memory test double — used by every pytest case |
| `nemoclaw/runner.py` | The agent container's FastAPI app, snapshot loading, LLM completion, agent loop |
| `nemoclaw/tools.py` | Tool implementations + registry + dispatch + `TOOL_INSTRUCTIONS` system-prompt addendum |
| `agents/<slug>/config.yaml` + `system_prompt.md` + `guardrails.yaml` | Per-agent-type definition. `agents/agent-builder/` is the meta-agent that drafts new agent types |
| `agents/tools.yaml` | Tool registry — the canonical list of available tools. PR review IS the security gate |
| `static/index.html` | Main board (Kanban + team pane + FAB + modals). Auto-refresh every 2s with signature-based diffing |
| `static/projects.html`, `static/settings.html`, `static/setup.html`, `static/login.html` | The other UI pages |
| `tests/` | pytest suite (112 passing as of latest) + dev-loop shell scripts |

## Daily dev loop

```bash
bash tests/run-tests.sh        # run the pytest suite
bash tests/dev-restart.sh      # restart the native uvicorn dev server
bash tests/inspect-db.sh       # quick SQLite snapshot
bash tests/smoke.sh            # end-to-end smoke (import → DB → restart → health)
```

After changes to `nemoclaw/`:

```bash
cd nemoclaw && docker build -q -t starforge-nemoclaw:dev . && cd ..
# then in the UI: Stop → Start the AI member to pick up the new image
```

Re-screenshot for the README after UI changes:

```bash
python tests/capture-screenshots.py
```

## Conventions (load-bearing)

- **Schema migrations** are idempotent `ALTER TABLE ADD COLUMN` inside
  `init_*_schema()` functions in `app.py`. SQLite, so most column adds
  are non-destructive.
- **Secrets at rest** use AES-256-GCM with `STARFORGE_KEY` (env var,
  or auto-generated to `secret.key` on first run).
- **Session cookies** carry random 256-bit tokens; only the SHA-256
  hash is stored in `sessions.token_hash` so a DB leak can't grant
  active sessions.
- **Tests use FakeRuntime** — never spin up real Docker in pytest.
  `tests/conftest.py` sets `STARFORGE_DISABLE_BACKGROUND_TASKS=1` so
  the health and image-update background loops stay off.
- **Tests use a fresh temp `STARFORGE_DATA_DIR`** — they never touch
  the real `board.db` or `secret.key`.
- **Don't commit secrets** — `.gitignore` excludes `secret.key`,
  `board.db`, `data/`, `tests/.uvicorn.*`.
- **Don't push without an explicit ask** beyond the first commit of a
  session. Pushing is a visible, hard-to-reverse action.

## Recent design decisions (not obvious from code)

- **Tool registry** lives in `agents/tools.yaml`. PR review is the
  security gate; there is intentionally no admin-approve UI for tools.
  When adding/removing a tool, edit this file in the same PR as the
  implementation.
- **Agent types are draft-by-default** when written by agent-builder.
  Admin must Activate via Settings before the new type appears in the
  New Team Member dropdown.
- **`set_task_status` is blocked in `comment_reply` mode** at the
  runtime level — chat threads don't move tasks. See
  `MODE_BLOCKED` in `nemoclaw/tools.py`.
- **Comments on AI-assigned tasks implicitly ping the assignee** — no
  `@` needed if the task already has an AI member. Explicit `@slug`
  still works for pinging others.
- **Member description is folded into the system prompt** as
  personality preamble via `STARFORGE_MEMBER_DESCRIPTION` env var.
  Edits require a container restart to take effect.
- **Save handler dirty-tracks fields** so a stale task modal can't
  overwrite an agent's `under_review` update.
- **Polling is signature-diffed** — UI re-renders only when something
  actually changed. Drag-and-drop pauses polling via an `isDragging`
  flag.
- **Health check reconciliation runs every 10s** — externally-killed
  containers flip to `not_provisioned` with `runtime_container_id`
  cleared so the next Start does a fresh provision.

## What's parked (intentionally not building)

- **C.2** NeMo Guardrails actual enforcement (today the manifest is
  decorative)
- **C.4** SSE / WebSocket streaming for long runs (2s polling works)
- **C.5** Scheduled / webhook triggers
- **D.2** Dynamic-load tools from external `tool.py` files
- **D.4** Real per-tool sandboxing (network/fs/resource limits)
- **Security hardening** (CSRF tokens, rate limiting on `/api/login`,
  account lockout, audit log) — necessary before exposing past
  localhost, not necessary for the current threat model

## Commit etiquette

- One concept per commit. Brief but complete messages — what changed
  and why.
- Always include a `Co-Authored-By: Claude Sonnet 4.6
  <noreply@anthropic.com>` trailer.
- Never update `git config` (the user manages their identity).
- Don't skip pre-commit hooks unless the user explicitly asks.

## When in doubt

The README's SysML section is the canonical design reference. If you
change the architecture, update it.
