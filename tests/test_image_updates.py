"""
Tests for image-update detection (Phase B.2.5).

Covers:
- update_available is False when the running digest matches the registry
- update_available flips to True when the registry advances
- check_image_update_for_member persists the latest digest
- check_all_image_updates iterates running AI members only
- Admin endpoints to read/write the check interval and trigger a manual run
- POST /api/team-members/{id}/check-image-update updates one member on demand
"""

from __future__ import annotations

import asyncio


def _new_project(admin_client, name: str) -> dict:
    r = admin_client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _configure_runtime(admin_client, project_id: int) -> None:
    r = admin_client.put(
        f"/api/projects/{project_id}/runtime-config",
        json={
            "type": "docker",
            "image": "starforge-nemoclaw:dev",
            "docker_host": "unix:///var/run/docker.sock",
            "image_pull_policy": "if_not_present",
        },
    )
    assert r.status_code == 200, r.text


def _create_ai_member(admin_client, project_id: int) -> dict:
    r = admin_client.post(
        f"/api/projects/{project_id}/members",
        json={"name": "AI", "type": "ai_agent", "agent_type": "network-engineer"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------- update_available computed field ----------

def test_freshly_provisioned_has_no_update_available(admin_client, fake_runtime):
    p = _new_project(admin_client, "No Update Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    # On first provision, runtime_image_digest_latest hasn't been polled yet — so
    # update_available is False (we don't claim updates we haven't verified).
    assert member["update_available"] is False


def test_update_available_true_after_registry_advances(admin_client, fake_runtime):
    import app
    p = _new_project(admin_client, "Update Available Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    # Simulate someone pushing a new image to the registry
    fake_runtime.set_registry_digest("starforge-nemoclaw:dev", "sha256:fake-newer-digest")

    # Manually trigger the check (we disabled the background loop in tests)
    asyncio.run(app.check_image_update_for_member(member["id"]))

    r = admin_client.get(f"/api/projects/{p['id']}/members")
    rec = next(m for m in r.json() if m["id"] == member["id"])
    assert rec["runtime_image_digest_latest"] == "sha256:fake-newer-digest"
    assert rec["update_available"] is True


# ---------- check_all_image_updates ----------

def test_check_all_only_touches_running_ai_members(admin_client, fake_runtime):
    """In this test's own project, only the running AI member should get checked.
    We assert by side-effect (digest_latest is set) rather than by total count,
    because earlier tests in the session leave rows with runtime_container_id set."""
    import app
    p = _new_project(admin_client, "Check All Project")
    _configure_runtime(admin_client, p["id"])
    _create_ai_member(admin_client, p["id"])
    admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "H", "type": "human"},
    )
    admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "U", "type": "ai_agent"},  # no agent_type → not provisioned
    )

    fake_runtime.set_registry_digest("starforge-nemoclaw:dev", "sha256:check-all-advance")
    asyncio.run(app.check_all_image_updates())

    members = admin_client.get(f"/api/projects/{p['id']}/members").json()
    by_name = {m["name"]: m for m in members}
    assert by_name["AI"]["runtime_image_digest_latest"] == "sha256:check-all-advance"
    assert by_name["H"]["runtime_image_digest_latest"] is None     # human never checked
    assert by_name["U"]["runtime_image_digest_latest"] is None     # unprovisioned skipped


# ---------- admin: interval setting ----------

def test_default_update_interval(admin_client):
    r = admin_client.get("/api/admin/settings/update-check-interval")
    assert r.status_code == 200
    assert r.json()["seconds"] == 300  # DEFAULT_UPDATE_INTERVAL


def test_set_update_interval_persists(admin_client):
    r = admin_client.put(
        "/api/admin/settings/update-check-interval", json={"seconds": 60}
    )
    assert r.status_code == 200
    assert r.json()["seconds"] == 60
    r2 = admin_client.get("/api/admin/settings/update-check-interval")
    assert r2.json()["seconds"] == 60


def test_set_update_interval_zero_disables(admin_client):
    r = admin_client.put(
        "/api/admin/settings/update-check-interval", json={"seconds": 0}
    )
    assert r.status_code == 200


def test_set_update_interval_rejects_negative(admin_client):
    r = admin_client.put(
        "/api/admin/settings/update-check-interval", json={"seconds": -1}
    )
    assert r.status_code == 422


# ---------- manual trigger endpoints ----------

def test_admin_trigger_image_check_runs_check(admin_client, fake_runtime):
    p = _new_project(admin_client, "Manual Trigger Project")
    _configure_runtime(admin_client, p["id"])
    _create_ai_member(admin_client, p["id"])

    r = admin_client.post("/api/admin/check-image-updates")
    assert r.status_code == 200
    assert r.json()["checked"] >= 1


def test_per_member_check_endpoint_updates_latest(admin_client, fake_runtime):
    p = _new_project(admin_client, "Per Member Check Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    fake_runtime.set_registry_digest("starforge-nemoclaw:dev", "sha256:fake-advance-2")

    r = admin_client.post(f"/api/team-members/{member['id']}/check-image-update")
    assert r.status_code == 200
    body = r.json()
    assert body["runtime_image_digest_latest"] == "sha256:fake-advance-2"
    assert body["update_available"] is True
