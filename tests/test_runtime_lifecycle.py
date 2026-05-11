"""
Tests for the runtime adapter lifecycle (Phase B.2).

These run against the in-memory FakeRuntime — no real Docker daemon required.
The fake records every call in self.calls so we can assert lifecycle ordering.
"""

from __future__ import annotations


def _new_project(admin_client, name: str) -> dict:
    r = admin_client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _configure_runtime(admin_client, project_id: int, image: str = "starforge-nemoclaw:dev") -> None:
    r = admin_client.put(
        f"/api/projects/{project_id}/runtime-config",
        json={
            "type": "docker",
            "docker_host": "unix:///var/run/docker.sock",
            "image": image,
            "image_pull_policy": "if_not_present",
        },
    )
    assert r.status_code == 200, r.text


def _create_ai_member(admin_client, project_id: int, name: str = "AI-1",
                       agent_type: str = "network-engineer") -> dict:
    r = admin_client.post(
        f"/api/projects/{project_id}/members",
        json={"name": name, "type": "ai_agent", "agent_type": agent_type},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------- adapter interface sanity ----------

def test_fake_runtime_implements_adapter():
    from runtime_adapter import RuntimeAdapter
    from runtime_fake import FakeRuntime
    fake = FakeRuntime()
    assert isinstance(fake, RuntimeAdapter)


# ---------- provisioning on member create ----------

def test_member_create_provisions_when_runtime_configured(admin_client, fake_runtime):
    p = _new_project(admin_client, "Provision Auto Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    # FakeRuntime ran sync — member should already be running
    assert member["runtime_status"] == "running"
    assert member["runtime_container_id"].startswith("fake-cid-")
    assert member["runtime_endpoint"].startswith("http://fake-")
    assert member["runtime_image_digest"]
    assert ("provision", member["id"]) in fake_runtime.calls


def test_member_create_skips_provision_when_no_runtime(admin_client, fake_runtime):
    p = _new_project(admin_client, "No Runtime Project")
    # No _configure_runtime call → project.runtime_config is empty
    member = _create_ai_member(admin_client, p["id"])
    assert member["runtime_status"] == "not_provisioned"
    assert member["runtime_container_id"] is None
    assert fake_runtime.calls == []


def test_member_create_skips_provision_when_no_agent_type(admin_client, fake_runtime):
    p = _new_project(admin_client, "No Agent Type Project")
    _configure_runtime(admin_client, p["id"])
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Unbound", "type": "ai_agent"},  # no agent_type
    )
    assert r.status_code == 201
    assert r.json()["runtime_status"] == "not_provisioned"
    assert fake_runtime.calls == []


def test_human_member_does_not_provision(admin_client, fake_runtime):
    p = _new_project(admin_client, "Human Project")
    _configure_runtime(admin_client, p["id"])
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Travis", "type": "human"},
    )
    assert r.status_code == 201
    assert fake_runtime.calls == []


# ---------- teardown on delete ----------

def test_member_delete_removes_container(admin_client, fake_runtime):
    p = _new_project(admin_client, "Delete Removes Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    cid = member["runtime_container_id"]
    assert cid in fake_runtime.containers

    r = admin_client.delete(f"/api/team-members/{member['id']}")
    assert r.status_code == 204
    assert cid not in fake_runtime.containers
    assert ("remove", cid) in fake_runtime.calls


# ---------- start / stop endpoints ----------

def test_stop_endpoint_pauses_container(admin_client, fake_runtime):
    p = _new_project(admin_client, "Stop Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    cid = member["runtime_container_id"]

    r = admin_client.post(f"/api/team-members/{member['id']}/runtime/stop")
    assert r.status_code == 200
    body = r.json()
    assert body["runtime_status"] == "stopped"
    assert fake_runtime.containers[cid]["status"] == "stopped"


def test_start_endpoint_resumes_stopped_container(admin_client, fake_runtime):
    p = _new_project(admin_client, "Restart-Stopped Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    admin_client.post(f"/api/team-members/{member['id']}/runtime/stop")

    r = admin_client.post(f"/api/team-members/{member['id']}/runtime/start")
    assert r.status_code == 200
    body = r.json()
    assert body["runtime_status"] == "running"
    cid = body["runtime_container_id"]
    assert fake_runtime.containers[cid]["status"] == "running"


def test_start_endpoint_provisions_fresh_when_not_yet_provisioned(admin_client, fake_runtime):
    p = _new_project(admin_client, "Start From Scratch Project")
    # First, add member WITHOUT runtime config so it doesn't auto-provision
    member = _create_ai_member(admin_client, p["id"])
    assert member["runtime_status"] == "not_provisioned"
    # Now configure runtime and explicitly start
    _configure_runtime(admin_client, p["id"])
    r = admin_client.post(f"/api/team-members/{member['id']}/runtime/start")
    assert r.status_code == 200
    body = r.json()
    assert body["runtime_status"] == "running"
    assert body["runtime_container_id"]


def test_stop_unprovisioned_member_rejected(admin_client, fake_runtime):
    p = _new_project(admin_client, "Stop Unprov Project")
    member = _create_ai_member(admin_client, p["id"])
    r = admin_client.post(f"/api/team-members/{member['id']}/runtime/stop")
    assert r.status_code == 400


def test_runtime_endpoints_reject_human_members(admin_client, fake_runtime):
    p = _new_project(admin_client, "Human Rejected Project")
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "H1", "type": "human"},
    )
    mid = r.json()["id"]
    s = admin_client.post(f"/api/team-members/{mid}/runtime/start")
    assert s.status_code == 400


# ---------- restart + image update ----------

def test_restart_recreates_container(admin_client, fake_runtime):
    p = _new_project(admin_client, "Restart Recreate Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    old_cid = member["runtime_container_id"]
    r = admin_client.post(f"/api/team-members/{member['id']}/runtime/restart")
    assert r.status_code == 200
    new_cid = r.json()["runtime_container_id"]
    assert new_cid != old_cid
    assert ("remove", old_cid) in fake_runtime.calls
    assert ("provision", member["id"]) in fake_runtime.calls


def test_restart_with_pull_bumps_image_digest(admin_client, fake_runtime):
    p = _new_project(admin_client, "Restart Pull Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    original_digest = member["runtime_image_digest"]

    r = admin_client.post(f"/api/team-members/{member['id']}/runtime/restart?pull=true")
    assert r.status_code == 200
    new_digest = r.json()["runtime_image_digest"]
    # FakeRuntime bumps digest on every pull
    assert new_digest != original_digest
