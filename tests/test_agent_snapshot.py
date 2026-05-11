"""
Tests for the Phase B agent-snapshot resolver.

Covers:
- resolve_agent_snapshot() reads config + system_prompt + guardrails and produces
  a deterministic content hash
- source:file references resolve correctly (YAML files → dict, .md files → str)
- Snapshot is persisted into team_members.config on create when agent_type is set
- /api/team-members/{id}/agent-snapshot returns the snapshot + freshness check
- /api/team-members/{id}/refresh-snapshot re-resolves and updates is_stale
- Changing agent_type via PATCH regenerates the snapshot
- Clearing agent_type drops the snapshot
"""

from __future__ import annotations

import json


def _new_project(admin_client, name: str) -> dict:
    r = admin_client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


# ---------- resolver unit tests ----------

def test_resolver_produces_snapshot_with_content():
    import app
    snap = app.resolve_agent_snapshot("network-engineer")
    # Top-level keys
    assert snap["agent_type"] == "network-engineer"
    assert snap["content_hash"]
    assert snap["snapshot_at"]
    # Sources tracked
    assert snap["sources"]["config"].endswith("config.yaml")
    assert snap["sources"]["system_prompt"].endswith("system_prompt.md")
    assert snap["sources"]["guardrails"].endswith("guardrails.yaml")
    # Resolved content
    assert isinstance(snap["system_prompt"], str)
    assert "network" in snap["system_prompt"].lower()
    assert isinstance(snap["guardrails"], dict)
    assert "input_rails" in snap["guardrails"]


def test_resolver_hash_is_deterministic():
    import app
    a = app.resolve_agent_snapshot("network-engineer")
    b = app.resolve_agent_snapshot("network-engineer")
    assert a["content_hash"] == b["content_hash"]


def test_resolver_unknown_slug_404():
    import app
    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException) as exc:
        app.resolve_agent_snapshot("does-not-exist")
    assert exc.value.status_code == 404


# ---------- snapshot persistence on create ----------

def test_member_create_persists_snapshot(admin_client):
    p = _new_project(admin_client, "Snapshot Project A")
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={
            "name": "Bound Agent",
            "type": "ai_agent",
            "agent_type": "network-engineer",
        },
    )
    assert r.status_code == 201, r.text
    member = r.json()
    config = member["config"]
    assert "agent_snapshot" in config
    snap = config["agent_snapshot"]
    assert snap["agent_type"] == "network-engineer"
    assert snap["content_hash"]
    assert "network" in snap["system_prompt"].lower()


def test_member_create_without_agent_type_has_no_snapshot(admin_client):
    p = _new_project(admin_client, "Snapshot Project B")
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Unbound", "type": "ai_agent"},
    )
    assert r.status_code == 201
    assert "agent_snapshot" not in r.json()["config"]


# ---------- snapshot endpoint ----------

def test_get_snapshot_endpoint_returns_fresh(admin_client):
    p = _new_project(admin_client, "Snapshot Project C")
    create = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "S1", "type": "ai_agent", "agent_type": "network-engineer"},
    )
    mid = create.json()["id"]
    r = admin_client.get(f"/api/team-members/{mid}/agent-snapshot")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["snapshot"]["agent_type"] == "network-engineer"
    assert body["is_stale"] is False
    assert body["current_hash"] == body["snapshot"]["content_hash"]


def test_get_snapshot_for_unbound_member(admin_client):
    p = _new_project(admin_client, "Snapshot Project D")
    create = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Human Joe", "type": "human"},
    )
    mid = create.json()["id"]
    r = admin_client.get(f"/api/team-members/{mid}/agent-snapshot")
    assert r.status_code == 200
    assert r.json()["snapshot"] is None


# ---------- staleness + refresh ----------

def test_snapshot_goes_stale_when_source_changes(admin_client, tmp_path, monkeypatch):
    """Mutate the underlying file and confirm is_stale flips."""
    import app
    p = _new_project(admin_client, "Stale Project")
    create = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Drifter", "type": "ai_agent", "agent_type": "network-engineer"},
    )
    mid = create.json()["id"]

    # Mutate by pointing AGENTS_DIR at a temp copy with a tweaked prompt
    src_dir = app.AGENTS_DIR / "network-engineer"
    fake_root = tmp_path / "agents"
    fake_agent = fake_root / "network-engineer"
    fake_agent.mkdir(parents=True)
    for f in src_dir.iterdir():
        (fake_agent / f.name).write_bytes(f.read_bytes())
    # mutate the system prompt
    sp = fake_agent / "system_prompt.md"
    sp.write_text(sp.read_text() + "\n\n# drift marker\n", encoding="utf-8")

    monkeypatch.setattr(app, "AGENTS_DIR", fake_root)

    r = admin_client.get(f"/api/team-members/{mid}/agent-snapshot")
    body = r.json()
    assert body["is_stale"] is True
    assert body["current_hash"] != body["snapshot"]["content_hash"]


def test_refresh_endpoint_updates_snapshot(admin_client, tmp_path, monkeypatch):
    import app
    p = _new_project(admin_client, "Refresh Project")
    create = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Refreshable", "type": "ai_agent", "agent_type": "network-engineer"},
    )
    mid = create.json()["id"]
    original_hash = create.json()["config"]["agent_snapshot"]["content_hash"]

    # Replace source with a drifted copy
    src_dir = app.AGENTS_DIR / "network-engineer"
    fake_root = tmp_path / "agents"
    fake_agent = fake_root / "network-engineer"
    fake_agent.mkdir(parents=True)
    for f in src_dir.iterdir():
        (fake_agent / f.name).write_bytes(f.read_bytes())
    (fake_agent / "system_prompt.md").write_text("CHANGED", encoding="utf-8")
    monkeypatch.setattr(app, "AGENTS_DIR", fake_root)

    # Confirm stale before refresh
    pre = admin_client.get(f"/api/team-members/{mid}/agent-snapshot").json()
    assert pre["is_stale"] is True

    # Refresh
    r = admin_client.post(f"/api/team-members/{mid}/refresh-snapshot")
    assert r.status_code == 200
    new_snap = r.json()["config"]["agent_snapshot"]
    assert new_snap["content_hash"] != original_hash
    assert new_snap["system_prompt"] == "CHANGED"

    # And the snapshot endpoint now reports fresh
    post = admin_client.get(f"/api/team-members/{mid}/agent-snapshot").json()
    assert post["is_stale"] is False


def test_refresh_on_unbound_member_rejected(admin_client):
    p = _new_project(admin_client, "Refresh Reject")
    create = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Plain Human", "type": "human"},
    )
    mid = create.json()["id"]
    r = admin_client.post(f"/api/team-members/{mid}/refresh-snapshot")
    assert r.status_code == 400


# ---------- patch regenerates / clears snapshot ----------

def test_patch_changing_agent_type_regenerates_snapshot(admin_client, tmp_path):
    """If we PATCH agent_type to a different slug, a new snapshot is built."""
    import app
    # Build a second fake agent on disk so we have two slugs to choose between
    new_slug = "alt-agent-test"
    alt_dir = app.AGENTS_DIR / new_slug
    alt_dir.mkdir(parents=True, exist_ok=True)
    (alt_dir / "config.yaml").write_text(
        "agent:\n  name: alt-agent\n  description: alt\n  model: claude-sonnet-4-6\n"
        "  system_prompt: \"alt prompt body\"\n",
        encoding="utf-8",
    )
    try:
        p = _new_project(admin_client, "Patch Snapshot Project")
        create = admin_client.post(
            f"/api/projects/{p['id']}/members",
            json={"name": "Switcher", "type": "ai_agent", "agent_type": "network-engineer"},
        )
        mid = create.json()["id"]
        original_hash = create.json()["config"]["agent_snapshot"]["content_hash"]

        # Switch to the alt agent
        r = admin_client.patch(f"/api/team-members/{mid}", json={"agent_type": new_slug})
        assert r.status_code == 200
        new_snap = r.json()["config"]["agent_snapshot"]
        assert new_snap["agent_type"] == new_slug
        assert new_snap["content_hash"] != original_hash
        assert new_snap["system_prompt"] == "alt prompt body"
    finally:
        # Cleanup the fake agent dir so other tests / repo aren't polluted
        for f in alt_dir.iterdir():
            f.unlink()
        alt_dir.rmdir()


def test_patch_clearing_agent_type_drops_snapshot(admin_client):
    p = _new_project(admin_client, "Patch Clear Project")
    create = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Clear Me", "type": "ai_agent", "agent_type": "network-engineer"},
    )
    mid = create.json()["id"]
    assert "agent_snapshot" in create.json()["config"]

    r = admin_client.patch(f"/api/team-members/{mid}", json={"agent_type": None})
    assert r.status_code == 200
    assert "agent_snapshot" not in r.json()["config"]
