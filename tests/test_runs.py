"""
Tests for Phase C.1 — agent run dispatch and callback.

Covers:
- Project runtime secrets: set / status / regenerate callback token
- Provisioning passes secrets through to the runtime adapter (env vars)
- POST /api/team-members/{id}/runs creates a queued run and dispatches it
- Dispatch failure marks the run failed
- Callback endpoint requires the right bearer token
- Callback updates run status + records token/cost
- Late callback on a terminal run is a no-op
- Listing + getting runs
- Cancel
"""

from __future__ import annotations

import asyncio


def _new_project(admin_client, name: str) -> dict:
    r = admin_client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _configure_runtime(admin_client, pid: int, callback_url: str = "http://host.docker.internal:8000") -> None:
    r = admin_client.put(
        f"/api/projects/{pid}/runtime-config",
        json={
            "type": "docker",
            "image": "starforge-nemoclaw:dev",
            "docker_host": "unix:///var/run/docker.sock",
            "image_pull_policy": "if_not_present",
            "starforge_callback_url": callback_url,
        },
    )
    assert r.status_code == 200, r.text


def _create_ai_member(admin_client, pid: int, name: str = "AI") -> dict:
    r = admin_client.post(
        f"/api/projects/{pid}/members",
        json={"name": name, "type": "ai_agent", "agent_type": "network-engineer"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------- runtime secrets ----------

def test_secrets_status_empty_by_default(admin_client):
    p = _new_project(admin_client, "Secrets Empty Project")
    r = admin_client.get(f"/api/projects/{p['id']}/runtime-secrets/status")
    assert r.status_code == 200
    assert r.json() == {"anthropic_api_key_set": False, "callback_token_set": False}


def test_set_and_status_runtime_secrets(admin_client):
    p = _new_project(admin_client, "Secrets Set Project")
    r = admin_client.put(
        f"/api/projects/{p['id']}/runtime-secrets",
        json={"anthropic_api_key": "sk-ant-test-fake"},
    )
    assert r.status_code == 200
    status = r.json()
    assert status["anthropic_api_key_set"] is True
    # Status endpoint never returns the secret value itself
    assert "anthropic_api_key" not in status


def test_secrets_never_appear_in_project_responses(admin_client):
    p = _new_project(admin_client, "Secrets Hidden Project")
    admin_client.put(f"/api/projects/{p['id']}/runtime-secrets",
                      json={"anthropic_api_key": "sk-ant-do-not-leak"})
    listing = admin_client.get("/api/projects").json()
    target = next(x for x in listing if x["id"] == p["id"])
    # No raw bytes blob and no decrypted leak
    assert "runtime_secrets_enc" not in target
    serialized = str(target)
    assert "sk-ant-do-not-leak" not in serialized


def test_regenerate_callback_token(admin_client):
    p = _new_project(admin_client, "Regen Token Project")
    r1 = admin_client.post(
        f"/api/projects/{p['id']}/runtime-secrets/regenerate-callback-token")
    assert r1.status_code == 200
    s1 = admin_client.get(f"/api/projects/{p['id']}/runtime-secrets/status").json()
    assert s1["callback_token_set"] is True


# ---------- secrets reach the adapter ----------

def test_provision_passes_secrets_to_adapter(admin_client, fake_runtime):
    p = _new_project(admin_client, "Provision With Secrets")
    _configure_runtime(admin_client, p["id"])
    admin_client.put(f"/api/projects/{p['id']}/runtime-secrets",
                      json={"anthropic_api_key": "sk-ant-fake-1"})
    member = _create_ai_member(admin_client, p["id"])
    cid = member["runtime_container_id"]
    container = fake_runtime.containers[cid]
    seen = container.get("secrets_seen", {})
    assert seen.get("anthropic_api_key") == "sk-ant-fake-1"
    # callback_token was auto-generated during provision
    assert seen.get("callback_token")


def test_provision_passes_member_identity_as_env(admin_client, fake_runtime):
    """Member name + description should reach the container as env vars so the
    agent can incorporate them into its system prompt as personality."""
    p = _new_project(admin_client, "Member Identity Env")
    _configure_runtime(admin_client, p["id"])
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={
            "name": "Snarky Beep",
            "type": "ai_agent",
            "agent_type": "network-engineer",
            "description": "Casual, dry sense of humor. Don't be formal.",
        },
    )
    assert r.status_code == 201, r.text
    member = r.json()
    env = fake_runtime.containers[member["runtime_container_id"]]["extra_env_seen"]
    assert env.get("STARFORGE_MEMBER_NAME") == "Snarky Beep"
    assert "Casual, dry" in (env.get("STARFORGE_MEMBER_DESCRIPTION") or "")


# ---------- run dispatch ----------

class _RecordingDispatcher:
    """Stand-in for the outbound HTTP call to nemoclaw. Captures invocations."""
    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, endpoint, payload, token):
        self.calls.append({"endpoint": endpoint, "payload": payload, "token": token})
        return {"ok": True}


def _setup_invocable_member(admin_client, fake_runtime, project_name="Run Project"):
    p = _new_project(admin_client, project_name)
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    return p, member


def test_create_run_returns_queued_record(admin_client, fake_runtime):
    import app
    _, member = _setup_invocable_member(admin_client, fake_runtime, "Run Queued")
    app._invoke_http_override = _RecordingDispatcher()
    try:
        r = admin_client.post(
            f"/api/team-members/{member['id']}/runs",
            json={"inputs": {"incident_id": "INC-42"}},
        )
        assert r.status_code == 201, r.text
        run = r.json()
        assert run["status"] in {"queued", "running"}
        assert run["member_id"] == member["id"]
        assert run["inputs"] == {"incident_id": "INC-42"}
        assert run["id"]  # UUID
    finally:
        app._invoke_http_override = None


def test_dispatch_sends_callback_url_and_token(admin_client, fake_runtime):
    import app
    p, member = _setup_invocable_member(admin_client, fake_runtime, "Run Dispatch")
    dispatcher = _RecordingDispatcher()
    app._invoke_http_override = dispatcher
    try:
        r = admin_client.post(
            f"/api/team-members/{member['id']}/runs",
            json={"inputs": {"x": 1}},
        )
        assert r.status_code == 201
        run_id = r.json()["id"]
        # Let the asyncio dispatch task run
        asyncio.run(asyncio.sleep(0.05))
        assert len(dispatcher.calls) == 1
        call = dispatcher.calls[0]
        assert call["payload"]["run_id"] == run_id
        assert call["payload"]["inputs"] == {"x": 1}
        assert call["payload"]["callback_url"].startswith("http://")
        assert call["payload"]["callback_token"]
        assert call["token"] == call["payload"]["callback_token"]
    finally:
        app._invoke_http_override = None


def test_dispatch_failure_marks_run_failed(admin_client, fake_runtime):
    import app

    async def boom(endpoint, payload, token):
        raise RuntimeError("nemoclaw unreachable")

    p, member = _setup_invocable_member(admin_client, fake_runtime, "Dispatch Fail")
    app._invoke_http_override = boom
    try:
        r = admin_client.post(
            f"/api/team-members/{member['id']}/runs", json={"inputs": {}})
        run_id = r.json()["id"]
        asyncio.run(asyncio.sleep(0.05))
        got = admin_client.get(f"/api/agent-runs/{run_id}").json()
        assert got["status"] == "failed"
        assert "nemoclaw unreachable" in (got["error"] or "")
    finally:
        app._invoke_http_override = None


def test_create_run_rejects_when_not_running(admin_client, fake_runtime):
    p = _new_project(admin_client, "Not Running Project")
    _configure_runtime(admin_client, p["id"])
    # Create without runtime config first so it stays not_provisioned
    r = admin_client.post(
        f"/api/projects/{p['id']}/members",
        json={"name": "Idle", "type": "ai_agent"},  # no agent_type → no provision
    )
    mid = r.json()["id"]
    rr = admin_client.post(f"/api/team-members/{mid}/runs", json={"inputs": {}})
    assert rr.status_code == 400


def test_create_run_requires_callback_url(admin_client, fake_runtime):
    """Without starforge_callback_url, nemoclaw can't report back — reject early."""
    p = _new_project(admin_client, "No Callback URL Project")
    # Configure runtime but WITHOUT callback_url
    admin_client.put(
        f"/api/projects/{p['id']}/runtime-config",
        json={
            "type": "docker", "image": "starforge-nemoclaw:dev",
            "image_pull_policy": "if_not_present",
        },
    )
    member = _create_ai_member(admin_client, p["id"])
    r = admin_client.post(
        f"/api/team-members/{member['id']}/runs", json={"inputs": {}})
    assert r.status_code == 400
    assert "callback_url" in r.json()["detail"].lower()


# ---------- callback endpoint ----------

def _make_run(admin_client, fake_runtime, project_name) -> tuple[dict, str, str]:
    import app
    app._invoke_http_override = _RecordingDispatcher()
    try:
        p, member = _setup_invocable_member(admin_client, fake_runtime, project_name)
        r = admin_client.post(
            f"/api/team-members/{member['id']}/runs", json={"inputs": {}})
        assert r.status_code == 201
        run = r.json()
        asyncio.run(asyncio.sleep(0.05))
        token_status = fake_runtime.containers[member["runtime_container_id"]]["secrets_seen"]
        return run, token_status["callback_token"], member["id"]
    finally:
        app._invoke_http_override = None


def test_callback_succeeds_with_valid_token(admin_client, fake_runtime):
    run, token, _ = _make_run(admin_client, fake_runtime, "Callback OK Project")
    r = admin_client.post(
        f"/api/agent-runs/{run['id']}/result",
        json={"status": "succeeded", "output": "hello", "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.01},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    got = admin_client.get(f"/api/agent-runs/{run['id']}").json()
    assert got["status"] == "succeeded"
    assert got["output"] == "hello"
    assert got["tokens_in"] == 100
    assert got["tokens_out"] == 50
    assert got["cost_usd"] == 0.01


def test_callback_rejects_missing_token(admin_client, fake_runtime):
    run, _, _ = _make_run(admin_client, fake_runtime, "Callback NoToken Project")
    r = admin_client.post(
        f"/api/agent-runs/{run['id']}/result",
        json={"status": "succeeded", "output": "nope"},
    )
    assert r.status_code == 401


def test_callback_rejects_wrong_token(admin_client, fake_runtime):
    run, _, _ = _make_run(admin_client, fake_runtime, "Callback WrongToken Project")
    r = admin_client.post(
        f"/api/agent-runs/{run['id']}/result",
        json={"status": "succeeded", "output": "nope"},
        headers={"Authorization": "Bearer not-the-right-token"},
    )
    assert r.status_code == 401


def test_late_callback_on_terminal_run_is_noop(admin_client, fake_runtime):
    run, token, _ = _make_run(admin_client, fake_runtime, "Callback Late Project")
    # First callback
    admin_client.post(
        f"/api/agent-runs/{run['id']}/result",
        json={"status": "succeeded", "output": "first"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Late one with different content
    r = admin_client.post(
        f"/api/agent-runs/{run['id']}/result",
        json={"status": "failed", "error": "stale"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json().get("ignored") is True
    got = admin_client.get(f"/api/agent-runs/{run['id']}").json()
    assert got["status"] == "succeeded"
    assert got["output"] == "first"


# ---------- list + cancel ----------

def test_list_runs_for_member(admin_client, fake_runtime):
    import app
    app._invoke_http_override = _RecordingDispatcher()
    try:
        p, member = _setup_invocable_member(admin_client, fake_runtime, "List Runs Project")
        for i in range(3):
            admin_client.post(
                f"/api/team-members/{member['id']}/runs",
                json={"inputs": {"i": i}},
            )
        r = admin_client.get(f"/api/team-members/{member['id']}/runs")
        assert r.status_code == 200
        runs = r.json()
        assert len(runs) >= 3
    finally:
        app._invoke_http_override = None


def test_cancel_run(admin_client, fake_runtime):
    run, _, _ = _make_run(admin_client, fake_runtime, "Cancel Project")
    r = admin_client.post(f"/api/agent-runs/{run['id']}/cancel")
    assert r.status_code == 200
    got = admin_client.get(f"/api/agent-runs/{run['id']}").json()
    assert got["status"] == "cancelled"
