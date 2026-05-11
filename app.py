"""Starforge — task API + auth + OIDC SSO + admin settings."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

import yaml

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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_team_project ON team_members(project_id)")


init_auth_schema()
init_projects_schema()
init_team_members_schema()
init_tasks_schema()

app = FastAPI(title="Starforge", version="0.3.0")
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
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_member(row)


@app.patch("/api/team-members/{mid}")
async def update_member(mid: int, body: TeamMemberUpdate, _: dict = Depends(current_user)):
    with db() as conn:
        existing = conn.execute("SELECT * FROM team_members WHERE id = ?", (mid,)).fetchone()
    if not existing:
        raise HTTPException(404, "team member not found")
    data = body.model_dump(exclude_unset=True)
    if "type" in data and data["type"] not in VALID_MEMBER_TYPES:
        raise HTTPException(400, f"invalid type; use one of {sorted(VALID_MEMBER_TYPES)}")
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
        cur = conn.execute("DELETE FROM team_members WHERE id = ?", (mid,))
        if cur.rowcount == 0:
            raise HTTPException(404, "team member not found")


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
    return row_to_task(row, _fetch_member_for(task.assignee_id))


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
    return row_to_task(row, _fetch_member_for(row["assignee_id"]))


@app.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: int, _: dict = Depends(current_user)):
    with db() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "task not found")


@app.get("/healthz")
async def healthz():
    return {"ok": True}
