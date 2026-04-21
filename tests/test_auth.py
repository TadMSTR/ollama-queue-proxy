"""Tests for authentication, priority ceiling, and rate limiting."""

from __future__ import annotations

import pytest

from ollama_queue_proxy.auth import AuthManager
from ollama_queue_proxy.config import AuthConfig, RateLimitConfig

from .conftest import ADMIN_KEY, ADMIN_KEY_CFG, LOW_KEY, LOW_KEY_CFG, USER_KEY, USER_KEY_CFG


def make_auth(enabled: bool = True, keys=None) -> AuthManager:
    return AuthManager(
        AuthConfig(
            enabled=enabled,
            keys=keys or [ADMIN_KEY_CFG, USER_KEY_CFG, LOW_KEY_CFG],
            rate_limit=RateLimitConfig(max_failures=3, window_seconds=60),
        )
    )


def test_lookup_valid_key():
    mgr = make_auth()
    cfg = mgr.lookup_key(ADMIN_KEY)
    assert cfg is not None
    assert cfg.client_id == "admin"


def test_lookup_invalid_key():
    mgr = make_auth()
    assert mgr.lookup_key("wrong-key") is None


def test_priority_ceiling_low_key_high_request():
    mgr = make_auth()
    result = mgr.enforce_priority_ceiling("high", LOW_KEY_CFG)
    assert result == "low"


def test_priority_ceiling_normal_key_high_request():
    mgr = make_auth()
    result = mgr.enforce_priority_ceiling("high", USER_KEY_CFG)
    assert result == "normal"


def test_priority_ceiling_high_key_no_cap():
    mgr = make_auth()
    result = mgr.enforce_priority_ceiling("high", ADMIN_KEY_CFG)
    assert result == "high"


def test_priority_ceiling_no_key_auth_disabled():
    mgr = make_auth(enabled=False)
    result = mgr.enforce_priority_ceiling("normal", None)
    assert result == "normal"


def test_priority_ceiling_unknown_priority_defaults_normal():
    mgr = make_auth()
    result = mgr.enforce_priority_ceiling("invalid", ADMIN_KEY_CFG)
    assert result == "normal"


@pytest.mark.asyncio
async def test_rate_limiting():
    mgr = make_auth()
    ip = "1.2.3.4"
    # Record max_failures failures
    for _ in range(3):
        await mgr._record_failure(ip)
    assert await mgr._is_rate_limited(ip) is True


@pytest.mark.asyncio
async def test_rate_limit_not_triggered_below_threshold():
    mgr = make_auth()
    ip = "1.2.3.5"
    await mgr._record_failure(ip)
    await mgr._record_failure(ip)
    assert await mgr._is_rate_limited(ip) is False
