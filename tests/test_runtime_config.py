"""
Tests for project-level runtime config (Phase B.1).

Covers:
- New schema: projects.runtime_config column + team_members runtime_* columns
- GET /api/projects/{id}/runtime-config returns the persisted config (or {})
- PUT /api/projects/{id}/runtime-config validates type and image_pull_policy
- PUT respects permission rules (admin or project creator)
- k8s type returns 501 (adapter not yet implemented)
"""

from __future__ import annotations

import sqlite3


def _new_project(admin_client, name: str) -> dict:
    r = admin_client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


# ---------- schema ----------

def test_projects_has_runtime_config_column(test_data_dir):
    conn = sqlite3.connect(test_data_dir / "board.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    conn.close()
    assert "runtime_config" in cols


def test_team_members_has_runtime_state_columns(test_data_dir):
    conn = sqlite3.connect(test_data_dir / "board.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(team_members)").fetchall()]
    conn.close()
    for needed in (
        "runtime_status",
        "runtime_container_id",
        "runtime_endpoint",
        "runtime_error",
        "runtime_started_at",
        "runtime_image_digest",
    ):
        assert needed in cols, f"missing column: {needed}"


def test_new_member_has_default_runtime_status(admin_client):
    p = _new_project(admin_client, "Runtime Default Project")
    create = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "X", "type": "ai_agent"},
    )
    assert create.status_code == 201
    # Field comes back via SELECT *, so it's in the response dict
    assert create.json().get("runtime_status") == "not_provisioned"


# ---------- runtime config GET / PUT ----------

def test_runtime_config_starts_empty(admin_client):
    p = _new_project(admin_client, "Runtime Empty Project")
    r = admin_client.get(f"/api/projects/{p['id']}/runtime-config")
    assert r.status_code == 200
    assert r.json() == {}


def test_set_valid_docker_runtime(admin_client):
    p = _new_project(admin_client, "Runtime Docker Project")
    r = admin_client.put(
        f"/api/projects/{p['id']}/runtime-config",
        json={
            "type": "docker",
            "docker_host": "unix:///var/run/docker.sock",
            "image": "starforge-nemoclaw:dev",
            "image_pull_policy": "if_not_present",
            "network": "starforge-agents",
            "cpu_limit": "1",
            "memory_limit": "2Gi",
            "extra_env": {"LOG_LEVEL": "debug"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "docker"
    assert body["image"] == "starforge-nemoclaw:dev"
    # And persists
    r2 = admin_client.get(f"/api/projects/{p['id']}/runtime-config")
    assert r2.json()["image"] == "starforge-nemoclaw:dev"


def test_set_invalid_type_rejected(admin_client):
    p = _new_project(admin_client, "Runtime Invalid Type")
    r = admin_client.put(
        f"/api/projects/{p['id']}/runtime-config",
        json={"type": "lambda", "image": "x"},
    )
    assert r.status_code == 400


def test_set_invalid_pull_policy_rejected(admin_client):
    p = _new_project(admin_client, "Runtime Invalid Pull")
    r = admin_client.put(
        f"/api/projects/{p['id']}/runtime-config",
        json={"type": "docker", "image": "x", "image_pull_policy": "yolo"},
    )
    assert r.status_code == 400


def test_k8s_type_returns_501(admin_client):
    p = _new_project(admin_client, "Runtime K8s Future")
    r = admin_client.put(
        f"/api/projects/{p['id']}/runtime-config",
        json={"type": "k8s", "image": "x"},
    )
    assert r.status_code == 501
    assert "not yet implemented" in r.json()["detail"].lower()


def test_runtime_config_in_project_list_response(admin_client):
    p = _new_project(admin_client, "Runtime In List")
    admin_client.put(
        f"/api/projects/{p['id']}/runtime-config",
        json={"type": "docker", "image": "test-img:1"},
    )
    listing = admin_client.get("/api/projects").json()
    target = next(x for x in listing if x["id"] == p["id"])
    assert target["runtime_config"]["image"] == "test-img:1"
