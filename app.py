"""Starforge — task API + auth + OIDC SSO + admin settings."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets as secrets_lib
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

from auth import decrypt as aes_decrypt
from auth import encrypt as aes_encrypt
from runtime_adapter import RuntimeAdapter

log = logging.getLogger("starforge")

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

import auth
import oidc
from auth import (
    attach_request_meta,
    clear_session_cookie,
    create_session,
    create_user,
    current_admin,
    current_user,
    db,
    get_user_by_email,
    hash_password,
    init_auth_schema,
    needs_rehash,
    now_iso,
    revoke_session,
    revoke_session_id,
    set_session_cookie,
    touch_login,
    user_count,
    verify_password,
)

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
AGENTS_DIR = ROOT / "agents"

VALID_STATUSES = {"todo", "in_progress", "under_review", "done"}


def init_projects_schema() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '#6ea8fe',
                default_assignee TEXT NOT NULL DEFAULT '',
                auto_archive_done_days INTEGER NOT NULL DEFAULT 0,
                is_archived INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "runtime_config" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN runtime_config TEXT NOT NULL DEFAULT '{}'")
        if "runtime_secrets_enc" not in cols:
            # AES-256-GCM ciphertext of a JSON dict containing:
            # { "anthropic_api_key": "...", "callback_token": "..." }
            conn.execute("ALTER TABLE projects ADD COLUMN runtime_secrets_enc BLOB")


def ensure_default_project() -> int:
    with db() as conn:
        row = conn.execute("SELECT id FROM projects WHERE slug = 'default'").fetchone()
        if row:
            return row["id"]
        ts = now_iso()
        cur = conn.execute(
            """INSERT INTO projects (slug, name, description, color, default_assignee,
               auto_archive_done_days, is_archived, created_by, created_at, updated_at)
               VALUES ('default', 'Default', 'Default project (auto-created on upgrade)',
                       '#6ea8fe', '', 0, 0, NULL, ?, ?)""",
            (ts, ts),
        )
        return cur.lastrowid


def init_tasks_schema() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'todo'
                    CHECK(status IN ('todo','in_progress','under_review','done')),
                assignee TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "created_by" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER")
        if "project_id" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN project_id INTEGER")
        if "assignee_id" not in cols:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN assignee_id INTEGER "
                "REFERENCES team_members(id) ON DELETE SET NULL"
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON tasks(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assignee ON tasks(assignee)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assignee_id ON tasks(assignee_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_project ON tasks(project_id)")
    default_id = ensure_default_project()
    with db() as conn:
        conn.execute(
            "UPDATE tasks SET project_id = ? WHERE project_id IS NULL", (default_id,)
        )


def init_team_members_schema() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS team_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'human'
                    CHECK(type IN ('human','ai_agent')),
                email TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '#6ea8fe',
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                config TEXT NOT NULL DEFAULT '{}',
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(team_members)").fetchall()}
        if "agent_type" not in cols:
            conn.execute("ALTER TABLE team_members ADD COLUMN agent_type TEXT")
        # Per-member runtime container state — populated by the Docker adapter (Phase B.2)
        if "runtime_status" not in cols:
            conn.execute(
                "ALTER TABLE team_members ADD COLUMN runtime_status TEXT NOT NULL DEFAULT 'not_provisioned'"
            )
        if "runtime_container_id" not in cols:
            conn.execute("ALTER TABLE team_members ADD COLUMN runtime_container_id TEXT")
        if "runtime_endpoint" not in cols:
            conn.execute("ALTER TABLE team_members ADD COLUMN runtime_endpoint TEXT")
        if "runtime_error" not in cols:
            conn.execute("ALTER TABLE team_members ADD COLUMN runtime_error TEXT")
        if "runtime_started_at" not in cols:
            conn.execute("ALTER TABLE team_members ADD COLUMN runtime_started_at TEXT")
        if "runtime_image_digest" not in cols:
            conn.execute("ALTER TABLE team_members ADD COLUMN runtime_image_digest TEXT")
        if "runtime_image_digest_latest" not in cols:
            conn.execute("ALTER TABLE team_members ADD COLUMN runtime_image_digest_latest TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_team_project ON team_members(project_id)")


def init_task_comments_schema() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                author_member_id INTEGER REFERENCES team_members(id) ON DELETE SET NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id)")


def init_agent_runs_schema() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agent_runs (
                id TEXT PRIMARY KEY,                       -- UUID
                member_id INTEGER NOT NULL REFERENCES team_members(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
                inputs TEXT NOT NULL DEFAULT '{}',
                output TEXT,
                error TEXT,
                tokens_in INTEGER,
                tokens_out INTEGER,
                cost_usd REAL,
                triggered_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_member ON agent_runs(member_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status)")


init_auth_schema()
init_projects_schema()
init_team_members_schema()
init_tasks_schema()
init_task_comments_schema()
init_agent_runs_schema()


# ---------- App settings (generic key/value, used for runtime intervals etc.) ----------

def get_app_setting(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_app_setting(key: str, value: str) -> None:
    ts = now_iso()
    with db() as conn:
        conn.execute(
            """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, ts),
        )


UPDATE_CHECK_KEY = "image_update_check_interval_seconds"
DEFAULT_UPDATE_INTERVAL = 300  # 5 minutes


def get_update_check_interval() -> int:
    raw = get_app_setting(UPDATE_CHECK_KEY, str(DEFAULT_UPDATE_INTERVAL))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_UPDATE_INTERVAL


# Will be populated below once the check functions are defined.
_update_loop_task: Optional[asyncio.Task] = None
_health_loop_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(_app):
    """Start the background pollers (image updates + container health
    reconciliation). Both are disabled when STARFORGE_DISABLE_BACKGROUND_TASKS
    is set (the test suite drives the checks directly)."""
    global _update_loop_task, _health_loop_task
    if not os.environ.get("STARFORGE_DISABLE_BACKGROUND_TASKS"):
        _update_loop_task = asyncio.create_task(_image_update_loop())
        _health_loop_task = asyncio.create_task(_health_check_loop())
    yield
    for t in (_update_loop_task, _health_loop_task):
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Starforge", version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------- Helpers ----------

def row_to_task(row: sqlite3.Row, member_row: Optional[sqlite3.Row] = None) -> dict[str, Any]:
    d = dict(row)
    try:
        d["metadata"] = json.loads(d.get("metadata") or "{}")
    except json.JSONDecodeError:
        d["metadata"] = {}
    if member_row and member_row["id"] is not None:
        d["assignee_member"] = {
            "id": member_row["id"],
            "name": member_row["name"],
            "color": member_row["color"],
            "type": member_row["type"],
            "role": member_row["role"],
        }
    else:
        d["assignee_member"] = None
    return d


def _fetch_member_for(assignee_id: Optional[int]) -> Optional[sqlite3.Row]:
    if not assignee_id:
        return None
    with db() as conn:
        return conn.execute(
            "SELECT id, name, color, type, role FROM team_members WHERE id = ?",
            (assignee_id,),
        ).fetchone()


def validate_assignee_for_project(assignee_id: Optional[int], project_id: int) -> None:
    if assignee_id is None:
        return
    with db() as conn:
        row = conn.execute(
            "SELECT project_id FROM team_members WHERE id = ?", (assignee_id,)
        ).fetchone()
    if not row:
        raise HTTPException(400, "assignee_id does not reference a known team member")
    if row["project_id"] != project_id:
        raise HTTPException(
            400, "assignee_id belongs to a member of a different project"
        )


def safe_user(u: dict) -> dict:
    return {
        "id": u["id"],
        "email": u["email"],
        "display_name": u["display_name"],
        "is_admin": bool(u["is_admin"]),
    }


def request_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    return f"{proto}://{host}"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return s or "project"


def unique_slug(base: str, exclude_id: Optional[int] = None) -> str:
    with db() as conn:
        sql = "SELECT 1 FROM projects WHERE slug = ?"
        params: list[Any] = [base]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        if not conn.execute(sql, params).fetchone():
            return base
        i = 2
        while True:
            cand = f"{base}-{i}"[:40]
            params2 = [cand]
            if exclude_id is not None:
                params2.append(exclude_id)
            if not conn.execute(sql, params2).fetchone():
                return cand
            i += 1


def get_project_row(pid: int) -> Optional[dict[str, Any]]:
    with db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def can_modify_project(user: dict, project: dict) -> bool:
    return bool(user.get("is_admin")) or project.get("created_by") == user["id"]


VALID_COLORS = {
    "#6ea8fe", "#b388ff", "#4caf78", "#f0b429",
    "#ff7171", "#29b6f6", "#ec407a", "#8a93a6",
}


def list_agent_types() -> list[dict[str, Any]]:
    """Scan ./agents/ and return one entry per agent directory that has a valid config.yaml."""
    if not AGENTS_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(AGENTS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        cfg = d / "config.yaml"
        if not cfg.exists():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        agent = data.get("agent") or {}
        if not agent.get("name"):
            continue
        out.append({
            "slug": d.name,
            "name": agent.get("name", d.name),
            "description": agent.get("description", ""),
            "model": agent.get("model", ""),
        })
    return out


def get_agent_type_slugs() -> set[str]:
    return {t["slug"] for t in list_agent_types()}


# ---------- Agent snapshot resolver ----------
#
# When a team_member is bound to an agent_type, we resolve agents/<slug>/config.yaml
# (and any source: file references inside it) into a single self-contained snapshot
# that gets persisted into team_members.config.
#
# The snapshot includes a SHA-256 content hash so the UI can detect when the
# source files on disk have drifted from the persisted snapshot and surface a
# "refresh from source" prompt to the operator.
#
# Why snapshot-at-save rather than resolve-at-invoke:
# - Reproducible agent runs (the prompt is pinned, not chasing HEAD).
# - No live filesystem (or, later, network) dependency on the invocation hot path.
# - Changes to source files surface as an explicit operator action, not a silent
#   behavior change under a live agent.


def _content_hash_from_parts(*parts: bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        # length-prefix each part so different chunkings can't collide
        h.update(len(p).to_bytes(8, "big"))
        h.update(p)
    return h.hexdigest()


def _resolve_content_field(
    value: Any, agent_dir: Path
) -> tuple[Any, Optional[str], bytes]:
    """Resolve a content field that may be inline / source:file / source:git.

    Returns (resolved_content, source_relative_path_or_None, raw_bytes_for_hashing).
    """
    if value is None:
        return None, None, b""
    if isinstance(value, str):
        return value, None, value.encode("utf-8")
    if isinstance(value, dict):
        source = value.get("source")
        if source == "file":
            rel = value.get("path", "")
            full = agent_dir / rel
            if not full.exists():
                raise HTTPException(400, f"referenced file missing: {full}")
            raw = full.read_bytes()
            if full.suffix in (".yaml", ".yml"):
                try:
                    content: Any = yaml.safe_load(raw.decode("utf-8")) or {}
                except yaml.YAMLError as e:
                    raise HTTPException(400, f"invalid YAML in {full}: {e}")
            else:
                content = raw.decode("utf-8")
            return content, rel, raw
        if source == "git":
            raise HTTPException(
                501,
                "git-sourced agent content is on the roadmap but not yet implemented; "
                "use source: file with content checked into ./agents/<slug>/",
            )
    # Pass-through for unrecognized shapes (e.g., inline structured guardrails)
    return value, None, json.dumps(value, sort_keys=True).encode("utf-8")


def resolve_agent_snapshot(slug: str) -> dict[str, Any]:
    """Read agents/<slug>/ and produce a self-contained snapshot dict."""
    agent_dir = AGENTS_DIR / slug
    if not agent_dir.is_dir():
        raise HTTPException(404, f"agent type '{slug}' not found in ./agents/")
    cfg_path = agent_dir / "config.yaml"
    if not cfg_path.exists():
        raise HTTPException(400, f"agents/{slug}/config.yaml missing")

    cfg_bytes = cfg_path.read_bytes()
    try:
        cfg_data: dict[str, Any] = yaml.safe_load(cfg_bytes.decode("utf-8")) or {}
    except yaml.YAMLError as e:
        raise HTTPException(400, f"invalid YAML in {cfg_path}: {e}")

    agent_section = cfg_data.get("agent", {}) or {}

    prompt, prompt_src, prompt_raw = _resolve_content_field(
        agent_section.get("system_prompt"), agent_dir
    )
    guardrails, guard_src, guard_raw = _resolve_content_field(
        agent_section.get("guardrails"), agent_dir
    )

    return {
        "snapshot_at": now_iso(),
        "agent_type": slug,
        "content_hash": _content_hash_from_parts(cfg_bytes, prompt_raw, guard_raw),
        "sources": {
            "config": f"agents/{slug}/config.yaml",
            "system_prompt": f"agents/{slug}/{prompt_src}" if prompt_src else None,
            "guardrails": f"agents/{slug}/{guard_src}" if guard_src else None,
        },
        "config": cfg_data,
        "system_prompt": prompt,
        "guardrails": guardrails,
    }


def current_snapshot_hash(slug: str) -> Optional[str]:
    """Compute today's content hash for `slug` without persisting. None if missing."""
    try:
        return resolve_agent_snapshot(slug)["content_hash"]
    except HTTPException:
        return None


# ---------- Runtime adapter wiring ----------
#
# Tests inject a FakeRuntime via _runtime_override. When None, the adapter is
# chosen at call time based on the project's runtime_config.type.

_runtime_override: Optional[RuntimeAdapter] = None


def get_runtime_for_project(rt_config: dict[str, Any]) -> Optional[RuntimeAdapter]:
    """Return an adapter for this project's runtime config, or None if not configured."""
    if _runtime_override is not None:
        return _runtime_override
    rt_type = rt_config.get("type")
    if not rt_type or not rt_config.get("image"):
        return None
    if rt_type == "docker":
        from runtime_docker import DockerRuntime
        return DockerRuntime(rt_config)
    raise HTTPException(501, f"runtime type '{rt_type}' not implemented")


def _set_member_runtime_state(member_id: int, **fields) -> None:
    """Update one or more runtime_* columns on a team member."""
    if not fields:
        return
    cols = [f"{k} = ?" for k in fields]
    cols.append("updated_at = ?")
    params = list(fields.values()) + [now_iso(), member_id]
    with db() as conn:
        conn.execute(
            f"UPDATE team_members SET {', '.join(cols)} WHERE id = ?", params
        )


async def _provision_member(member_id: int, project_id: int) -> None:
    """Background task: pull image, start container, update DB with the result."""
    try:
        with db() as conn:
            project_row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            member_row = conn.execute("SELECT * FROM team_members WHERE id = ?", (member_id,)).fetchone()
        if not project_row or not member_row:
            return
        project = dict(project_row)
        member = _row_to_member(member_row)
        try:
            rt_config = json.loads(project.get("runtime_config") or "{}")
        except json.JSONDecodeError:
            rt_config = {}
        rt = get_runtime_for_project(rt_config)
        if rt is None:
            # Nothing to do — runtime not configured. Leave status as-is.
            _set_member_runtime_state(member_id, runtime_status="not_provisioned",
                                       runtime_error=None)
            return
        snapshot = (member.get("config") or {}).get("agent_snapshot")
        if not snapshot:
            _set_member_runtime_state(
                member_id,
                runtime_status="error",
                runtime_error="no agent snapshot — bind member to an agent_type first",
            )
            return
        # Decrypt project secrets and surface to the adapter so it can pass
        # them to the container as env vars (Anthropic key, callback token).
        proj_secrets = get_project_secrets(project["id"])
        # Auto-create the callback token if it's missing — Phase C needs it for
        # nemoclaw → Starforge result callbacks.
        if not proj_secrets.get("callback_token"):
            ensure_project_callback_token(project["id"])
            proj_secrets = get_project_secrets(project["id"])
        result = await rt.provision(
            member_id=member_id,
            project_slug=project["slug"],
            snapshot=snapshot,
            config=rt_config,
            secrets=proj_secrets,
        )
        _set_member_runtime_state(
            member_id,
            runtime_status="running",
            runtime_container_id=result.container_id,
            runtime_endpoint=result.endpoint,
            runtime_image_digest=result.image_digest,
            runtime_started_at=now_iso(),
            runtime_error=None,
        )
    except Exception as e:  # noqa: BLE001 — last-resort surface to DB
        _set_member_runtime_state(member_id, runtime_status="error", runtime_error=str(e))


async def _trigger_provision(member_id: int, project_id: int) -> None:
    """Set status=starting and run provision. Sync when FakeRuntime overrides;
    fire-and-forget on real Docker (which can take many seconds to pull)."""
    _set_member_runtime_state(member_id, runtime_status="starting", runtime_error=None)
    if _runtime_override is not None:
        # Tests get deterministic synchronous behavior.
        await _provision_member(member_id, project_id)
    else:
        asyncio.create_task(_provision_member(member_id, project_id))


async def _teardown_member_runtime(member: dict[str, Any], remove: bool = True) -> None:
    """Stop (and optionally remove) the member's container. Safe if not provisioned."""
    cid = member.get("runtime_container_id")
    if not cid:
        return
    project = get_project_row(member["project_id"])
    if not project:
        return
    try:
        rt_config = json.loads(project.get("runtime_config") or "{}")
    except json.JSONDecodeError:
        rt_config = {}
    try:
        rt = get_runtime_for_project(rt_config)
    except HTTPException:
        rt = None
    if rt is None:
        return
    try:
        if remove:
            await rt.remove(cid)
        else:
            await rt.stop(cid)
    except Exception as e:  # noqa: BLE001
        # Don't block the user-facing operation on cleanup errors.
        _set_member_runtime_state(member["id"], runtime_error=f"teardown: {e}")


# ---------- Page routes (gating) ----------

@app.get("/")
async def index(request: Request):
    if user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    token = request.cookies.get(auth.SESSION_COOKIE)
    if not auth.get_user_by_session(token):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/setup")
async def setup_page():
    if user_count() > 0:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "setup.html")


@app.get("/login")
async def login_page(request: Request):
    if user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    token = request.cookies.get(auth.SESSION_COOKIE)
    if auth.get_user_by_session(token):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/settings")
async def settings_page(_: dict = Depends(current_admin)):
    return FileResponse(STATIC_DIR / "settings.html")


@app.get("/projects")
async def projects_page(request: Request):
    if user_count() == 0:
        return RedirectResponse("/setup", status_code=303)
    token = request.cookies.get(auth.SESSION_COOKIE)
    if not auth.get_user_by_session(token):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC_DIR / "projects.html")


# ---------- Setup / login API ----------

class SetupBody(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=256)


@app.post("/api/setup")
async def api_setup(body: SetupBody, request: Request, response: Response):
    if user_count() > 0:
        raise HTTPException(409, "setup already complete")
    uid = create_user(str(body.email), body.display_name, body.password, is_admin=True)
    ua, ip = attach_request_meta(request)
    token = create_session(uid, ua, ip)
    touch_login(uid)
    set_session_cookie(response, token)
    return {"ok": True, "user": safe_user(auth.get_user_by_id(uid))}


class LoginBody(BaseModel):
    email: EmailStr
    password: str


@app.post("/api/login")
async def api_login(body: LoginBody, request: Request, response: Response):
    user = get_user_by_email(str(body.email))
    if not user or not user.get("password_hash") or not user["is_active"]:
        raise HTTPException(401, "invalid credentials")
    if not verify_password(user["password_hash"], body.password):
        raise HTTPException(401, "invalid credentials")
    if needs_rehash(user["password_hash"]):
        new_hash = hash_password(body.password)
        with db() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))
    ua, ip = attach_request_meta(request)
    token = create_session(user["id"], ua, ip)
    touch_login(user["id"])
    set_session_cookie(response, token)
    return {"ok": True, "user": safe_user(user)}


@app.post("/api/logout")
async def api_logout(request: Request, response: Response):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        revoke_session(token)
    clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/me")
async def api_me(user: dict = Depends(current_user)):
    return safe_user(user)


@app.get("/api/auth/providers")
async def api_providers():
    return [{"slug": p["slug"], "display_name": p["display_name"]} for p in oidc.list_enabled_providers()]


# ---------- OIDC ----------

@app.get("/auth/{slug}/start")
async def oidc_start(slug: str, request: Request, return_to: str = "/"):
    provider = oidc.get_provider_by_slug(slug)
    if not provider or not provider["is_enabled"]:
        raise HTTPException(404, "unknown provider")
    if not return_to.startswith("/"):
        return_to = "/"
    url = await oidc.begin_login(provider, request_base(request), return_to)
    return RedirectResponse(url, status_code=303)


@app.get("/auth/{slug}/callback")
async def oidc_callback(
    slug: str,
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    if error:
        raise HTTPException(400, f"OIDC error: {error}: {error_description or ''}")
    if not code or not state:
        raise HTTPException(400, "missing code or state")
    provider, claims, return_to = await oidc.complete_login(slug, code, state, request_base(request))
    user_id = oidc.find_or_create_user_for_claims(provider, claims)
    ua, ip = attach_request_meta(request)
    token = create_session(user_id, ua, ip)
    touch_login(user_id)
    resp = RedirectResponse(return_to, status_code=303)
    set_session_cookie(resp, token)
    return resp


# ---------- Admin: SSO providers ----------

class ProviderCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    display_name: str = Field(min_length=1, max_length=100)
    issuer: str = Field(min_length=8, max_length=500)
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: str = Field(min_length=1, max_length=500)
    scopes: str = "openid email profile"
    is_enabled: bool = True


class ProviderUpdate(BaseModel):
    display_name: Optional[str] = None
    issuer: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    scopes: Optional[str] = None
    is_enabled: Optional[bool] = None


@app.get("/api/admin/sso")
async def admin_list_sso(_: dict = Depends(current_admin)):
    return oidc.list_all_providers()


@app.post("/api/admin/sso", status_code=201)
async def admin_create_sso(body: ProviderCreate, _: dict = Depends(current_admin)):
    if oidc.get_provider_by_slug(body.slug):
        raise HTTPException(409, "slug already exists")
    pid = oidc.create_provider(
        slug=body.slug,
        display_name=body.display_name,
        issuer=body.issuer,
        client_id=body.client_id,
        client_secret=body.client_secret,
        scopes=body.scopes,
        is_enabled=body.is_enabled,
    )
    p = oidc.get_provider_by_id(pid)
    p.pop("client_secret_enc", None)
    return p


@app.patch("/api/admin/sso/{pid}")
async def admin_update_sso(pid: int, body: ProviderUpdate, _: dict = Depends(current_admin)):
    if not oidc.get_provider_by_id(pid):
        raise HTTPException(404, "not found")
    oidc.update_provider(pid, **body.model_dump(exclude_unset=True))
    p = oidc.get_provider_by_id(pid)
    p.pop("client_secret_enc", None)
    return p


@app.delete("/api/admin/sso/{pid}", status_code=204)
async def admin_delete_sso(pid: int, _: dict = Depends(current_admin)):
    oidc.delete_provider(pid)


@app.get("/api/admin/sso/{pid}/redirect_uri")
async def admin_redirect_uri(pid: int, request: Request, _: dict = Depends(current_admin)):
    p = oidc.get_provider_by_id(pid)
    if not p:
        raise HTTPException(404, "not found")
    return {"redirect_uri": f"{request_base(request)}/auth/{p['slug']}/callback"}


# ---------- Admin: sessions ----------

@app.get("/api/admin/sessions")
async def admin_list_sessions(_: dict = Depends(current_admin)):
    with db() as conn:
        rows = conn.execute(
            """SELECT s.id, s.user_id, u.email, s.created_at, s.expires_at, s.last_seen_at,
                      s.user_agent, s.ip
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.expires_at > ?
               ORDER BY s.last_seen_at DESC""",
            (now_iso(),),
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/admin/sessions/{sid}", status_code=204)
async def admin_revoke_session(sid: int, _: dict = Depends(current_admin)):
    revoke_session_id(sid)


# ---------- Admin: image update check interval ----------

class UpdateCheckInterval(BaseModel):
    seconds: int = Field(ge=0, le=86400)


@app.get("/api/admin/settings/update-check-interval")
async def admin_get_update_interval(_: dict = Depends(current_admin)):
    return {"seconds": get_update_check_interval()}


@app.put("/api/admin/settings/update-check-interval")
async def admin_set_update_interval(
    body: UpdateCheckInterval, _: dict = Depends(current_admin)
):
    set_app_setting(UPDATE_CHECK_KEY, str(body.seconds))
    return {"seconds": body.seconds}


@app.post("/api/admin/check-image-updates")
async def admin_trigger_image_check(_: dict = Depends(current_admin)):
    """Run an image-update check immediately across every running AI member."""
    n = await check_all_image_updates()
    return {"ok": True, "checked": n}


@app.post("/api/admin/check-runtime-health")
async def admin_trigger_health_check(_: dict = Depends(current_admin)):
    """Force-reconcile container status across every supposedly-running AI member.
    Use after manually killing containers to flush stale state."""
    n = await check_all_member_health()
    return {"ok": True, "checked": n}


@app.post("/api/team-members/{mid}/check-image-update")
async def trigger_member_image_check(mid: int, _: dict = Depends(current_user)):
    """Run an image-update check for one member on demand."""
    await check_image_update_for_member(mid)
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise HTTPException(404, "team member not found")
    return _row_to_member(row)


# ---------- Projects ----------

class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: Optional[str] = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    description: str = ""
    color: str = "#6ea8fe"
    default_assignee: str = ""
    auto_archive_done_days: int = Field(default=0, ge=0, le=3650)


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    slug: Optional[str] = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    description: Optional[str] = None
    color: Optional[str] = None
    default_assignee: Optional[str] = None
    auto_archive_done_days: Optional[int] = Field(default=None, ge=0, le=3650)
    is_archived: Optional[bool] = None


def _project_with_count(conn, row) -> dict[str, Any]:
    d = dict(row)
    d["task_count"] = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE project_id = ?", (d["id"],)
    ).fetchone()["c"]
    try:
        d["runtime_config"] = json.loads(d.get("runtime_config") or "{}")
    except json.JSONDecodeError:
        d["runtime_config"] = {}
    # Strip raw encrypted bytes — they're not JSON-encodable and shouldn't be
    # exposed over the API anyway. Status is read via /runtime-secrets/status.
    d.pop("runtime_secrets_enc", None)
    return d


@app.get("/api/projects")
async def list_projects(
    include_archived: bool = False,
    _: dict = Depends(current_user),
):
    sql = "SELECT * FROM projects"
    if not include_archived:
        sql += " WHERE is_archived = 0"
    sql += " ORDER BY name COLLATE NOCASE"
    with db() as conn:
        rows = conn.execute(sql).fetchall()
        return [_project_with_count(conn, r) for r in rows]


@app.post("/api/projects", status_code=201)
async def create_project(body: ProjectCreate, user: dict = Depends(current_user)):
    if body.color not in VALID_COLORS:
        raise HTTPException(400, "invalid color")
    base = body.slug or slugify(body.name)
    slug = unique_slug(base)
    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO projects (slug, name, description, color, default_assignee,
                                     auto_archive_done_days, is_archived, created_by,
                                     created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (slug, body.name, body.description, body.color, body.default_assignee,
             body.auto_archive_done_days, user["id"], ts, ts),
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _project_with_count(conn, row)


@app.get("/api/projects/{pid}")
async def get_project(pid: int, _: dict = Depends(current_user)):
    project = get_project_row(pid)
    if not project:
        raise HTTPException(404, "project not found")
    with db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return _project_with_count(conn, row)


@app.patch("/api/projects/{pid}")
async def update_project(pid: int, body: ProjectUpdate, user: dict = Depends(current_user)):
    project = get_project_row(pid)
    if not project:
        raise HTTPException(404, "project not found")
    if not can_modify_project(user, project):
        raise HTTPException(403, "only an admin or the project creator can edit")

    data = body.model_dump(exclude_unset=True)
    if "color" in data and data["color"] not in VALID_COLORS:
        raise HTTPException(400, "invalid color")
    if "slug" in data and data["slug"] != project["slug"]:
        with db() as conn:
            clash = conn.execute(
                "SELECT 1 FROM projects WHERE slug = ? AND id != ?", (data["slug"], pid)
            ).fetchone()
        if clash:
            raise HTTPException(409, "slug already in use")
    if "is_archived" in data:
        if project["slug"] == "default" and data["is_archived"]:
            raise HTTPException(400, "cannot archive the default project")
        data["is_archived"] = 1 if data["is_archived"] else 0

    fields, params = [], []
    for k, v in data.items():
        fields.append(f"{k} = ?")
        params.append(v)
    if not fields:
        raise HTTPException(400, "no fields to update")
    fields.append("updated_at = ?")
    params.append(now_iso())
    params.append(pid)
    with db() as conn:
        conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return _project_with_count(conn, row)


VALID_RUNTIME_TYPES = {"docker", "k8s"}
VALID_PULL_POLICIES = {"if_not_present", "always", "never"}


# ---------- Per-project runtime secrets (AES-256-GCM at rest) ----------

def get_project_secrets(project_id: int) -> dict[str, Any]:
    """Decrypted secrets dict for a project. Returns {} if unset/unreadable."""
    with db() as conn:
        row = conn.execute(
            "SELECT runtime_secrets_enc FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    blob = row["runtime_secrets_enc"] if row else None
    if not blob:
        return {}
    try:
        return json.loads(aes_decrypt(bytes(blob)))
    except Exception:  # noqa: BLE001 — corrupt / wrong-key blob: treat as empty
        return {}


def set_project_secrets(project_id: int, **updates: Optional[str]) -> None:
    """Merge updates into the project's secret dict and persist encrypted.
    A None value clears that key."""
    secrets = get_project_secrets(project_id)
    for k, v in updates.items():
        if v is None:
            secrets.pop(k, None)
        else:
            secrets[k] = v
    blob = aes_encrypt(json.dumps(secrets)) if secrets else None
    with db() as conn:
        conn.execute(
            "UPDATE projects SET runtime_secrets_enc = ?, updated_at = ? WHERE id = ?",
            (blob, now_iso(), project_id),
        )


def ensure_project_callback_token(project_id: int) -> str:
    """Generate-on-demand callback token used to auth nemoclaw → Starforge results.

    The token is created once on first need and stored encrypted; subsequent
    calls return the existing value. Regeneration is an explicit admin action.
    """
    secrets = get_project_secrets(project_id)
    if secrets.get("callback_token"):
        return secrets["callback_token"]
    token = secrets_lib.token_urlsafe(32)
    set_project_secrets(project_id, callback_token=token)
    return token


class ProjectRuntimeConfig(BaseModel):
    """Project-level configuration for the agent runtime (Phase B/C).

    Sensitive fields (Anthropic API key, callback token) live in
    runtime_secrets_enc (AES-encrypted), not here.
    """
    type: Optional[str] = None  # "docker" | "k8s" | None (= not configured)
    docker_host: str = Field(default="", max_length=500)
    image: str = Field(default="", max_length=500)
    image_pull_policy: str = "if_not_present"
    network: str = Field(default="", max_length=200)
    cpu_limit: str = Field(default="1", max_length=50)
    memory_limit: str = Field(default="2Gi", max_length=50)
    extra_env: dict[str, str] = Field(default_factory=dict)
    # URL that the nemoclaw container will POST results back to. Must be
    # reachable from inside the container — e.g. http://host.docker.internal:8000
    # for Docker Desktop, http://starforge:8000 for compose, etc.
    starforge_callback_url: str = Field(default="", max_length=500)


class ProjectRuntimeSecrets(BaseModel):
    """Write-only payload for project runtime secrets.

    Pass None to leave a field unchanged. Omit a field to leave it unchanged.
    GET responses never echo these back.
    """
    anthropic_api_key: Optional[str] = Field(default=None, max_length=500)
    callback_token: Optional[str] = Field(default=None, max_length=200)


class ProjectRuntimeSecretsStatus(BaseModel):
    anthropic_api_key_set: bool
    callback_token_set: bool


@app.get("/api/projects/{pid}/runtime-config")
async def get_project_runtime_config(pid: int, _: dict = Depends(current_user)):
    project = get_project_row(pid)
    if not project:
        raise HTTPException(404, "project not found")
    try:
        cfg = json.loads(project.get("runtime_config") or "{}")
    except json.JSONDecodeError:
        cfg = {}
    return cfg


@app.put("/api/projects/{pid}/runtime-config")
async def set_project_runtime_config(
    pid: int,
    body: ProjectRuntimeConfig,
    user: dict = Depends(current_user),
):
    project = get_project_row(pid)
    if not project:
        raise HTTPException(404, "project not found")
    if not can_modify_project(user, project):
        raise HTTPException(403, "only admin or project creator can edit runtime config")
    if body.type and body.type not in VALID_RUNTIME_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(VALID_RUNTIME_TYPES)} or null")
    if body.image_pull_policy not in VALID_PULL_POLICIES:
        raise HTTPException(400, f"image_pull_policy must be one of {sorted(VALID_PULL_POLICIES)}")
    if body.type == "k8s":
        # Schema-supported but adapter not built yet (B.2 is docker-only first)
        raise HTTPException(501, "k8s runtime adapter not yet implemented — use docker for now")
    cfg_json = json.dumps(body.model_dump())
    with db() as conn:
        conn.execute(
            "UPDATE projects SET runtime_config = ?, updated_at = ? WHERE id = ?",
            (cfg_json, now_iso(), pid),
        )
    return body.model_dump()


@app.get("/api/projects/{pid}/runtime-secrets/status")
async def get_project_runtime_secrets_status(pid: int, _: dict = Depends(current_user)):
    if not get_project_row(pid):
        raise HTTPException(404, "project not found")
    s = get_project_secrets(pid)
    return {
        "anthropic_api_key_set": bool(s.get("anthropic_api_key")),
        "callback_token_set": bool(s.get("callback_token")),
    }


@app.put("/api/projects/{pid}/runtime-secrets")
async def put_project_runtime_secrets(
    pid: int, body: ProjectRuntimeSecrets, user: dict = Depends(current_user)
):
    project = get_project_row(pid)
    if not project:
        raise HTTPException(404, "project not found")
    if not can_modify_project(user, project):
        raise HTTPException(403, "only admin or project creator can set secrets")
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, "no fields to update")
    set_project_secrets(pid, **data)
    return await get_project_runtime_secrets_status(pid, user)


@app.post("/api/projects/{pid}/runtime-secrets/regenerate-callback-token")
async def regenerate_callback_token(pid: int, user: dict = Depends(current_user)):
    project = get_project_row(pid)
    if not project:
        raise HTTPException(404, "project not found")
    if not can_modify_project(user, project):
        raise HTTPException(403, "only admin or project creator can regenerate the token")
    new_token = secrets_lib.token_urlsafe(32)
    set_project_secrets(pid, callback_token=new_token)
    return {"ok": True, "callback_token_set": True}


@app.delete("/api/projects/{pid}", status_code=204)
async def delete_project(pid: int, user: dict = Depends(current_user)):
    project = get_project_row(pid)
    if not project:
        raise HTTPException(404, "project not found")
    if not can_modify_project(user, project):
        raise HTTPException(403, "only an admin or the project creator can delete")
    if project["slug"] == "default":
        raise HTTPException(400, "cannot delete the default project")
    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE project_id = ?", (pid,)
        ).fetchone()["c"]
        if count > 0:
            raise HTTPException(
                400,
                f"project still has {count} task(s); archive instead, or delete tasks first",
            )
        conn.execute("DELETE FROM projects WHERE id = ?", (pid,))


# ---------- Team members ----------

VALID_MEMBER_TYPES = {"human", "ai_agent"}


class TeamMemberCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str = "human"
    email: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=100)
    description: str = ""
    color: str = "#6ea8fe"
    agent_type: Optional[str] = None


class TeamMemberUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    type: Optional[str] = None
    email: Optional[str] = Field(default=None, max_length=200)
    role: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = None
    color: Optional[str] = None
    is_active: Optional[bool] = None
    agent_type: Optional[str] = None


def _row_to_member(row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["config"] = json.loads(d.get("config") or "{}")
    except json.JSONDecodeError:
        d["config"] = {}
    # Computed: a newer image is at the registry than the one we're running.
    running = d.get("runtime_image_digest")
    latest = d.get("runtime_image_digest_latest")
    d["update_available"] = bool(running and latest and running != latest)
    return d


@app.get("/api/projects/{pid}/members")
async def list_members(pid: int, _: dict = Depends(current_user)):
    if not get_project_row(pid):
        raise HTTPException(404, "project not found")
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM team_members WHERE project_id = ? ORDER BY name COLLATE NOCASE",
            (pid,),
        ).fetchall()
    return [_row_to_member(r) for r in rows]


@app.post("/api/projects/{pid}/members", status_code=201)
async def create_member(pid: int, body: TeamMemberCreate, user: dict = Depends(current_user)):
    project = get_project_row(pid)
    if not project:
        raise HTTPException(404, "project not found")
    if project["is_archived"]:
        raise HTTPException(400, "cannot add members to an archived project")
    if body.type not in VALID_MEMBER_TYPES:
        raise HTTPException(400, f"invalid type; use one of {sorted(VALID_MEMBER_TYPES)}")
    if body.color not in VALID_COLORS:
        raise HTTPException(400, "invalid color")
    if body.agent_type:
        if body.type != "ai_agent":
            raise HTTPException(400, "agent_type is only valid for ai_agent members")
        if body.agent_type not in get_agent_type_slugs():
            raise HTTPException(400, f"unknown agent_type: {body.agent_type}")

    config_blob: dict[str, Any] = {}
    if body.agent_type:
        config_blob["agent_snapshot"] = resolve_agent_snapshot(body.agent_type)

    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO team_members
               (project_id, name, type, email, role, description, color, is_active,
                config, agent_type, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (pid, body.name, body.type, body.email, body.role, body.description,
             body.color, json.dumps(config_blob), body.agent_type, user["id"], ts, ts),
        )
        new_id = cur.lastrowid

    # Auto-provision the runtime container if this is an AI agent member
    # bound to an agent_type AND the project has a runtime configured.
    if body.type == "ai_agent" and body.agent_type:
        try:
            rt_cfg = json.loads(project.get("runtime_config") or "{}")
        except json.JSONDecodeError:
            rt_cfg = {}
        if rt_cfg.get("type") and rt_cfg.get("image"):
            await _trigger_provision(new_id, pid)

    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (new_id,)).fetchone()
    return _row_to_member(row)


@app.patch("/api/team-members/{mid}")
async def update_member(mid: int, body: TeamMemberUpdate, _: dict = Depends(current_user)):
    with db() as conn:
        existing = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    if not existing:
        raise HTTPException(404, "team member not found")
    data = body.model_dump(exclude_unset=True)
    if "type" in data and data["type"] != existing["type"]:
        # Type is immutable after creation — changing human <→ ai_agent would
        # invalidate any running runtime container and break run history semantics.
        raise HTTPException(400, "team member type cannot be changed after creation")
    if "color" in data and data["color"] not in VALID_COLORS:
        raise HTTPException(400, "invalid color")
    if "is_active" in data:
        data["is_active"] = 1 if data["is_active"] else 0
    if "agent_type" in data and data["agent_type"]:
        final_type = data.get("type", existing["type"])
        if final_type != "ai_agent":
            raise HTTPException(400, "agent_type is only valid for ai_agent members")
        if data["agent_type"] not in get_agent_type_slugs():
            raise HTTPException(400, f"unknown agent_type: {data['agent_type']}")

    # If agent_type is being added or changed, refresh the snapshot.
    # If cleared (set to None or empty), drop the snapshot.
    if "agent_type" in data:
        existing_config_raw = existing["config"] or "{}"
        try:
            existing_config = json.loads(existing_config_raw)
        except json.JSONDecodeError:
            existing_config = {}
        new_at = data["agent_type"]
        if new_at:
            existing_config["agent_snapshot"] = resolve_agent_snapshot(new_at)
        else:
            existing_config.pop("agent_snapshot", None)
        data["config"] = json.dumps(existing_config)
    fields, params = [], []
    for k, v in data.items():
        fields.append(f"{k} = ?")
        params.append(v)
    if not fields:
        raise HTTPException(400, "no fields to update")
    fields.append("updated_at = ?")
    params.append(now_iso())
    params.append(mid)
    with db() as conn:
        conn.execute(f"UPDATE team_members SET {', '.join(fields)} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    return _row_to_member(row)


@app.get("/api/agent-types")
async def api_agent_types(_: dict = Depends(current_user)):
    """List AI agent types defined under ./agents/ in the repo."""
    return list_agent_types()


@app.get("/api/team-members/{mid}/agent-snapshot")
async def get_member_agent_snapshot(mid: int, _: dict = Depends(current_user)):
    """Return the persisted agent snapshot plus a freshness check against the source files."""
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise HTTPException(404, "team member not found")
    member = _row_to_member(row)
    snapshot = (member.get("config") or {}).get("agent_snapshot")
    if not snapshot:
        return {
            "snapshot": None,
            "current_hash": None,
            "is_stale": False,
            "source_missing": False,
        }
    current = current_snapshot_hash(snapshot["agent_type"])
    return {
        "snapshot": snapshot,
        "current_hash": current,
        "is_stale": current is not None and current != snapshot["content_hash"],
        "source_missing": current is None,
    }


@app.post("/api/team-members/{mid}/refresh-snapshot")
async def refresh_member_agent_snapshot(mid: int, _: dict = Depends(current_user)):
    """Re-resolve agents/<slug>/ from disk and persist a new snapshot for this member."""
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise HTTPException(404, "team member not found")
    member = _row_to_member(row)
    slug = member.get("agent_type")
    if not slug:
        raise HTTPException(400, "this member is not bound to an agent_type")
    config = dict(member.get("config") or {})
    config["agent_snapshot"] = resolve_agent_snapshot(slug)
    with db() as conn:
        conn.execute(
            "UPDATE team_members SET config = ?, updated_at = ? WHERE id = ?",
            (json.dumps(config), now_iso(), mid),
        )
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    return _row_to_member(row)


@app.delete("/api/team-members/{mid}", status_code=204)
async def delete_member(mid: int, _: dict = Depends(current_user)):
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise HTTPException(404, "team member not found")
    member = _row_to_member(row)
    # Tear down the container before deleting the DB row so we don't orphan it.
    if member.get("runtime_container_id"):
        await _teardown_member_runtime(member, remove=True)
    with db() as conn:
        conn.execute("DELETE FROM team_members WHERE id = ?", (mid,))


# ---------- Runtime control endpoints ----------

@app.post("/api/team-members/{mid}/runtime/start")
async def member_runtime_start(mid: int, _: dict = Depends(current_user)):
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise HTTPException(404, "team member not found")
    member = _row_to_member(row)
    if member["type"] != "ai_agent":
        raise HTTPException(400, "only ai_agent members have runtime containers")
    if not member.get("agent_type"):
        raise HTTPException(400, "bind the member to an agent_type first")

    cid = member.get("runtime_container_id")
    if cid:
        # Container exists — just start it
        project = get_project_row(member["project_id"]) or {}
        try:
            rt_cfg = json.loads(project.get("runtime_config") or "{}")
        except json.JSONDecodeError:
            rt_cfg = {}
        rt = get_runtime_for_project(rt_cfg)
        if rt is None:
            raise HTTPException(400, "no runtime configured for this project")
        await rt.start(cid)
        _set_member_runtime_state(mid, runtime_status="running", runtime_error=None)
    else:
        # Fresh provision
        await _trigger_provision(mid, member["project_id"])
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    return _row_to_member(row)


@app.post("/api/team-members/{mid}/runtime/stop")
async def member_runtime_stop(mid: int, _: dict = Depends(current_user)):
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise HTTPException(404, "team member not found")
    member = _row_to_member(row)
    if not member.get("runtime_container_id"):
        raise HTTPException(400, "no runtime container to stop")
    await _teardown_member_runtime(member, remove=False)
    _set_member_runtime_state(mid, runtime_status="stopped")
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    return _row_to_member(row)


@app.post("/api/team-members/{mid}/runtime/restart")
async def member_runtime_restart(
    mid: int,
    pull: bool = False,
    _: dict = Depends(current_user),
):
    """Remove the existing container and re-provision. With pull=true, the
    image is pulled fresh — this is the "Update" path."""
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise HTTPException(404, "team member not found")
    member = _row_to_member(row)
    if member["type"] != "ai_agent":
        raise HTTPException(400, "only ai_agent members have runtime containers")
    # Tear down existing
    if member.get("runtime_container_id"):
        await _teardown_member_runtime(member, remove=True)
        _set_member_runtime_state(
            mid,
            runtime_container_id=None,
            runtime_endpoint=None,
            runtime_image_digest=None,
            runtime_started_at=None,
        )
    # If pull requested, temporarily force pull_policy=always for this provision
    if pull:
        project = get_project_row(member["project_id"]) or {}
        try:
            rt_cfg = json.loads(project.get("runtime_config") or "{}")
        except json.JSONDecodeError:
            rt_cfg = {}
        # We honor pull_policy=always semantics by ensuring adapter pulls fresh.
        # The Docker adapter pulls if policy is always; FakeRuntime always pulls.
        # No DB write — this only affects this invocation.
        rt = get_runtime_for_project({**rt_cfg, "image_pull_policy": "always"})
        if rt is None:
            raise HTTPException(400, "no runtime configured for this project")
        # Direct provision so the override pull_policy is honored
        await _set_member_runtime_state_async_starting(mid)
        try:
            snapshot = (member.get("config") or {}).get("agent_snapshot")
            if not snapshot:
                _set_member_runtime_state(mid, runtime_status="error",
                                           runtime_error="missing agent_snapshot")
            else:
                proj = get_project_row(member["project_id"]) or {}
                proj_secrets = get_project_secrets(proj.get("id", 0))
                result = await rt.provision(
                    member_id=mid, project_slug=proj.get("slug", ""),
                    snapshot=snapshot, config={**rt_cfg, "image_pull_policy": "always"},
                    secrets=proj_secrets,
                )
                _set_member_runtime_state(
                    mid,
                    runtime_status="running",
                    runtime_container_id=result.container_id,
                    runtime_endpoint=result.endpoint,
                    runtime_image_digest=result.image_digest,
                    runtime_started_at=now_iso(),
                    runtime_error=None,
                )
        except Exception as e:  # noqa: BLE001
            _set_member_runtime_state(mid, runtime_status="error", runtime_error=str(e))
    else:
        await _trigger_provision(mid, member["project_id"])
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    return _row_to_member(row)


async def _set_member_runtime_state_async_starting(mid: int) -> None:
    _set_member_runtime_state(mid, runtime_status="starting", runtime_error=None)


# ---------- Image update check ----------

async def check_image_update_for_member(member_id: int) -> Optional[str]:
    """Ask the registry for the current digest of this member's image and
    persist it as runtime_image_digest_latest. Returns the digest (or None)."""
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (member_id,)).fetchone()
    if not row:
        return None
    member = _row_to_member(row)
    if member["type"] != "ai_agent" or not member.get("runtime_container_id"):
        return None
    project = get_project_row(member["project_id"])
    if not project:
        return None
    try:
        rt_cfg = json.loads(project.get("runtime_config") or "{}")
    except json.JSONDecodeError:
        return None
    try:
        rt = get_runtime_for_project(rt_cfg)
    except HTTPException:
        return None
    if rt is None:
        return None
    image = rt_cfg.get("image")
    if not image:
        return None
    digest = await rt.get_registry_digest(image)
    if digest is None:
        return None
    _set_member_runtime_state(member_id, runtime_image_digest_latest=digest)
    return digest


async def check_all_image_updates() -> int:
    """Run an update check across every running AI member. Returns how many were checked."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM team_members "
            "WHERE type = 'ai_agent' AND runtime_container_id IS NOT NULL"
        ).fetchall()
    n = 0
    for r in rows:
        try:
            await check_image_update_for_member(r["id"])
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("image update check failed for member %s: %s", r["id"], e)
    return n


# ---------- Agent runs (Phase C.1: dispatch + callback) ----------

# Tests inject a stand-in for the outbound HTTP call to nemoclaw so we don't
# need a live container. The override is a callable matching the same
# signature as _http_post_invoke.
_invoke_http_override = None


async def _http_post_invoke(
    endpoint: str, payload: dict[str, Any], token: str
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{endpoint}/invoke", json=payload, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"nemoclaw rejected invoke: {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except Exception:
            return {}


async def _dispatch_invoke(
    endpoint: str, payload: dict[str, Any], token: str
) -> dict[str, Any]:
    if _invoke_http_override is not None:
        return await _invoke_http_override(endpoint, payload, token)
    return await _http_post_invoke(endpoint, payload, token)


def _row_to_run(row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["inputs"] = json.loads(d.get("inputs") or "{}")
    except json.JSONDecodeError:
        d["inputs"] = {}
    return d


class RunCreate(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    """Callback payload from nemoclaw when a run finishes."""
    status: str  # "succeeded" | "failed"
    output: Optional[str] = None
    error: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cost_usd: Optional[float] = None


@app.post("/api/team-members/{mid}/runs", status_code=201)
async def create_agent_run(
    mid: int, body: RunCreate, user: dict = Depends(current_user)
):
    """Create a queued run and dispatch it to the member's nemoclaw container."""
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    if not row:
        raise HTTPException(404, "team member not found")
    member = _row_to_member(row)
    if member["type"] != "ai_agent":
        raise HTTPException(400, "only ai_agent members can be invoked")
    if not member.get("runtime_container_id") or not member.get("runtime_endpoint"):
        raise HTTPException(400, "member has no running container — start it first")
    if member.get("runtime_status") != "running":
        raise HTTPException(400, f"container not running (status: {member.get('runtime_status')})")

    project = get_project_row(member["project_id"])
    if not project:
        raise HTTPException(500, "member's project disappeared")
    try:
        rt_cfg = json.loads(project.get("runtime_config") or "{}")
    except json.JSONDecodeError:
        rt_cfg = {}
    callback_url = rt_cfg.get("starforge_callback_url", "")
    if not callback_url:
        raise HTTPException(
            400,
            "project's starforge_callback_url isn't set — nemoclaw needs it "
            "to report results back. Configure it in the project's runtime settings.",
        )
    callback_token = ensure_project_callback_token(project["id"])

    run_id = str(uuid.uuid4())
    ts = now_iso()
    with db() as conn:
        conn.execute(
            """INSERT INTO agent_runs
               (id, member_id, status, inputs, triggered_by, created_at)
               VALUES (?, ?, 'queued', ?, ?, ?)""",
            (run_id, mid, json.dumps(body.inputs), user["id"], ts),
        )

    # Dispatch (fire-and-forget; we update status as we go).
    asyncio.create_task(
        _dispatch_run(
            run_id=run_id,
            member_endpoint=member["runtime_endpoint"],
            callback_url=callback_url,
            callback_token=callback_token,
            inputs=body.inputs,
            snapshot=(member.get("config") or {}).get("agent_snapshot") or {},
        )
    )

    with db() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(row)


async def _maybe_trigger_agent_for_task(task: dict[str, Any]) -> None:
    """If `task` was just (re)assigned to a running AI agent, auto-create a run
    for that agent with the task as input. Idempotent — won't fire if there's
    already an in-flight run for the same task+member."""
    assignee_id = task.get("assignee_id")
    if not assignee_id:
        return
    with db() as conn:
        member_row = conn.execute(
            "SELECT * FROM team_members WHERE id = ?", (assignee_id,)
        ).fetchone()
    if not member_row:
        return
    member = _row_to_member(member_row)
    if member["type"] != "ai_agent":
        return
    if member.get("runtime_status") != "running":
        # Could auto-start here; for v1, require the operator to start the agent first.
        log.info("task %s assigned to non-running agent %s — skipping auto-trigger",
                 task.get("id"), member["id"])
        return
    if not member.get("runtime_endpoint"):
        return
    project = get_project_row(member["project_id"])
    if not project:
        return
    try:
        rt_cfg = json.loads(project.get("runtime_config") or "{}")
    except json.JSONDecodeError:
        return
    callback_url = rt_cfg.get("starforge_callback_url", "")
    if not callback_url:
        log.info("project has no starforge_callback_url — skipping auto-trigger")
        return

    # Idempotency: skip if this member already has a queued/running run for this task
    with db() as conn:
        existing = conn.execute(
            """SELECT id FROM agent_runs
               WHERE member_id = ? AND status IN ('queued','running')
                 AND json_extract(inputs, '$.task_id') = ?""",
            (member["id"], task["id"]),
        ).fetchone()
    if existing:
        log.info("task %s already has an in-flight run for member %s — skipping",
                 task["id"], member["id"])
        return

    callback_token = ensure_project_callback_token(project["id"])
    run_id = str(uuid.uuid4())

    # Pull existing comments so the agent has the rejection / re-try context
    # of "what was said last time" when it's invoked. Trimmed shape — author
    # kind/name + body + timestamp — keeps the prompt token-cost reasonable
    # while giving the LLM enough to act on.
    prior_comments: list[dict[str, Any]] = []
    with db() as conn:
        comment_rows = conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
            (task["id"],),
        ).fetchall()
    for c in _hydrate_comments(comment_rows):
        prior_comments.append({
            "author_kind": c.get("author_kind") or "user",
            "author_name": c.get("author_name") or "—",
            "body": c.get("body") or "",
            "created_at": c.get("created_at") or "",
        })

    inputs = {
        "task_id": task["id"],
        "task_title": task.get("title", ""),
        "task_description": task.get("description", "") or "",
        "task_status": task.get("status", ""),
        "prior_comments": prior_comments,
    }
    with db() as conn:
        conn.execute(
            """INSERT INTO agent_runs (id, member_id, status, inputs, triggered_by, created_at)
               VALUES (?, ?, 'queued', ?, NULL, ?)""",
            (run_id, member["id"], json.dumps(inputs), now_iso()),
        )
    asyncio.create_task(
        _dispatch_run(
            run_id=run_id,
            member_endpoint=member["runtime_endpoint"],
            callback_url=callback_url,
            callback_token=callback_token,
            inputs=inputs,
            snapshot=(member.get("config") or {}).get("agent_snapshot") or {},
        )
    )
    log.info("auto-triggered run %s for member %s on task %s", run_id, member["id"], task["id"])


async def _dispatch_run(
    *,
    run_id: str,
    member_endpoint: str,
    callback_url: str,
    callback_token: str,
    inputs: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    """Send the invoke request to nemoclaw and flip the run row to 'running'.
    Failures here flip to 'failed' immediately so the UI doesn't spin forever."""
    payload = {
        "run_id": run_id,
        "callback_url": callback_url.rstrip("/"),
        "callback_token": callback_token,
        "inputs": inputs,
        "snapshot": snapshot,
    }
    try:
        await _dispatch_invoke(member_endpoint, payload, callback_token)
    except Exception as e:  # noqa: BLE001
        with db() as conn:
            conn.execute(
                """UPDATE agent_runs SET status='failed', error=?, completed_at=? WHERE id=?""",
                (f"dispatch failed: {e}", now_iso(), run_id),
            )
        return
    with db() as conn:
        conn.execute(
            "UPDATE agent_runs SET status='running', started_at=? WHERE id=? AND status='queued'",
            (now_iso(), run_id),
        )


@app.get("/api/team-members/{mid}/runs")
async def list_member_runs(
    mid: int, limit: int = 50, _: dict = Depends(current_user)
):
    limit = max(1, min(limit, 200))
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_runs WHERE member_id = ? ORDER BY created_at DESC LIMIT ?",
            (mid, limit),
        ).fetchall()
    return [_row_to_run(r) for r in rows]


@app.get("/api/agent-runs/{run_id}")
async def get_agent_run(run_id: str, _: dict = Depends(current_user)):
    with db() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "run not found")
    return _row_to_run(row)


@app.post("/api/agent-runs/{run_id}/cancel")
async def cancel_agent_run(run_id: str, _: dict = Depends(current_user)):
    with db() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise HTTPException(404, "run not found")
    run = _row_to_run(row)
    if run["status"] in {"succeeded", "failed", "cancelled"}:
        return run  # nothing to do
    # Best-effort: tell nemoclaw to stop. We mark cancelled regardless so the
    # callback (if it lands later) becomes a no-op.
    member_row = None
    with db() as conn:
        member_row = conn.execute("SELECT * FROM team_members WHERE id = ?", (run["member_id"],)).fetchone()
        conn.execute(
            "UPDATE agent_runs SET status='cancelled', completed_at=? WHERE id=?",
            (now_iso(), run_id),
        )
    if member_row and member_row["runtime_endpoint"]:
        try:
            project = get_project_row(member_row["project_id"]) or {}
            token = (get_project_secrets(project.get("id", 0)) or {}).get("callback_token", "")
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.delete(
                    f"{member_row['runtime_endpoint']}/runs/{run_id}",
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                )
        except Exception as e:  # noqa: BLE001
            log.info("cancel: best-effort DELETE failed for %s: %s", run_id, e)
    with db() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(row)


@app.post("/api/agent-runs/{run_id}/result")
async def post_agent_run_result(run_id: str, body: RunResult, request: Request):
    """Callback endpoint nemoclaw POSTs to when a run finishes.

    Authenticated by Bearer token matching the run's project's callback_token —
    NOT by the user session, since nemoclaw isn't a user.
    """
    # Look up the run (need member → project for token validation)
    with db() as conn:
        row = conn.execute(
            """SELECT r.*, m.project_id AS project_id
               FROM agent_runs r JOIN team_members m ON m.id = r.member_id
               WHERE r.id = ?""",
            (run_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "run not found")
    if row["status"] in {"succeeded", "failed", "cancelled"}:
        # Idempotent: late callback after a terminal state is a no-op.
        return {"ok": True, "ignored": True}

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    presented = auth[len("Bearer "):]
    expected = (get_project_secrets(row["project_id"]) or {}).get("callback_token")
    if not expected or not secrets_lib.compare_digest(presented, expected):
        raise HTTPException(401, "invalid callback token")

    if body.status not in {"succeeded", "failed"}:
        raise HTTPException(400, "status must be 'succeeded' or 'failed'")

    with db() as conn:
        conn.execute(
            """UPDATE agent_runs SET
                   status = ?, output = ?, error = ?,
                   tokens_in = ?, tokens_out = ?, cost_usd = ?,
                   completed_at = ?
               WHERE id = ?""",
            (
                body.status, body.output, body.error,
                body.tokens_in, body.tokens_out, body.cost_usd,
                now_iso(), run_id,
            ),
        )
    return {"ok": True}


HEALTH_CHECK_INTERVAL = 30  # seconds — fast-ish so killed containers surface within ~30s


async def check_member_health(member_id: int) -> Optional[str]:
    """Inspect the container and reconcile runtime_status if it drifted.

    Catches the case where the container was killed externally (`docker rm -f`,
    OOM, host reboot, etc.) — Starforge would otherwise keep showing the member
    as running forever. Returns the resolved status, or None if nothing to do.
    """
    with db() as conn:
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (member_id,)).fetchone()
    if not row:
        return None
    member = _row_to_member(row)
    if member["type"] != "ai_agent" or not member.get("runtime_container_id"):
        return None
    if member.get("runtime_status") not in ("running", "starting"):
        return None  # nothing claimed to be alive — leave alone
    project = get_project_row(member["project_id"])
    if not project:
        return None
    try:
        rt_cfg = json.loads(project.get("runtime_config") or "{}")
    except json.JSONDecodeError:
        return None
    try:
        rt = get_runtime_for_project(rt_cfg)
    except HTTPException:
        return None
    if rt is None:
        return None

    inspection = await rt.inspect(member["runtime_container_id"])
    if inspection is None:
        # Container vanished entirely
        _set_member_runtime_state(
            member_id,
            runtime_status="stopped",
            runtime_error="container no longer exists (killed or removed externally)",
        )
        return "stopped"

    docker_status = inspection.status
    if docker_status == "running":
        if member["runtime_status"] != "running":
            _set_member_runtime_state(member_id, runtime_status="running", runtime_error=None)
        return "running"
    # Anything not "running" (exited, dead, paused, restarting, …) → reconcile
    _set_member_runtime_state(
        member_id,
        runtime_status="stopped",
        runtime_error=f"container is not running (docker status: {docker_status})",
    )
    return "stopped"


async def check_all_member_health() -> int:
    """Iterate every AI member that claims to be running/starting and reconcile."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM team_members "
            "WHERE type = 'ai_agent' "
            "AND runtime_status IN ('running','starting') "
            "AND runtime_container_id IS NOT NULL"
        ).fetchall()
    n = 0
    for r in rows:
        try:
            await check_member_health(r["id"])
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("health check failed for member %s: %s", r["id"], e)
    return n


async def _health_check_loop() -> None:
    """Background reconciliation: detect externally-killed/exited containers
    and flip their DB row to 'stopped' so the UI shows reality."""
    log.info("health check loop started (interval=%ss)", HEALTH_CHECK_INTERVAL)
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        try:
            await check_all_member_health()
        except Exception as e:  # noqa: BLE001
            log.exception("health check loop iteration failed: %s", e)


async def _image_update_loop() -> None:
    """Background poll: rerun the image-update check on the configured interval."""
    log.info("image update loop started")
    while True:
        interval = get_update_check_interval()
        if interval <= 0:
            # Disabled — poll the setting periodically so a re-enable takes effect.
            await asyncio.sleep(30)
            continue
        try:
            await check_all_image_updates()
        except Exception as e:  # noqa: BLE001
            log.exception("image update loop iteration failed: %s", e)
        await asyncio.sleep(interval)


# ---------- Tasks (auth required) ----------

class TaskCreate(BaseModel):
    project_id: int
    title: str = Field(min_length=1, max_length=500)
    description: str = ""
    status: str = "todo"
    assignee: str = ""
    assignee_id: Optional[int] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    description: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    assignee_id: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None
    project_id: Optional[int] = None


@app.get("/tasks")
async def list_tasks(
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    assignee_id: Optional[int] = None,
    project_id: Optional[int] = None,
    _: dict = Depends(current_user),
):
    sql = """SELECT t.*,
                    m.id AS m_id, m.name AS m_name, m.color AS m_color,
                    m.type AS m_type, m.role AS m_role
             FROM tasks t
             LEFT JOIN team_members m ON m.id = t.assignee_id
             WHERE 1=1"""
    params: list[Any] = []
    if status:
        if status not in VALID_STATUSES:
            raise HTTPException(400, f"invalid status; use one of {sorted(VALID_STATUSES)}")
        sql += " AND t.status = ?"
        params.append(status)
    if assignee:
        sql += " AND t.assignee = ?"
        params.append(assignee)
    if assignee_id is not None:
        sql += " AND t.assignee_id = ?"
        params.append(assignee_id)
    if project_id is not None:
        sql += " AND t.project_id = ?"
        params.append(project_id)
    sql += " ORDER BY t.updated_at DESC"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        member = None
        if d.get("m_id") is not None:
            member = {
                "id": d["m_id"], "name": d["m_name"], "color": d["m_color"],
                "type": d["m_type"], "role": d["m_role"],
            }
        for k in ("m_id", "m_name", "m_color", "m_type", "m_role"):
            d.pop(k, None)
        try:
            d["metadata"] = json.loads(d.get("metadata") or "{}")
        except json.JSONDecodeError:
            d["metadata"] = {}
        d["assignee_member"] = member
        out.append(d)
    return out


@app.post("/tasks", status_code=201)
async def create_task(task: TaskCreate, user: dict = Depends(current_user)):
    if task.status not in VALID_STATUSES:
        raise HTTPException(400, f"invalid status; use one of {sorted(VALID_STATUSES)}")
    project = get_project_row(task.project_id)
    if not project:
        raise HTTPException(400, "project_id does not reference a known project")
    if project["is_archived"]:
        raise HTTPException(400, "cannot create tasks in an archived project")
    validate_assignee_for_project(task.assignee_id, task.project_id)
    ts = now_iso()
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO tasks
               (title, description, status, assignee, assignee_id, metadata,
                created_at, updated_at, created_by, project_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task.title, task.description, task.status, task.assignee, task.assignee_id,
             json.dumps(task.metadata), ts, ts, user["id"], task.project_id),
        )
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone()
    out = row_to_task(row, _fetch_member_for(task.assignee_id))
    # Auto-trigger: if the task was assigned to a running AI agent at create, fire a run.
    if task.assignee_id:
        asyncio.create_task(_maybe_trigger_agent_for_task(out))
    return out


@app.get("/tasks/{task_id}")
async def get_task(task_id: int, _: dict = Depends(current_user)):
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, "task not found")
    return row_to_task(row, _fetch_member_for(row["assignee_id"]))


@app.patch("/tasks/{task_id}")
async def update_task(task_id: int, patch: TaskUpdate, _: dict = Depends(current_user)):
    with db() as conn:
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not existing:
        raise HTTPException(404, "task not found")

    data = patch.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in VALID_STATUSES:
        raise HTTPException(400, f"invalid status; use one of {sorted(VALID_STATUSES)}")

    final_project_id = data.get("project_id", existing["project_id"])
    if "project_id" in data:
        target = get_project_row(final_project_id)
        if not target:
            raise HTTPException(400, "project_id does not reference a known project")
        if target["is_archived"]:
            raise HTTPException(400, "cannot move tasks into an archived project")
    final_assignee_id = data.get("assignee_id", existing["assignee_id"])
    # If the project changed and the assignee wasn't explicitly updated, drop a now-orphaned assignee
    if "project_id" in data and "assignee_id" not in data and final_assignee_id is not None:
        with db() as conn:
            mr = conn.execute(
                "SELECT project_id FROM team_members WHERE id = ?", (final_assignee_id,)
            ).fetchone()
        if not mr or mr["project_id"] != final_project_id:
            data["assignee_id"] = None
            final_assignee_id = None
    validate_assignee_for_project(final_assignee_id, final_project_id)

    fields, params = [], []
    for k, v in data.items():
        if k == "metadata":
            v = json.dumps(v)
        fields.append(f"{k} = ?")
        params.append(v)
    if not fields:
        raise HTTPException(400, "no fields to update")
    fields.append("updated_at = ?")
    params.append(now_iso())
    params.append(task_id)
    with db() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    out = row_to_task(row, _fetch_member_for(row["assignee_id"]))
    # Auto-trigger the assigned agent when either:
    #   (a) the task was just (re)assigned to a different AI agent, OR
    #   (b) the task was moved back into the "todo" column (e.g. dragged back
    #       for re-investigation, or reopened after being marked done)
    assignee_changed = (
        "assignee_id" in data
        and data["assignee_id"]
        and data["assignee_id"] != existing["assignee_id"]
    )
    moved_to_todo = (
        "status" in data
        and data["status"] == "todo"
        and existing["status"] != "todo"
    )
    if assignee_changed or moved_to_todo:
        asyncio.create_task(_maybe_trigger_agent_for_task(out))
    return out


@app.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: int, _: dict = Depends(current_user)):
    with db() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "task not found")


# ---------- Task comments (humans + AI agents both write here) ----------

class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


def _row_to_comment(row) -> dict[str, Any]:
    return dict(row)


def _hydrate_comments(rows: list) -> list[dict[str, Any]]:
    """Attach author display info: name + (email or agent_type) + is_agent."""
    out = []
    with db() as conn:
        for r in rows:
            d = dict(r)
            if d.get("author_user_id"):
                u = conn.execute(
                    "SELECT email, display_name FROM users WHERE id = ?",
                    (d["author_user_id"],),
                ).fetchone()
                if u:
                    d["author_name"] = u["display_name"] or u["email"]
                    d["author_kind"] = "user"
            if d.get("author_member_id"):
                m = conn.execute(
                    "SELECT name, agent_type FROM team_members WHERE id = ?",
                    (d["author_member_id"],),
                ).fetchone()
                if m:
                    d["author_name"] = m["name"]
                    d["author_kind"] = "agent"
                    d["author_agent_type"] = m["agent_type"]
            out.append(d)
    return out


@app.get("/api/tasks/{task_id}/comments")
async def list_task_comments(task_id: int, _: dict = Depends(current_user)):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            raise HTTPException(404, "task not found")
        rows = conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
    return _hydrate_comments(rows)


_MENTION_RE = re.compile(r"@([A-Za-z0-9._-]+)")


def find_mentioned_members(project_id: int, body: str) -> list[dict[str, Any]]:
    """Scan `body` for @tokens that match AI agent members in `project_id`.
    A match is case-insensitive equality against either the slugified member
    name or the member's agent_type slug."""
    tokens = {t.lower() for t in _MENTION_RE.findall(body)}
    if not tokens:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM team_members WHERE project_id = ? AND type = 'ai_agent'",
            (project_id,),
        ).fetchall()
    matched: list[dict[str, Any]] = []
    seen: set[int] = set()
    for r in rows:
        m = _row_to_member(r)
        candidates = {
            slugify(m.get("name") or ""),
            (m.get("agent_type") or "").lower(),
        }
        candidates.discard("")
        if tokens & candidates and m["id"] not in seen:
            matched.append(m)
            seen.add(m["id"])
    return matched


async def _trigger_agent_for_comment(
    member: dict[str, Any], task: dict[str, Any], triggering_comment: dict[str, Any]
) -> None:
    """Q&A variant of _maybe_trigger_agent_for_task: dispatch the agent in
    comment_reply mode with the full comment thread as context."""
    if member.get("runtime_status") != "running" or not member.get("runtime_endpoint"):
        log.info("mentioned agent %s isn't running — skipping reply trigger", member["id"])
        return
    project = get_project_row(member["project_id"])
    if not project:
        return
    try:
        rt_cfg = json.loads(project.get("runtime_config") or "{}")
    except json.JSONDecodeError:
        return
    callback_url = rt_cfg.get("starforge_callback_url", "")
    if not callback_url:
        return
    callback_token = ensure_project_callback_token(project["id"])

    # Full comment thread including the triggering one
    with db() as conn:
        comment_rows = conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
            (task["id"],),
        ).fetchall()
    prior_comments = [
        {
            "author_kind": c.get("author_kind") or "user",
            "author_name": c.get("author_name") or "—",
            "body": c.get("body") or "",
            "created_at": c.get("created_at") or "",
        }
        for c in _hydrate_comments(comment_rows)
    ]

    run_id = str(uuid.uuid4())
    inputs = {
        "task_id": task["id"],
        "task_title": task.get("title", ""),
        "task_description": task.get("description", "") or "",
        "task_status": task.get("status", ""),
        "mode": "comment_reply",
        "triggering_comment": triggering_comment,
        "prior_comments": prior_comments,
    }
    with db() as conn:
        conn.execute(
            """INSERT INTO agent_runs (id, member_id, status, inputs, triggered_by, created_at)
               VALUES (?, ?, 'queued', ?, NULL, ?)""",
            (run_id, member["id"], json.dumps(inputs), now_iso()),
        )
    asyncio.create_task(
        _dispatch_run(
            run_id=run_id,
            member_endpoint=member["runtime_endpoint"],
            callback_url=callback_url,
            callback_token=callback_token,
            inputs=inputs,
            snapshot=(member.get("config") or {}).get("agent_snapshot") or {},
        )
    )
    log.info("comment-triggered reply run %s for member %s on task %s",
             run_id, member["id"], task["id"])


@app.post("/api/tasks/{task_id}/comments", status_code=201)
async def create_task_comment(
    task_id: int, body: CommentCreate, user: dict = Depends(current_user)
):
    with db() as conn:
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task_row:
            raise HTTPException(404, "task not found")
        cur = conn.execute(
            """INSERT INTO task_comments
                   (task_id, author_user_id, body, created_at)
                   VALUES (?, ?, ?, ?)""",
            (task_id, user["id"], body.body, now_iso()),
        )
        row = conn.execute("SELECT * FROM task_comments WHERE id = ?", (cur.lastrowid,)).fetchone()
    hydrated = _hydrate_comments([row])[0]

    # Q&A trigger. Two paths, both user-only to prevent agent-to-agent loops:
    #   1) Explicit @mention → trigger each mentioned agent
    #   2) No mention but the task is assigned to an AI agent → trigger the assignee
    #      (most natural conversational UX: comment on the task and the assigned
    #       agent picks it up without you having to remember the @ syntax)
    task = dict(task_row)
    targets: list[dict[str, Any]] = find_mentioned_members(task["project_id"], body.body)
    if not targets and task.get("assignee_id"):
        with db() as conn:
            m_row = conn.execute(
                "SELECT * FROM team_members WHERE id = ?", (task["assignee_id"],)
            ).fetchone()
        if m_row:
            m = _row_to_member(m_row)
            if m["type"] == "ai_agent":
                targets = [m]

    triggering = {
        "author_kind": "user",
        "author_name": hydrated.get("author_name", ""),
        "body": body.body,
        "created_at": hydrated.get("created_at", ""),
    }
    for member in targets:
        asyncio.create_task(_trigger_agent_for_comment(member, task, triggering))

    return hydrated


# ---------- Agent task actions (status + comment, authed by callback token) ----------

class AgentTaskAction(BaseModel):
    """One action an agent wants to perform on a task."""
    type: str  # "set_status" | "comment"
    status: Optional[str] = None  # for set_status
    body: Optional[str] = None    # for comment


class AgentTaskActionsRequest(BaseModel):
    agent_member_id: int
    task_id: int
    actions: list[AgentTaskAction]


@app.post("/api/agents/task-actions")
async def agent_task_actions(body: AgentTaskActionsRequest, request: Request):
    """Endpoint nemoclaw calls to update a task or post a comment as the agent.
    Authenticated by Bearer token == the project's callback_token. The agent
    can only act on tasks within the project that issued the token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    presented = auth[len("Bearer "):]

    # Resolve member → project
    with db() as conn:
        member = conn.execute(
            "SELECT * FROM team_members WHERE id = ?", (body.agent_member_id,)
        ).fetchone()
    if not member:
        raise HTTPException(404, "agent member not found")
    member_dict = _row_to_member(member)
    if member_dict["type"] != "ai_agent":
        raise HTTPException(400, "agent_member_id must refer to an ai_agent member")

    expected = (get_project_secrets(member_dict["project_id"]) or {}).get("callback_token")
    if not expected or not secrets_lib.compare_digest(presented, expected):
        raise HTTPException(401, "invalid callback token")

    # Verify task belongs to the same project
    with db() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (body.task_id,)).fetchone()
    if not task:
        raise HTTPException(404, "task not found")
    if task["project_id"] != member_dict["project_id"]:
        raise HTTPException(403, "agent can only act on tasks in its own project")

    results = []
    for action in body.actions:
        if action.type == "set_status":
            if action.status not in VALID_STATUSES:
                raise HTTPException(400, f"invalid status: {action.status}")
            with db() as conn:
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (action.status, now_iso(), body.task_id),
                )
            results.append({"type": "set_status", "ok": True, "status": action.status})
        elif action.type == "comment":
            if not action.body or not action.body.strip():
                raise HTTPException(400, "comment body required")
            with db() as conn:
                conn.execute(
                    """INSERT INTO task_comments
                           (task_id, author_member_id, body, created_at)
                           VALUES (?, ?, ?, ?)""",
                    (body.task_id, body.agent_member_id, action.body, now_iso()),
                )
            results.append({"type": "comment", "ok": True})
        else:
            raise HTTPException(400, f"unknown action type: {action.type}")
    return {"ok": True, "results": results}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
