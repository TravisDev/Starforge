"""
DockerRuntime: RuntimeAdapter backed by a real Docker daemon.

Implements the lifecycle by talking to Docker via the `docker` SDK. The SDK is
synchronous, so we wrap blocking calls in run_in_executor.

Container naming convention: starforge-<project_slug>-member-<member_id>.
This makes orphan cleanup straightforward — if a container with the expected
name already exists at provision time, we tear it down before creating fresh.

Container port: 8080 inside, mapped to an ephemeral host port. Starforge
resolves the host port via `inspect` and stores the endpoint as
http://localhost:<host_port> in team_members.runtime_endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from runtime_adapter import InspectResult, ProvisionResult, RuntimeAdapter

log = logging.getLogger(__name__)


def _container_name(project_slug: str, member_id: int) -> str:
    return f"starforge-{project_slug}-member-{member_id}"


def _normalize_memory(s: Optional[str]) -> Optional[str]:
    """Accept Kubernetes-style (2Gi, 512Mi) or Docker-style (2g, 512m) units.
    Docker SDK requires lowercase b/k/m/g without the 'i' suffix."""
    if not s:
        return None
    s = s.strip()
    mapping = {"Gi": "g", "Mi": "m", "Ki": "k", "Ti": "t",
               "G": "g", "M": "m", "K": "k", "T": "t"}
    for k, v in mapping.items():
        if s.endswith(k):
            return s[: -len(k)] + v
    return s.lower()  # already e.g. "2g"


class DockerRuntime(RuntimeAdapter):
    def __init__(self, project_config: dict[str, Any]) -> None:
        # Lazy-import so unit tests don't need the docker SDK installed.
        import docker  # type: ignore
        self._docker = docker
        base_url = project_config.get("docker_host") or None
        self.client = docker.DockerClient(base_url=base_url) if base_url else docker.from_env()
        self.cfg = project_config

    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def pull_image(self, image: str) -> str:
        log.info("pulling image %s", image)
        img = await self._run(self.client.images.pull, image)
        return getattr(img, "id", "") or ""

    async def get_registry_digest(self, image: str) -> Optional[str]:
        """Manifest-only lookup against the source registry. Doesn't pull layers."""
        try:
            data = await self._run(self.client.images.get_registry_data, image)
            return getattr(data, "id", None)
        except Exception as e:  # noqa: BLE001
            log.warning("get_registry_digest(%s) failed: %s", image, e)
            return None

    async def provision(
        self,
        *,
        member_id: int,
        project_slug: str,
        snapshot: dict[str, Any],
        config: dict[str, Any],
        secrets: Optional[dict[str, Any]] = None,
    ) -> ProvisionResult:
        image = config.get("image")
        if not image:
            raise RuntimeError("runtime_config.image is not set")
        pull_policy = config.get("image_pull_policy", "if_not_present")

        # Decide whether to pull
        digest = ""
        try:
            existing_img = await self._run(self.client.images.get, image)
            existing_digest = getattr(existing_img, "id", "") or ""
        except self._docker.errors.ImageNotFound:
            existing_digest = ""

        if pull_policy == "always" or (pull_policy == "if_not_present" and not existing_digest):
            digest = await self.pull_image(image)
        else:
            digest = existing_digest

        # Tear down any orphan with the same name (e.g., stale from prior run)
        name = _container_name(project_slug, member_id)
        try:
            orphan = await self._run(self.client.containers.get, name)
            log.info("removing orphan container %s", name)
            await self._run(orphan.remove, force=True)
        except self._docker.errors.NotFound:
            pass

        # Compose run kwargs
        env = {"AGENT_SNAPSHOT_JSON": json.dumps(snapshot)}
        # Secrets → env (Anthropic key, callback token)
        if secrets:
            if secrets.get("anthropic_api_key"):
                env["ANTHROPIC_API_KEY"] = secrets["anthropic_api_key"]
            if secrets.get("callback_token"):
                env["STARFORGE_CALLBACK_TOKEN"] = secrets["callback_token"]
        # Non-secret runtime knobs the container needs
        if config.get("starforge_callback_url"):
            env["STARFORGE_CALLBACK_URL"] = config["starforge_callback_url"]
        env.update(config.get("extra_env") or {})

        kwargs: dict[str, Any] = dict(
            name=name,
            environment=env,
            ports={"8080/tcp": None},          # ephemeral host port
            detach=True,
            restart_policy={"Name": "unless-stopped"},
        )
        network = config.get("network")
        if network:
            kwargs["network"] = network
        mem_limit = _normalize_memory(config.get("memory_limit"))
        if mem_limit:
            kwargs["mem_limit"] = mem_limit
        cpu_limit = config.get("cpu_limit")
        if cpu_limit:
            try:
                kwargs["nano_cpus"] = int(float(cpu_limit) * 1e9)
            except (TypeError, ValueError):
                pass

        container = await self._run(self.client.containers.run, image, **kwargs)
        await self._run(container.reload)

        # Resolve mapped host port
        endpoint: Optional[str] = None
        ports = (container.attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
        binding = (ports.get("8080/tcp") or [None])[0]
        if binding:
            host_port = binding.get("HostPort")
            if host_port:
                endpoint = f"http://localhost:{host_port}"

        return ProvisionResult(
            container_id=container.id,
            endpoint=endpoint,
            image_digest=digest or getattr(container.image, "id", ""),
        )

    async def stop(self, container_id: str) -> None:
        try:
            c = await self._run(self.client.containers.get, container_id)
            await self._run(c.stop)
        except self._docker.errors.NotFound:
            pass

    async def start(self, container_id: str) -> None:
        try:
            c = await self._run(self.client.containers.get, container_id)
            await self._run(c.start)
        except self._docker.errors.NotFound:
            pass

    async def remove(self, container_id: str) -> None:
        try:
            c = await self._run(self.client.containers.get, container_id)
            await self._run(c.remove, force=True)
        except self._docker.errors.NotFound:
            pass

    async def inspect(self, container_id: str) -> Optional[InspectResult]:
        try:
            c = await self._run(self.client.containers.get, container_id)
        except self._docker.errors.NotFound:
            return None
        ports = (c.attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
        binding = (ports.get("8080/tcp") or [None])[0]
        endpoint = None
        if binding and binding.get("HostPort"):
            endpoint = f"http://localhost:{binding['HostPort']}"
        return InspectResult(
            status=c.status,
            image_digest=getattr(c.image, "id", "") or "",
            endpoint=endpoint,
        )
