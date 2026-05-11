"""
Tests for runtime-container health reconciliation.

Repro the user-reported bug: user `docker kill`s the agent's container, but
Starforge still reports runtime_status='running' until the next provision /
explicit stop. The health check loop fixes this — these tests prove it.
"""

from __future__ import annotations

import asyncio


def _new_project(admin_client, name: str) -> dict:
    r = admin_client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _configure_runtime(admin_client, pid: int) -> None:
    admin_client.put(
        f"/api/projects/{pid}/runtime-config",
        json={"type": "docker", "image": "starforge-nemoclaw:dev",
              "image_pull_policy": "if_not_present",
              "starforge_callback_url": "http://x:8000"},
    )


def _create_ai_member(admin_client, pid: int, name="AI") -> dict:
    r = admin_client.post(
        f"/api/projects/{pid}/members",
        json={"name": name, "type": "ai_agent", "agent_type": "network-engineer"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------- health reconciliation ----------

def test_killed_container_resets_to_not_provisioned(admin_client, fake_runtime):
    """User-reported bug: kill the container externally — health check should
    catch it, flip status, AND clear container_id so the next Start does a
    fresh provision instead of trying to start a dead ID."""
    import app
    p = _new_project(admin_client, "Health Killed Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    assert member["runtime_status"] == "running"
    cid = member["runtime_container_id"]

    # Simulate `docker rm -f` by removing the container from FakeRuntime's store
    fake_runtime.containers.pop(cid, None)

    asyncio.run(app.check_member_health(member["id"]))

    out = admin_client.get(f"/api/projects/{p['id']}/members").json()
    rec = next(m for m in out if m["id"] == member["id"])
    assert rec["runtime_status"] == "not_provisioned"
    assert rec["runtime_container_id"] is None
    assert rec["runtime_endpoint"] is None
    assert "no longer exists" in (rec["runtime_error"] or "")


def test_start_after_external_kill_provisions_fresh(admin_client, fake_runtime):
    """Closing the loop on the user-reported scenario: kill container, click
    Start, expect a NEW container in fake_runtime (fresh provision, not a
    no-op `docker start` of the dead ID)."""
    import app
    p = _new_project(admin_client, "Start After Kill Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    original_cid = member["runtime_container_id"]

    # External kill
    fake_runtime.containers.pop(original_cid, None)

    # Click Start (no prior health check yet — endpoint must handle stale ID itself)
    r = admin_client.post(f"/api/team-members/{member['id']}/runtime/start")
    assert r.status_code == 200
    body = r.json()
    assert body["runtime_status"] == "running"
    assert body["runtime_container_id"] != original_cid
    assert body["runtime_container_id"] in fake_runtime.containers


def test_exited_container_flips_to_stopped(admin_client, fake_runtime):
    """Container still exists but isn't running (status: exited)."""
    import app
    p = _new_project(admin_client, "Health Exited Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    cid = member["runtime_container_id"]

    # Simulate container crash
    fake_runtime.containers[cid]["status"] = "exited"

    asyncio.run(app.check_member_health(member["id"]))

    out = admin_client.get(f"/api/projects/{p['id']}/members").json()
    rec = next(m for m in out if m["id"] == member["id"])
    assert rec["runtime_status"] == "stopped"
    assert "exited" in (rec["runtime_error"] or "")


def test_healthy_container_stays_running(admin_client, fake_runtime):
    """Health check on a healthy container is a no-op."""
    import app
    p = _new_project(admin_client, "Health Healthy Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])

    asyncio.run(app.check_member_health(member["id"]))

    out = admin_client.get(f"/api/projects/{p['id']}/members").json()
    rec = next(m for m in out if m["id"] == member["id"])
    assert rec["runtime_status"] == "running"
    assert rec["runtime_error"] is None


def test_check_all_only_touches_running_or_starting(admin_client, fake_runtime):
    """Members with runtime_status='stopped' or 'not_provisioned' should be skipped."""
    import app
    p = _new_project(admin_client, "Health Skip Project")
    _configure_runtime(admin_client, p["id"])
    # Healthy AI
    healthy = _create_ai_member(admin_client, p["id"], name="Healthy AI")
    # AI with no container
    unprov = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Unprov", "type": "ai_agent"},
    ).json()
    # Human
    admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Joe", "type": "human"},
    )
    asyncio.run(app.check_all_member_health())

    out = {m["id"]: m for m in admin_client.get(f"/api/projects/{p['id']}/members").json()}
    assert out[healthy["id"]]["runtime_status"] == "running"
    assert out[unprov["id"]]["runtime_status"] == "not_provisioned"


def test_admin_endpoint_triggers_health_check(admin_client, fake_runtime):
    p = _new_project(admin_client, "Health Admin Trigger")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    # Kill it
    fake_runtime.containers.pop(member["runtime_container_id"], None)

    r = admin_client.post("/api/admin/check-runtime-health")
    assert r.status_code == 200
    assert r.json()["checked"] >= 1

    out = admin_client.get(f"/api/projects/{p['id']}/members").json()
    rec = next(m for m in out if m["id"] == member["id"])
    assert rec["runtime_status"] == "not_provisioned"
    assert rec["runtime_container_id"] is None
