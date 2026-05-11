"""
Tests for the agent-type registry feature.

Covers:
- list_agent_types() reads ./agents/ correctly
- GET /api/agent-types requires auth and returns the registered agents
- team_members.agent_type column is migrated in
- Creating a team member validates the agent_type against the registry
- Type/agent_type combinations are validated (no human + agent_type, no unknown slug)
"""

from __future__ import annotations

import sqlite3


# ---------- list_agent_types() ----------

def test_list_agent_types_finds_network_engineer():
    import app
    types = app.list_agent_types()
    slugs = [t["slug"] for t in types]
    assert "network-engineer" in slugs, f"got slugs: {slugs}"


def test_agent_type_entry_has_expected_fields():
    import app
    types = {t["slug"]: t for t in app.list_agent_types()}
    ne = types["network-engineer"]
    assert ne["name"]
    assert ne["model"]  # something pinned — could be any provider/model
    assert "network" in ne["description"].lower()


# ---------- /api/agent-types endpoint ----------

def test_agent_types_endpoint_requires_auth(client):
    fresh = client.__class__(client.app)
    # No setup-then-cookie flow on the fresh client — should reject.
    resp = fresh.get("/api/agent-types")
    assert resp.status_code == 401


def test_agent_types_endpoint_returns_registry(admin_client):
    resp = admin_client.get("/api/agent-types")
    assert resp.status_code == 200, resp.text
    slugs = {t["slug"] for t in resp.json()}
    assert "network-engineer" in slugs


# ---------- schema migration ----------

def test_team_members_has_agent_type_column(test_data_dir):
    conn = sqlite3.connect(test_data_dir / "board.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(team_members)").fetchall()]
    conn.close()
    assert "agent_type" in cols


# ---------- create / update validation ----------

def _new_project(admin_client, name: str) -> dict:
    r = admin_client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_ai_member_with_valid_agent_type(admin_client):
    p = _new_project(admin_client, "Net Project A")
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Net Eng 1", "type": "ai_agent", "agent_type": "network-engineer"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["agent_type"] == "network-engineer"
    assert body["type"] == "ai_agent"


def test_create_ai_member_with_unknown_agent_type_rejected(admin_client):
    p = _new_project(admin_client, "Net Project B")
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Phantom", "type": "ai_agent", "agent_type": "does-not-exist"},
    )
    assert r.status_code == 400
    assert "unknown agent_type" in r.json().get("detail", "").lower()


def test_create_human_with_agent_type_rejected(admin_client):
    p = _new_project(admin_client, "Net Project C")
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Travis", "type": "human", "agent_type": "network-engineer"},
    )
    assert r.status_code == 400
    assert "ai_agent" in r.json().get("detail", "").lower()


def test_create_member_without_agent_type_still_works(admin_client):
    """agent_type is optional even for ai_agent — the wizard hasn't filled it in yet."""
    p = _new_project(admin_client, "Net Project D")
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Unbound Agent", "type": "ai_agent"},
    )
    assert r.status_code == 201
    assert r.json()["agent_type"] is None


def test_member_type_is_immutable_after_creation(admin_client):
    p = admin_client.post("/api/projects", json={"name": "Type Immutable Project"}).json()
    create = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Locked Type", "type": "human"},
    )
    mid = create.json()["id"]
    # Trying to convert human → ai_agent must be rejected
    r = admin_client.patch(f"/api/team-members/{mid}", json={"type": "ai_agent"})
    assert r.status_code == 400
    # PATCHing the same type (no-op) is fine
    r2 = admin_client.patch(f"/api/team-members/{mid}", json={"type": "human", "role": "fixed role"})
    assert r2.status_code == 200
    assert r2.json()["role"] == "fixed role"


def test_patch_member_to_invalid_agent_type_rejected(admin_client):
    p = _new_project(admin_client, "Net Project E")
    create = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Patchable", "type": "ai_agent", "agent_type": "network-engineer"},
    )
    mid = create.json()["id"]
    r = admin_client.patch(f"/api/team-members/{mid}", json={"agent_type": "nope"})
    assert r.status_code == 400


def test_patch_human_member_with_agent_type_rejected(admin_client):
    p = _new_project(admin_client, "Net Project F")
    create = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "A Human", "type": "human"},
    )
    mid = create.json()["id"]
    r = admin_client.patch(f"/api/team-members/{mid}", json={"agent_type": "network-engineer"})
    assert r.status_code == 400
