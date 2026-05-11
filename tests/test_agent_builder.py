"""
Tests for the agent-builder meta-flow:
- POST /api/agent-types creates a draft (auth'd by project callback token)
- list_agent_types(include_drafts=False) hides drafts
- list_agent_types(include_drafts=True) shows them
- admin endpoints to list / activate / reject drafts
- Rejection cleanup
- Live agents can't be deleted through the reject endpoint (safety)
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

AGENTS_ROOT = Path(__file__).resolve().parent.parent / "agents"


@pytest.fixture
def cleanup_drafts():
    """Remove any draft directories we create so tests don't pollute the repo."""
    created: list[str] = []
    yield created
    for slug in created:
        d = AGENTS_ROOT / slug
        if d.exists():
            shutil.rmtree(d)


def _new_project_with_runtime(admin_client):
    p = admin_client.post("/api/projects", json={"name": "Agent Builder Test"}).json()
    admin_client.put(
        f"/api/projects/{p['id']}/runtime-config",
        json={"type": "docker", "image": "starforge-nemoclaw:dev",
              "image_pull_policy": "if_not_present",
              "starforge_callback_url": "http://x:8000"},
    )
    return p


def _create_ai_member(admin_client, pid, name="builder"):
    r = admin_client.post(
        f"/api/projects/{pid}/members",
        json={"name": name, "type": "ai_agent", "agent_type": "network-engineer"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------- draft creation ----------

def test_create_agent_type_draft_writes_files(admin_client, fake_runtime, cleanup_drafts):
    p = _new_project_with_runtime(admin_client)
    member = _create_ai_member(admin_client, p["id"])
    token = fake_runtime.containers[member["runtime_container_id"]]["secrets_seen"]["callback_token"]

    slug = "test-drafted-agent"
    cleanup_drafts.append(slug)

    r = admin_client.post(
        f"/api/agent-types?created_by_member_id={member['id']}",
        json={
            "slug": slug,
            "name": "Test Drafted Agent",
            "description": "Built by the test suite",
            "model": "llama3.1:8b",
            "provider": "ollama",
            "provider_endpoint": "http://host.docker.internal:11434/v1",
            "system_prompt": "# Test\n\nYou are a test agent that does test things.",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft"

    d = AGENTS_ROOT / slug
    assert (d / "config.yaml").exists()
    assert (d / "system_prompt.md").exists()
    assert (d / "_status.yaml").exists()


def test_draft_hidden_from_default_list(admin_client, fake_runtime, cleanup_drafts):
    p = _new_project_with_runtime(admin_client)
    member = _create_ai_member(admin_client, p["id"])
    token = fake_runtime.containers[member["runtime_container_id"]]["secrets_seen"]["callback_token"]

    slug = "test-hidden-draft"
    cleanup_drafts.append(slug)
    admin_client.post(
        f"/api/agent-types?created_by_member_id={member['id']}",
        json={"slug": slug, "name": "Hidden", "model": "x", "provider": "ollama",
              "system_prompt": "the system prompt that is long enough"},
        headers={"Authorization": f"Bearer {token}"},
    )

    listed = admin_client.get("/api/agent-types").json()
    slugs = [a["slug"] for a in listed]
    assert slug not in slugs  # hidden because still draft


def test_admin_can_list_drafts(admin_client, fake_runtime, cleanup_drafts):
    p = _new_project_with_runtime(admin_client)
    member = _create_ai_member(admin_client, p["id"])
    token = fake_runtime.containers[member["runtime_container_id"]]["secrets_seen"]["callback_token"]

    slug = "test-listable-draft"
    cleanup_drafts.append(slug)
    admin_client.post(
        f"/api/agent-types?created_by_member_id={member['id']}",
        json={"slug": slug, "name": "Listable", "model": "x", "provider": "ollama",
              "system_prompt": "the system prompt that is long enough"},
        headers={"Authorization": f"Bearer {token}"},
    )

    r = admin_client.get("/api/admin/agent-types/drafts")
    assert r.status_code == 200
    slugs = [a["slug"] for a in r.json()]
    assert slug in slugs


def test_admin_activate_promotes_draft(admin_client, fake_runtime, cleanup_drafts):
    p = _new_project_with_runtime(admin_client)
    member = _create_ai_member(admin_client, p["id"])
    token = fake_runtime.containers[member["runtime_container_id"]]["secrets_seen"]["callback_token"]

    slug = "test-activatable"
    cleanup_drafts.append(slug)
    admin_client.post(
        f"/api/agent-types?created_by_member_id={member['id']}",
        json={"slug": slug, "name": "ActivateMe", "model": "x", "provider": "ollama",
              "system_prompt": "the system prompt that is long enough"},
        headers={"Authorization": f"Bearer {token}"},
    )

    r = admin_client.post(f"/api/admin/agent-types/{slug}/activate")
    assert r.status_code == 200
    assert r.json()["status"] == "active"

    listed_slugs = [a["slug"] for a in admin_client.get("/api/agent-types").json()]
    assert slug in listed_slugs


def test_admin_reject_deletes_draft(admin_client, fake_runtime, cleanup_drafts):
    p = _new_project_with_runtime(admin_client)
    member = _create_ai_member(admin_client, p["id"])
    token = fake_runtime.containers[member["runtime_container_id"]]["secrets_seen"]["callback_token"]

    slug = "test-rejectable"
    cleanup_drafts.append(slug)  # in case rejection fails
    admin_client.post(
        f"/api/agent-types?created_by_member_id={member['id']}",
        json={"slug": slug, "name": "RejectMe", "model": "x", "provider": "ollama",
              "system_prompt": "the system prompt that is long enough"},
        headers={"Authorization": f"Bearer {token}"},
    )

    r = admin_client.delete(f"/api/admin/agent-types/{slug}")
    assert r.status_code == 204
    assert not (AGENTS_ROOT / slug).exists()


def test_admin_cannot_delete_live_agent_types(admin_client):
    """The reject endpoint refuses to nuke a non-draft. Prevents accidental
    deletion of working agents (like network-engineer)."""
    r = admin_client.delete("/api/admin/agent-types/network-engineer")
    assert r.status_code == 400


def test_create_rejects_invalid_provider(admin_client, fake_runtime, cleanup_drafts):
    p = _new_project_with_runtime(admin_client)
    member = _create_ai_member(admin_client, p["id"])
    token = fake_runtime.containers[member["runtime_container_id"]]["secrets_seen"]["callback_token"]

    r = admin_client.post(
        f"/api/agent-types?created_by_member_id={member['id']}",
        json={"slug": "bad-provider", "name": "BP", "model": "x", "provider": "lambda",
              "system_prompt": "the system prompt that is long enough"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


def test_create_rejects_duplicate_slug(admin_client, fake_runtime, cleanup_drafts):
    p = _new_project_with_runtime(admin_client)
    member = _create_ai_member(admin_client, p["id"])
    token = fake_runtime.containers[member["runtime_container_id"]]["secrets_seen"]["callback_token"]

    # network-engineer already exists from the repo
    r = admin_client.post(
        f"/api/agent-types?created_by_member_id={member['id']}",
        json={"slug": "network-engineer", "name": "Dupe", "model": "x", "provider": "ollama",
              "system_prompt": "the system prompt that is long enough"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 409


def test_create_rejects_bad_token(admin_client, cleanup_drafts):
    r = admin_client.post(
        "/api/agent-types?created_by_member_id=1",
        json={"slug": "bad-token", "name": "BT", "model": "x", "provider": "ollama",
              "system_prompt": "the system prompt that is long enough"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401
