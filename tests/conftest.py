"""Shared fixtures for ollama-queue-proxy tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ollama_queue_proxy.config import (
    ApiKeyConfig,
    AuthConfig,
    Config,
    HostConfig,
    OllamaConfig,
    ProxyConfig,
    QueueConfig,
    RateLimitConfig,
    TierConfig,
    WebhookConfig,
    LoggingConfig,
)


def make_config(
    auth_enabled: bool = False,
    keys: list[ApiKeyConfig] | None = None,
    allow_model_management: bool = False,
    max_concurrent: int = 2,
) -> Config:
    return Config(
        proxy=ProxyConfig(max_concurrent=max_concurrent, allow_model_management=allow_model_management),
        ollama=OllamaConfig(hosts=[HostConfig(url="http://ollama-test:11434", name="test")]),
        queue=QueueConfig(
            high=TierConfig(max_depth=5, max_wait=10),
            normal=TierConfig(max_depth=10, max_wait=30),
            low=TierConfig(max_depth=20, max_wait=60),
        ),
        webhooks=WebhookConfig(enabled=False),
        auth=AuthConfig(
            enabled=auth_enabled,
            keys=keys or [],
            rate_limit=RateLimitConfig(max_failures=5, window_seconds=60),
        ) if not (auth_enabled and not keys) else AuthConfig(enabled=False),
        logging=LoggingConfig(level="error", format="text"),
    )


ADMIN_KEY = "test-admin-key-00000000"
USER_KEY = "test-user-key-111111111"
LOW_KEY = "test-low-key-2222222222"

ADMIN_KEY_CFG = ApiKeyConfig(
    key=ADMIN_KEY,
    client_id="admin",
    description="Admin key",
    max_priority="high",
    management=True,
)
USER_KEY_CFG = ApiKeyConfig(
    key=USER_KEY,
    client_id="user",
    description="Regular user",
    max_priority="normal",
    management=False,
)
LOW_KEY_CFG = ApiKeyConfig(
    key=LOW_KEY,
    client_id="background",
    description="Background jobs",
    max_priority="low",
    management=False,
)
