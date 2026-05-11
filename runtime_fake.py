"""
FakeRuntime: in-memory RuntimeAdapter implementation for tests.

Tracks every call in self.calls so tests can assert on lifecycle ordering.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from runtime_adapter import InspectResult, ProvisionResult, RuntimeAdapter


class FakeRuntime(RuntimeAdapter):
    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.image_digests: dict[str, str] = {}
        self.calls: list[tuple[str, Any]] = []
        self._next_id = 1
        self._pull_counter = 0

    def reset(self) -> None:
        self.containers.clear()
        self.image_digests.clear()
        self.calls.clear()
        self._next_id = 1
        self._pull_counter = 0

    async def pull_image(self, image: str) -> str:
        self.calls.append(("pull_image", image))
        self._pull_counter += 1
        # Each pull bumps the digest so tests can simulate image updates.
        digest = f"sha256:fake-{image}-pull{self._pull_counter}"
        self.image_digests[image] = digest
        return digest

    async def provision(
        self,
        *,
        member_id: int,
        project_slug: str,
        snapshot: dict[str, Any],
        config: dict[str, Any],
    ) -> ProvisionResult:
        self.calls.append(("provision", member_id))
        image = config.get("image", "unknown:latest")
        digest = await self.pull_image(image)
        cid = f"fake-cid-{self._next_id}"
        self._next_id += 1
        self.containers[cid] = {
            "id": cid,
            "status": "running",
            "image": image,
            "image_digest": digest,
            "endpoint": f"http://fake-{project_slug}-member-{member_id}:8080",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_agent_type": snapshot.get("agent_type"),
        }
        return ProvisionResult(
            container_id=cid,
            endpoint=self.containers[cid]["endpoint"],
            image_digest=digest,
        )

    async def stop(self, container_id: str) -> None:
        self.calls.append(("stop", container_id))
        if container_id in self.containers:
            self.containers[container_id]["status"] = "stopped"

    async def start(self, container_id: str) -> None:
        self.calls.append(("start", container_id))
        if container_id in self.containers:
            self.containers[container_id]["status"] = "running"

    async def remove(self, container_id: str) -> None:
        self.calls.append(("remove", container_id))
        self.containers.pop(container_id, None)

    async def inspect(self, container_id: str) -> Optional[InspectResult]:
        c = self.containers.get(container_id)
        if not c:
            return None
        return InspectResult(
            status=c["status"],
            image_digest=c["image_digest"],
            endpoint=c["endpoint"],
        )
