"""Webhook delivery: fire-and-forget event notifications."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
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
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _check_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, label: str) -> None:
    """Raise ValueError if addr falls within a private/loopback/link-local network."""
    for net in _PRIVATE_NETWORKS:
        if addr in net:
            raise ValueError(
                f"Webhook URL resolves to a private/loopback address "
                f"({label} -> {addr}). This is an SSRF risk. Use a public URL."
            )


def validate_webhook_url(url: str) -> None:
    """
    Validate webhook URL at startup.
    Resolves hostnames to IP addresses and rejects RFC 1918, loopback,
    link-local (169.254/fe80), and non-http(s) schemes.
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

    # Try parsing as IP literal first
    try:
        addr = ipaddress.ip_address(host)
        _check_private(addr, host)
        return
    except ValueError as e:
        if "private" in str(e) or "SSRF" in str(e):
            raise
        # Not an IP literal — resolve hostname below

    # Resolve hostname to IP(s) and check all results
    try:
        results = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"Webhook URL hostname cannot be resolved: {host} ({e})")
    if not results:
        raise ValueError(f"Webhook URL hostname resolved to no addresses: {host}")
    for family, _, _, _, sockaddr in results:
        addr = ipaddress.ip_address(sockaddr[0])
        _check_private(addr, host)


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
