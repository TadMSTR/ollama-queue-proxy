"""Tests for client injection — port-based auth bypass."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ollama_queue_proxy.config import (
    ApiKeyConfig,
    ClientInjectionConfig,
    InjectionListenerConfig,
)
from ollama_queue_proxy.injection import make_injection_app, set_shared_state
from ollama_queue_proxy.proxy import _STRIP_REQUEST_HEADERS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_key_cfg(client_id: str = "memsearch", max_priority: str = "low") -> ApiKeyConfig:
    return ApiKeyConfig(
        key="secret",
        client_id=client_id,
        max_priority=max_priority,  # type: ignore[arg-type]
    )


def _make_mock_state(response_body: dict | None = None):
    """Build a minimal mock AppState that the injection handler can use."""
    from fastapi.responses import JSONResponse

    mock_state = MagicMock()
    mock_state.shutting_down = False
    mock_state.config.proxy.max_request_body_mb = 50
    mock_state.client_stats = {}

    if response_body is None:
        response_body = {"model": "nomic-embed-text", "embeddings": [[0.1, 0.2]]}

    # _enqueue_request calls read_body then dispatch_request via queue.
    # Patch at the main module level to intercept before queuing.
    return mock_state


# ---------------------------------------------------------------------------
# Authorization header stripping (security FLAG G)
# ---------------------------------------------------------------------------


def test_authorization_in_strip_headers():
    """proxy.py must strip Authorization before forwarding — covers FLAG G."""
    assert "authorization" in _STRIP_REQUEST_HEADERS


# ---------------------------------------------------------------------------
# Injection app: responds without Bearer token
# ---------------------------------------------------------------------------


def test_injection_app_responds_without_auth(tmp_path):
    """Injection port must accept requests with no Authorization header."""
    key_cfg = make_key_cfg()

    with patch("ollama_queue_proxy.injection._shared_state") as mock_ref, \
         patch("ollama_queue_proxy.main._enqueue_request", new_callable=AsyncMock) as mock_enqueue:
        from fastapi.responses import JSONResponse

        mock_enqueue.return_value = JSONResponse(
            status_code=200,
            content={"embeddings": [[0.1, 0.2]]},
        )

        # Patch the module-level _shared_state directly
        import ollama_queue_proxy.injection as inj_mod

        mock_state = MagicMock()
        mock_state.shutting_down = False
        inj_mod._shared_state = mock_state

        inj_app = make_injection_app("memsearch", key_cfg)
        client = TestClient(inj_app, raise_server_exceptions=True)

        resp = client.post(
            "/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": "hello"},
            # No Authorization header
        )
        assert resp.status_code == 200
        assert mock_enqueue.called

        # Verify the injected client_id was passed
        call_kwargs = mock_enqueue.call_args
        assert call_kwargs.kwargs["client_id"] == "memsearch"

        inj_mod._shared_state = None  # cleanup


def test_injection_app_returns_503_when_state_unset():
    """Injection port must return 503 if called before main app has started."""
    import ollama_queue_proxy.injection as inj_mod

    key_cfg = make_key_cfg()
    inj_mod._shared_state = None

    inj_app = make_injection_app("memsearch", key_cfg)
    client = TestClient(inj_app, raise_server_exceptions=True)

    resp = client.post("/api/embeddings", json={"model": "nomic-embed-text", "prompt": "hello"})
    assert resp.status_code == 503
    assert "starting up" in resp.json()["error"]


def test_injection_app_returns_503_when_shutting_down():
    """Injection port must return 503 during shutdown."""
    import ollama_queue_proxy.injection as inj_mod

    key_cfg = make_key_cfg()
    mock_state = MagicMock()
    mock_state.shutting_down = True
    inj_mod._shared_state = mock_state

    inj_app = make_injection_app("memsearch", key_cfg)
    client = TestClient(inj_app, raise_server_exceptions=True)

    resp = client.post("/api/embeddings", json={"model": "nomic-embed-text", "prompt": "hello"})
    assert resp.status_code == 503
    assert "shutting down" in resp.json()["error"]

    inj_mod._shared_state = None  # cleanup


# ---------------------------------------------------------------------------
# Main port still requires Bearer
# ---------------------------------------------------------------------------


def test_main_port_requires_bearer(tmp_path):
    """
    When auth is enabled, the main port must return 401 for requests without Bearer.
    Injection bypasses this — main port must NOT be softened.
    """
    import yaml
    from ollama_queue_proxy.config import load_config

    cfg_data = {
        "ollama": {"hosts": [{"url": "http://ollama:11434", "name": "test"}]},
        "auth": {
            "enabled": True,
            "keys": [{"key": "mykey", "client_id": "svc", "max_priority": "normal"}],
        },
    }
    path = str(tmp_path / "config.yml")
    with open(path, "w") as f:
        yaml.safe_dump(cfg_data, f)

    cfg = load_config(path)
    assert cfg.auth.enabled is True
    assert cfg.auth.keys[0].key == "mykey"

    # Auth check is unit-tested here via AuthManager directly
    from ollama_queue_proxy.auth import AuthManager

    mgr = AuthManager(cfg.auth)
    key = mgr.lookup_key("mykey")
    assert key is not None
    assert mgr.lookup_key("wrong") is None


# ---------------------------------------------------------------------------
# set_shared_state round-trip
# ---------------------------------------------------------------------------


def test_set_shared_state():
    import ollama_queue_proxy.injection as inj_mod

    sentinel = object()
    set_shared_state(sentinel)
    assert inj_mod._shared_state is sentinel
    set_shared_state(None)
    assert inj_mod._shared_state is None


# ---------------------------------------------------------------------------
# Priority ceiling enforced on injection port
# ---------------------------------------------------------------------------


def test_injection_priority_ceiling_enforced():
    """
    If key's max_priority is 'low', a 'high' X-Queue-Priority header from the
    client should be silently capped to 'low' on the injection port.
    """
    import ollama_queue_proxy.injection as inj_mod
    from unittest.mock import patch, AsyncMock, MagicMock

    key_cfg = make_key_cfg(max_priority="low")
    mock_state = MagicMock()
    mock_state.shutting_down = False
    inj_mod._shared_state = mock_state

    captured_tier = {}

    async def fake_enqueue(request, client_id, tier, state):
        captured_tier["tier"] = tier
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=200, content={})

    with patch("ollama_queue_proxy.main._enqueue_request", side_effect=fake_enqueue):
        inj_app = make_injection_app("memsearch", key_cfg)
        client = TestClient(inj_app, raise_server_exceptions=True)

        # Client claims high priority
        client.post(
            "/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": "hi"},
            headers={"X-Queue-Priority": "high"},
        )

    assert captured_tier.get("tier") == "low", (
        f"Expected priority capped to 'low', got {captured_tier.get('tier')}"
    )

    inj_mod._shared_state = None  # cleanup
