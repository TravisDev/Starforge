"""
Tests for the tool registry — a single ./agents/tools.yaml file in the repo.

Threat model: tools with full process privileges, like OpenClaw skills. Defense:
PR review is the gate. Adding a tool means editing this file, and the merge is
where governance happens. These tests just confirm the runtime reads the
registry correctly.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REGISTRY = Path(__file__).resolve().parent.parent / "agents" / "tools.yaml"


def test_registry_file_exists():
    assert REGISTRY.exists(), "agents/tools.yaml is the canonical tool registry"


def test_registry_yaml_is_valid():
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    assert data is not None
    assert "tools" in data
    assert isinstance(data["tools"], list)
    assert len(data["tools"]) > 0


def test_built_in_tools_are_listed_via_api(admin_client):
    r = admin_client.get("/api/tools")
    assert r.status_code == 200
    slugs = {t["slug"] for t in r.json()}
    for expected in {"http_get", "set_task_status", "add_comment", "create_agent_type", "finish"}:
        assert expected in slugs, f"missing built-in tool: {expected}"


def test_tools_unauthenticated_returns_401(admin_client):
    """Like other API surfaces — needs a session."""
    fresh = admin_client.__class__(admin_client.app)
    r = fresh.get("/api/tools")
    assert r.status_code == 401


def test_each_tool_has_declared_capabilities(admin_client):
    """Every tool must declare its capabilities — even if empty — so a
    reviewer can see at a glance what the tool can reach."""
    tools = admin_client.get("/api/tools").json()
    for t in tools:
        assert "capabilities" in t, f"{t['slug']} missing capabilities"
        caps = t["capabilities"]
        assert "network" in caps
        assert "filesystem" in caps
        assert "env_vars" in caps


def test_each_tool_has_required_fields(admin_client):
    tools = admin_client.get("/api/tools").json()
    for t in tools:
        assert t.get("slug")
        assert t.get("name")
        assert t.get("description")
