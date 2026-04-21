"""Webhook delivery: fire-and-forget event notifications."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .config import WebhookConfig

logger = logging.getLogger(__name__)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def validate_webhook_url(url: str) -> None:
    """
    Validate webhook URL at startup.
    Rejects RFC 1918 addresses, loopback, and non-http(s) schemes.
    Raises ValueError with a descriptive message if invalid.
    """
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Webhook URL must use http or https scheme, got: {parsed.scheme!r}"
        )
    host = parsed.hostname
    if not host:
        raise ValueError("Webhook URL has no hostname")
    try:
        addr = ipaddress.ip_address(host)
        for net in _PRIVATE_NETWORKS:
            if addr in net:
                raise ValueError(
                    f"Webhook URL points to a private/loopback address ({host}). "
                    "This is an SSRF risk. Use a public URL."
                )
    except ValueError as e:
        # ip_address() raises ValueError for hostnames — that's fine, pass through
        if "private" in str(e) or "SSRF" in str(e):
            raise


class WebhookManager:
    def __init__(self, config: WebhookConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client

    async def fire(self, event: str, tier: str | None = None, **kwargs) -> None:
        if not self._config.enabled:
            return
        if event not in self._config.events:
            return
        asyncio.create_task(self._deliver(event, tier, **kwargs))

    async def _deliver(self, event: str, tier: str | None, **kwargs) -> None:
        payload = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if tier:
            payload["tier"] = tier
        payload.update(kwargs)
        try:
            await self._client.post(
                self._config.url,
                json=payload,
                timeout=5.0,
            )
        except Exception as e:
            logger.warning("webhook.delivery_failed event=%s error=%s", event, e)
