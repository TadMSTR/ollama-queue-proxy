"""Host manager: tracks Ollama host health and model inventory."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .config import OllamaConfig

logger = logging.getLogger(__name__)


@dataclass
class OllamaHost:
    url: str
    name: str
    healthy: bool = True
    models: list[str] = field(default_factory=list)
    last_checked: datetime | None = None
    requests_handled: int = 0
    failures: int = 0


class HostManager:
    def __init__(self, config: OllamaConfig) -> None:
        self._config = config
        self.hosts: list[OllamaHost] = [
            OllamaHost(url=h.url, name=h.name) for h in config.hosts
        ]
        self._check_task: asyncio.Task | None = None

    async def startup_check(self, client: httpx.AsyncClient) -> None:
        """Check all hosts on startup and populate model inventories."""
        for host in self.hosts:
            await self._check_host(host, client)

    async def start_background_checks(self, client: httpx.AsyncClient) -> None:
        self._check_task = asyncio.create_task(self._health_loop(client))

    async def stop(self) -> None:
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

    async def _health_loop(self, client: httpx.AsyncClient) -> None:
        while True:
            await asyncio.sleep(self._config.health_check_interval)
            for host in self.hosts:
                if not host.healthy:
                    await self._check_host(host, client)

    async def _check_host(self, host: OllamaHost, client: httpx.AsyncClient) -> None:
        try:
            resp = await client.get(
                f"{host.url}/api/tags",
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            was_unhealthy = not host.healthy
            host.healthy = True
            host.models = models
            host.last_checked = datetime.now(timezone.utc)
            if was_unhealthy:
                logger.warning("host.recovered name=%s models=%d", host.name, len(models))
            else:
                if models:
                    logger.info("host.ok name=%s models=%s", host.name, models)
                else:
                    logger.warning("host.no_models name=%s", host.name)
        except Exception as e:
            was_healthy = host.healthy
            host.healthy = False
            host.last_checked = datetime.now(timezone.utc)
            if was_healthy:
                logger.warning("host.unhealthy name=%s error=%s", host.name, e)

    def select_host(self, model: str | None) -> OllamaHost | None:
        """Return first healthy host that has the requested model (if specified)."""
        for host in self.hosts:
            if not host.healthy:
                continue
            if model and host.models and model not in host.models:
                continue
            return host
        return None

    def mark_unhealthy(self, host: OllamaHost, error: str) -> None:
        host.healthy = False
        host.failures += 1
        logger.warning("host.failure name=%s error=%s", host.name, error)
