"""
Tests for the C.1.5 + C.5 chunk:
- Task comments: user CRUD + agent write-back via callback token
- Assignment trigger: when an AI member is assigned a todo task, fire a run
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
        json={
            "type": "docker", "image": "starforge-nemoclaw:dev",
            "image_pull_policy": "if_not_present",
            "starforge_callback_url": "http://host.docker.internal:8000",
        },
    )


def _create_ai_member(admin_client, pid: int, name="AI") -> dict:
    r = admin_client.post(
        f"/api/projects/{pid}/members",
        json={"name": name, "type": "ai_agent", "agent_type": "network-engineer"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _new_task(admin_client, pid: int, title: str, **extra) -> dict:
    r = admin_client.post("/tasks", json={"project_id": pid, "title": title, **extra})
    assert r.status_code == 201, r.text
    return r.json()


# ---------- comments by users ----------

def test_user_comment_create_and_list(admin_client):
    p = _new_project(admin_client, "Comments User Project")
    t = _new_task(admin_client, p["id"], "Comment me")
    r = admin_client.post(f"/api/tasks/{t['id']}/comments", json={"body": "first!"})
    assert r.status_code == 201
    body = r.json()
    assert body["author_kind"] == "user"
    assert body["body"] == "first!"

    lst = admin_client.get(f"/api/tasks/{t['id']}/comments").json()
    assert len(lst) == 1
    assert lst[0]["body"] == "first!"


def test_comments_404_on_missing_task(admin_client):
    r = admin_client.get("/api/tasks/999999/comments")
    assert r.status_code == 404


# ---------- agent task-actions ----------

def _setup_member_with_token(admin_client, fake_runtime, project_name):
    """Returns (member, callback_token)."""
    p = _new_project(admin_client, project_name)
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    token = fake_runtime.containers[member["runtime_container_id"]]["secrets_seen"]["callback_token"]
    return p, member, token


def test_agent_set_status_via_task_action(admin_client, fake_runtime):
    p, member, token = _setup_member_with_token(admin_client, fake_runtime, "Agent Set Status")
    t = _new_task(admin_client, p["id"], "do this")
    r = admin_client.post(
        "/api/agents/task-actions",
        json={
            "agent_member_id": member["id"],
            "task_id": t["id"],
            "actions": [{"type": "set_status", "status": "in_progress"}],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    # Verify status flipped
    updated = admin_client.get(f"/tasks/{t['id']}").json()
    assert updated["status"] == "in_progress"


def test_agent_post_comment_via_task_action(admin_client, fake_runtime):
    p, member, token = _setup_member_with_token(admin_client, fake_runtime, "Agent Comment")
    t = _new_task(admin_client, p["id"], "investigate")
    r = admin_client.post(
        "/api/agents/task-actions",
        json={
            "agent_member_id": member["id"],
            "task_id": t["id"],
            "actions": [{"type": "comment", "body": "GET http://x returned 200"}],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    lst = admin_client.get(f"/api/tasks/{t['id']}/comments").json()
    assert len(lst) == 1
    assert lst[0]["author_kind"] == "agent"
    assert "GET http://x returned 200" in lst[0]["body"]


def test_agent_action_rejects_wrong_token(admin_client, fake_runtime):
    p, member, _ = _setup_member_with_token(admin_client, fake_runtime, "Agent Wrong Token")
    t = _new_task(admin_client, p["id"], "x")
    r = admin_client.post(
        "/api/agents/task-actions",
        json={"agent_member_id": member["id"], "task_id": t["id"],
              "actions": [{"type": "comment", "body": "no"}]},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_agent_action_rejects_cross_project_task(admin_client, fake_runtime):
    """An agent can't write to a task in a different project, even with the right token."""
    p1, member, token = _setup_member_with_token(admin_client, fake_runtime, "Agent CrossA")
    p2 = _new_project(admin_client, "Agent CrossB")
    t = _new_task(admin_client, p2["id"], "in other project")
    r = admin_client.post(
        "/api/agents/task-actions",
        json={"agent_member_id": member["id"], "task_id": t["id"],
              "actions": [{"type": "comment", "body": "trespass"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


# ---------- assignment trigger ----------

class _RecordingDispatcher:
    def __init__(self):
        self.calls: list[dict] = []
    async def __call__(self, endpoint, payload, token):
        self.calls.append({"endpoint": endpoint, "payload": payload, "token": token})
        return {"ok": True}


def test_assignment_at_create_fires_run(admin_client, fake_runtime):
    import app
    p = _new_project(admin_client, "Trigger At Create")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    app._invoke_http_override = _RecordingDispatcher()
    try:
        r = admin_client.post("/tasks", json={
            "project_id": p["id"], "title": "auto-assigned",
            "assignee_id": member["id"], "description": "do thing",
        })
        assert r.status_code == 201
        asyncio.run(asyncio.sleep(0.05))
        runs = admin_client.get(f"/api/team-members/{member['id']}/runs").json()
        # One auto-triggered run with task_id in inputs
        triggered = [run for run in runs if run["inputs"].get("task_id") == r.json()["id"]]
        assert len(triggered) == 1
        assert triggered[0]["inputs"]["task_title"] == "auto-assigned"
        assert triggered[0]["inputs"]["task_description"] == "do thing"
    finally:
        app._invoke_http_override = None


def test_assignment_at_patch_fires_run(admin_client, fake_runtime):
    import app
    p = _new_project(admin_client, "Trigger At Patch")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    # Create unassigned
    t = _new_task(admin_client, p["id"], "do later")
    app._invoke_http_override = _RecordingDispatcher()
    try:
        r = admin_client.patch(f"/tasks/{t['id']}", json={"assignee_id": member["id"]})
        assert r.status_code == 200
        asyncio.run(asyncio.sleep(0.05))
        runs = admin_client.get(f"/api/team-members/{member['id']}/runs").json()
        triggered = [run for run in runs if run["inputs"].get("task_id") == t["id"]]
        assert len(triggered) == 1
    finally:
        app._invoke_http_override = None


def test_assignment_trigger_skips_human_assignee(admin_client, fake_runtime):
    import app
    p = _new_project(admin_client, "Trigger Skip Human")
    _configure_runtime(admin_client, p["id"])
    # Create a human member
    hr = admin_client.post(f"/api/projects/{p['id']}/members",
                            json={"name": "Joe", "type": "human"})
    human_id = hr.json()["id"]
    app._invoke_http_override = _RecordingDispatcher()
    try:
        admin_client.post("/tasks", json={
            "project_id": p["id"], "title": "human task",
            "assignee_id": human_id,
        })
        asyncio.run(asyncio.sleep(0.05))
        assert app._invoke_http_override.calls == []
    finally:
        app._invoke_http_override = None


def test_retry_includes_prior_comments_in_dispatch_inputs(admin_client, fake_runtime):
    """When the trigger fires, the dispatch should include any existing comments
    so the agent has rejection / re-try context. Set up the task unassigned
    first, post a comment, then assign — to avoid racing with the create-time
    trigger."""
    import app
    p = _new_project(admin_client, "Re-try With Context")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    t = _new_task(admin_client, p["id"], "needs re-investigation")
    # Drop a comment before assignment so the trigger picks it up
    admin_client.post(f"/api/tasks/{t['id']}/comments",
                       json={"body": "Reviewer: please verify with curl, not ping"})
    dispatcher = _RecordingDispatcher()
    app._invoke_http_override = dispatcher
    try:
        admin_client.patch(f"/tasks/{t['id']}", json={"assignee_id": member["id"]})
        asyncio.run(asyncio.sleep(0.1))
        relevant = [c for c in dispatcher.calls
                     if c["payload"]["inputs"].get("task_id") == t["id"]]
        assert relevant, "expected at least one dispatch for the assigned task"
        comments = relevant[-1]["payload"]["inputs"].get("prior_comments") or []
        assert any("verify with curl" in c["body"] for c in comments)
        assert any(c["author_kind"] == "user" for c in comments)
    finally:
        app._invoke_http_override = None


def test_moving_back_to_todo_triggers_run(admin_client, fake_runtime):
    """Dragging a task back into the todo column should re-notify the assigned agent."""
    import app
    p = _new_project(admin_client, "Back-To-Todo Trigger")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    # Create task already in done state, assigned to the agent
    t = _new_task(admin_client, p["id"], "re-open me", assignee_id=member["id"], status="done")
    app._invoke_http_override = _RecordingDispatcher()
    try:
        r = admin_client.patch(f"/tasks/{t['id']}", json={"status": "todo"})
        assert r.status_code == 200
        asyncio.run(asyncio.sleep(0.05))
        triggered = [run for run in admin_client.get(f"/api/team-members/{member['id']}/runs").json()
                      if run["inputs"].get("task_id") == t["id"]]
        assert len(triggered) == 1, f"expected one run triggered by status→todo, got {triggered}"
    finally:
        app._invoke_http_override = None


def test_user_comment_with_mention_triggers_qa_reply(admin_client, fake_runtime):
    """User posts @mention → agent gets dispatched in comment_reply mode."""
    import app
    p = _new_project(admin_client, "Mention Reply Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"], name="Beep Boop 42")
    t = _new_task(admin_client, p["id"], "discuss me")
    dispatcher = _RecordingDispatcher()
    app._invoke_http_override = dispatcher
    try:
        # By slugified name
        admin_client.post(f"/api/tasks/{t['id']}/comments",
                          json={"body": "@beep-boop-42 can you check the BGP table?"})
        asyncio.run(asyncio.sleep(0.1))
        relevant = [c for c in dispatcher.calls
                    if c["payload"]["inputs"].get("task_id") == t["id"]
                    and c["payload"]["inputs"].get("mode") == "comment_reply"]
        assert relevant, "expected comment_reply dispatch"
        payload = relevant[-1]["payload"]
        assert payload["inputs"]["triggering_comment"]["body"].startswith("@beep-boop-42")
    finally:
        app._invoke_http_override = None


def test_mention_via_agent_type_slug_also_triggers(admin_client, fake_runtime):
    import app
    p = _new_project(admin_client, "Mention By Type Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"], name="Net Eng")
    t = _new_task(admin_client, p["id"], "talk")
    dispatcher = _RecordingDispatcher()
    app._invoke_http_override = dispatcher
    try:
        # By agent_type (network-engineer)
        admin_client.post(f"/api/tasks/{t['id']}/comments",
                          json={"body": "@network-engineer what subnets did you check?"})
        asyncio.run(asyncio.sleep(0.1))
        relevant = [c for c in dispatcher.calls
                    if c["payload"]["inputs"].get("task_id") == t["id"]]
        assert relevant
    finally:
        app._invoke_http_override = None


def test_comment_without_mention_on_unassigned_task_does_not_trigger(admin_client, fake_runtime):
    """Plain comments on tasks with no assignee shouldn't fire anything."""
    import app
    p = _new_project(admin_client, "No Mention Unassigned")
    _configure_runtime(admin_client, p["id"])
    _create_ai_member(admin_client, p["id"])
    t = _new_task(admin_client, p["id"], "silent comment")  # no assignee_id
    dispatcher = _RecordingDispatcher()
    app._invoke_http_override = dispatcher
    try:
        admin_client.post(f"/api/tasks/{t['id']}/comments",
                          json={"body": "just a plain comment, nobody pinged"})
        asyncio.run(asyncio.sleep(0.1))
        assert dispatcher.calls == []
    finally:
        app._invoke_http_override = None


def test_implicit_mention_when_task_assigned_to_ai(admin_client, fake_runtime):
    """A plain comment on a task already assigned to an AI agent should
    implicitly ping that agent — no @mention needed."""
    import app
    p = _new_project(admin_client, "Implicit Mention Project")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    t = _new_task(admin_client, p["id"], "talk", assignee_id=member["id"])
    asyncio.run(asyncio.sleep(0.1))   # let the create-time trigger settle
    dispatcher = _RecordingDispatcher()
    app._invoke_http_override = dispatcher
    try:
        admin_client.post(f"/api/tasks/{t['id']}/comments",
                          json={"body": "are you sure about your finding?"})
        asyncio.run(asyncio.sleep(0.1))
        replies = [c for c in dispatcher.calls
                   if c["payload"]["inputs"].get("mode") == "comment_reply"
                   and c["payload"]["inputs"].get("task_id") == t["id"]]
        assert replies, "expected an implicit comment_reply dispatch"
        assert replies[-1]["payload"]["inputs"]["triggering_comment"]["body"] == \
               "are you sure about your finding?"
    finally:
        app._invoke_http_override = None


def test_implicit_mention_skips_human_assignee(admin_client, fake_runtime):
    """Implicit-mention is AI-only — commenting on a human-assigned task doesn't fire."""
    import app
    p = _new_project(admin_client, "Implicit Mention Human")
    hr = admin_client.post(f"/api/projects/{p['id']}/members",
                            json={"name": "Joe", "type": "human"})
    human_id = hr.json()["id"]
    t = _new_task(admin_client, p["id"], "hello human", assignee_id=human_id)
    dispatcher = _RecordingDispatcher()
    app._invoke_http_override = dispatcher
    try:
        admin_client.post(f"/api/tasks/{t['id']}/comments",
                          json={"body": "hey there"})
        asyncio.run(asyncio.sleep(0.1))
        assert dispatcher.calls == []
    finally:
        app._invoke_http_override = None


def test_unknown_mention_does_not_trigger(admin_client, fake_runtime):
    import app
    p = _new_project(admin_client, "Unknown Mention Project")
    _configure_runtime(admin_client, p["id"])
    _create_ai_member(admin_client, p["id"], name="Beep")
    t = _new_task(admin_client, p["id"], "x")
    dispatcher = _RecordingDispatcher()
    app._invoke_http_override = dispatcher
    try:
        admin_client.post(f"/api/tasks/{t['id']}/comments",
                          json={"body": "@nobody where are you?"})
        asyncio.run(asyncio.sleep(0.1))
        assert dispatcher.calls == []
    finally:
        app._invoke_http_override = None


def test_already_in_todo_no_trigger_on_status_no_op(admin_client, fake_runtime):
    """PATCHing status=todo when the task is already in todo shouldn't re-trigger."""
    import app
    p = _new_project(admin_client, "No-Op Todo Trigger")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    # Task starts in todo. The create-time trigger fires once.
    t = _new_task(admin_client, p["id"], "stay put", assignee_id=member["id"])
    asyncio.run(asyncio.sleep(0.05))
    before = len([run for run in admin_client.get(f"/api/team-members/{member['id']}/runs").json()
                  if run["inputs"].get("task_id") == t["id"]])
    app._invoke_http_override = _RecordingDispatcher()
    try:
        admin_client.patch(f"/tasks/{t['id']}", json={"status": "todo"})
        asyncio.run(asyncio.sleep(0.05))
        after = len([run for run in admin_client.get(f"/api/team-members/{member['id']}/runs").json()
                     if run["inputs"].get("task_id") == t["id"]])
        assert after == before, "no new run should fire when status was already todo"
    finally:
        app._invoke_http_override = None


def test_assignment_trigger_idempotent(admin_client, fake_runtime):
    """Repeated patches assigning the SAME agent shouldn't fire duplicate runs."""
    import app
    p = _new_project(admin_client, "Trigger Idempotent")
    _configure_runtime(admin_client, p["id"])
    member = _create_ai_member(admin_client, p["id"])
    t = _new_task(admin_client, p["id"], "once-only")
    app._invoke_http_override = _RecordingDispatcher()
    try:
        admin_client.patch(f"/tasks/{t['id']}", json={"assignee_id": member["id"]})
        asyncio.run(asyncio.sleep(0.05))
        # Patch the same assignee again — should be a no-op since assignee_id didn't change
        admin_client.patch(f"/tasks/{t['id']}", json={"assignee_id": member["id"]})
        asyncio.run(asyncio.sleep(0.05))
        runs_for_task = [r for r in admin_client.get(f"/api/team-members/{member['id']}/runs").json()
                          if r["inputs"].get("task_id") == t["id"]]
        assert len(runs_for_task) == 1
    finally:
        app._invoke_http_override = None
