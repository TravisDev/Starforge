"""
Runtime adapter abstraction for AI-agent containers.

Starforge spawns one container per AI-agent team member. The container holds
the agent's resolved snapshot and stays running for the member's lifetime —
no cold-start cost per invocation.

This module defines the abstract interface. Concrete implementations:
- runtime_docker.DockerRuntime — real Docker daemon
- runtime_fake.FakeRuntime — in-memory test double

Lifecycle (called by app.py based on team_members events):
- provision: pull image + create + start container
- stop / start: pause without losing the container
- remove: tear down the container completely
- inspect: read current container state
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ProvisionResult:
    container_id: str
    endpoint: Optional[str]
    image_digest: str


@dataclass
class InspectResult:
    status: str          # e.g. "running" | "stopped" | "exited"
    image_digest: str
    endpoint: Optional[str]


class RuntimeAdapter(ABC):
    """Lifecycle operations on the per-member container."""

    @abstractmethod
    async def provision(
        self,
        *,
        member_id: int,
        project_slug: str,
        snapshot: dict[str, Any],
        config: dict[str, Any],
        secrets: Optional[dict[str, Any]] = None,
    ) -> ProvisionResult:
        """Pull image, create + start a container holding `snapshot`.

        `secrets` provides plaintext values (Anthropic key, callback token) that
        the adapter is expected to surface to the container as env vars. The
        caller is responsible for decrypting these before invocation.
        """

    @abstractmethod
    async def stop(self, container_id: str) -> None:
        """Stop without removing (pause use case)."""

    @abstractmethod
    async def start(self, container_id: str) -> None:
        """Start a previously-stopped container."""

    @abstractmethod
    async def remove(self, container_id: str) -> None:
        """Tear down completely."""

    @abstractmethod
    async def inspect(self, container_id: str) -> Optional[InspectResult]:
        """Current state, or None if container is gone."""

    @abstractmethod
    async def pull_image(self, image: str) -> str:
        """Pull (or re-pull) and return the resulting digest."""

    @abstractmethod
    async def get_registry_digest(self, image: str) -> Optional[str]:
        """Look up the current digest in the source registry without pulling layers.

        Returns None if it can't be determined (image not in registry, auth fail,
        registry unreachable). The caller treats None as 'no info, leave alone',
        not 'no update'.
        """
