"""
Integration tests covering v0.2.0 feature combinations.

Cache tests that actually touch Valkey run only when VALKEY_URL is set
(CI injects it via the service container; local dev can set it manually).
Unit-level tests that verify the same semantics via mocks always run.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import JSONResponse

from ollama_queue_proxy.cache import (
    EmbeddingCache,
    _embed_key,
)
from ollama_queue_proxy.concurrency import ClientConcurrencyManager
from ollama_queue_proxy.config import ApiKeyConfig, EmbeddingCacheConfig
from ollama_queue_proxy.routes.status import _pm_label
from ollama_queue_proxy.routing import RoutingTable

VALKEY_URL = os.environ.get("VALKEY_URL", "redis://localhost:6379/0")


# ---------------------------------------------------------------------------
# Fixture: live Valkey connection (skip if unreachable)
# ---------------------------------------------------------------------------


@pytest.fixture
async def live_cache():
    """EmbeddingCache backed by a real Valkey — skipped if unreachable."""
    cfg = EmbeddingCacheConfig(
        enabled=True,
        backend=VALKEY_URL,
        ttl=60,
        max_entry_bytes=65536,
        key_prefix="oqp:inttest:",
        connect_timeout=2,
    )
    cache = EmbeddingCache(cfg)
    try:
        await cache.startup()
    except SystemExit:
        pytest.skip("Valkey not available — skipping live cache test")
    yield cache
    # Cleanup test keys
    if cache._client:
        async for key in cache._client.scan_iter("oqp:inttest:*"):
            await cache._client.delete(key)
    await cache.close()


# ---------------------------------------------------------------------------
# Live Valkey: second identical request returns cache hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_cache_hit_on_identical_request(live_cache):
    """Second identical /api/embed request must return from cache, not upstream."""
    cache = live_cache
    body_data = {"model": "nomic-embed-text", "input": "integration test sentence"}
    response_bytes = json.dumps({"embeddings": [[0.1, 0.2, 0.3]]}).encode()

    # Prime the cache
    await cache.set("/api/embed", body_data, "nomic-embed-text", response_bytes, "test-client")

    # Retrieve from cache
    result = await cache.get("/api/embed", body_data, "nomic-embed-text", "test-client")
    assert result == response_bytes


@pytest.mark.asyncio
async def test_live_cache_separate_endpoints(live_cache):
    """Same (model, text) on /api/embed vs /api/embeddings must be separate cache entries."""
    cache = live_cache
    text = "integration test cross-endpoint"

    embed_data = {"model": "nomic", "input": text}
    embeddings_data = {"model": "nomic", "prompt": text}
    response_bytes = json.dumps({"result": [0.9]}).encode()

    await cache.set("/api/embed", embed_data, "nomic", response_bytes, None)
    # /api/embeddings entry not stored — should miss
    result = await cache.get("/api/embeddings", embeddings_data, "nomic", None)
    assert result is None, "/api/embeddings must NOT hit the /api/embed cache entry"


# ---------------------------------------------------------------------------
# Injection port + cache hit: client attribution correct
# ---------------------------------------------------------------------------


def test_injection_port_client_id_with_cache_hit():
    """
    Injection port sets client_id to injected identity.
    Cache hit for that request should attribute stats to the injected client_id.
    This is a unit-level invariant test (no live Valkey required).
    """
    import ollama_queue_proxy.injection as inj_mod

    key_cfg = ApiKeyConfig(key="k", client_id="memsearch", max_priority="low")

    mock_state = MagicMock()
    mock_state.shutting_down = False
    inj_mod._shared_state = mock_state

    # Verify injection app would pass the correct client_id to _enqueue_request
    from unittest.mock import patch

    from fastapi.responses import JSONResponse

    captured_client_id = {}

    async def fake_enqueue(request, client_id, tier, state, reentries=0):
        captured_client_id["id"] = client_id
        return JSONResponse(status_code=200, content={"embeddings": [[0.1]]})

    with patch("ollama_queue_proxy.main._enqueue_request", side_effect=fake_enqueue):
        from fastapi.testclient import TestClient

        from ollama_queue_proxy.injection import make_injection_app

        inj_app = make_injection_app("memsearch", key_cfg)
        client = TestClient(inj_app, raise_server_exceptions=True)
        client.post("/api/embed", json={"model": "nomic", "input": "hi"})

    assert captured_client_id.get("id") == "memsearch"
    inj_mod._shared_state = None  # cleanup


# ---------------------------------------------------------------------------
# keep_alive + cache: cached response returns without keep_alive affecting key
# ---------------------------------------------------------------------------


def test_keep_alive_injection_does_not_affect_embed_cache_key():
    """
    keep_alive injected into /api/embed body must NOT change the cache key
    because keep_alive is not part of the embedding semantic. The cache key
    is derived from model + input only, not keep_alive.
    """
    body_without = json.dumps({"model": "nomic", "input": "hello"}, separators=(",", ":")).encode()
    body_with = json.dumps(
        {"model": "nomic", "input": "hello", "keep_alive": "5m"}, separators=(",", ":")
    ).encode()

    # Parse both to extract input for key derivation (same as EmbeddingCache does)
    data_without = json.loads(body_without)
    data_with = json.loads(body_with)

    key_without = _embed_key("oqp:embed:", "nomic", data_without)
    key_with = _embed_key("oqp:embed:", "nomic", data_with)

    # This previously read `assert key_with == key_with` — a tautology that held for
    # any _embed_key whatsoever. It sat beneath a comment asserting the opposite of
    # this test's own name and docstring ("Keys differ because keep_alive is part of
    # the dict passed to hashing"), and nothing could ever contradict it. The comment
    # was simply wrong: _embed_key reads only `input` out of body_data, so keep_alive
    # is structurally excluded from the preimage.
    #
    # The property is worth a real assertion. keep_alive is injected per-client before
    # the cache lookup, so if it did reach the key, two clients asking for the same
    # embedding with different keep_alive values would miss each other's cache entries
    # — silently halving the hit rate this cache exists to provide.
    assert key_with == key_without, "keep_alive must not reach the cache key preimage"
    assert key_with == _embed_key("oqp:embed:", "nomic", json.loads(body_with)), (
        "the key must be stable for a repeated identical injected body"
    )


# ---------------------------------------------------------------------------
# Per-client cap + priority: capped batch doesn't block interactive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capped_batch_does_not_block_interactive():
    """
    A batch client at concurrency cap must not block an interactive (high-priority)
    client from acquiring its own concurrency slot.
    """
    mgr = ClientConcurrencyManager(
        [
            ApiKeyConfig(key="k1", client_id="batch", max_concurrent=1),
            ApiKeyConfig(key="k2", client_id="interactive", max_concurrent=0),
        ]
    )

    # Fill batch client's cap
    await mgr.acquire("batch")

    # Interactive client (unlimited) should still acquire immediately
    interactive_done = False

    async def interactive_acquire():
        nonlocal interactive_done
        await mgr.acquire("interactive")
        interactive_done = True

    import asyncio

    task = asyncio.create_task(interactive_acquire())
    await asyncio.sleep(0.05)
    assert interactive_done, "Interactive (unlimited) client must not be blocked by batch cap"
    task.cancel()


# ---------------------------------------------------------------------------
# Streaming on injection port
# ---------------------------------------------------------------------------


def test_injection_handler_accepts_streaming_path():
    """
    The injection handler registers the catch-all route so streaming paths
    like /api/generate are accepted (not 404).
    """
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    import ollama_queue_proxy.injection as inj_mod
    from ollama_queue_proxy.injection import make_injection_app

    key_cfg = ApiKeyConfig(key="k", client_id="streamer", max_priority="normal")

    mock_state = MagicMock()
    mock_state.shutting_down = False
    inj_mod._shared_state = mock_state

    async def fake_enqueue(request, client_id, tier, state, reentries=0):
        return JSONResponse(status_code=200, content={"response": "ok"})

    with patch("ollama_queue_proxy.main._enqueue_request", side_effect=fake_enqueue):
        inj_app = make_injection_app("streamer", key_cfg)
        client = TestClient(inj_app, raise_server_exceptions=True)

        resp = client.post(
            "/api/generate",
            json={"model": "llama3", "prompt": "hello"},
        )
        assert resp.status_code == 200

    inj_mod._shared_state = None  # cleanup


# ---------------------------------------------------------------------------
# Model-aware + priority: high-priority reaches correct host
# ---------------------------------------------------------------------------


def test_model_aware_routing_picks_model_host():
    """
    When model_aware is active, a request for 'llama3' must route to the
    host that has llama3 loaded, regardless of which host is 'first'.
    """
    from unittest.mock import MagicMock

    from ollama_queue_proxy.config import HostConfig, OllamaConfig, RoutingConfig

    ollama_cfg = OllamaConfig(
        hosts=[
            HostConfig(url="http://a:11434", name="a", weight=1),
            HostConfig(url="http://b:11434", name="b", weight=1),
        ]
    )
    routing_cfg = RoutingConfig(strategy="model_aware", fallback="any_healthy")  # type: ignore[arg-type]
    table = RoutingTable(ollama_cfg, routing_cfg, MagicMock())

    table._states["a"].installed_models = set()
    table._states["a"].reachable = True
    table._states["b"].installed_models = {"llama3"}
    table._states["b"].reachable = True

    result = table.pick("llama3")
    assert result is not None
    assert result.name == "b"
    assert table.routing_decisions["model_match"] == 1


# ---------------------------------------------------------------------------
# v0.1.x config compatibility
# ---------------------------------------------------------------------------


def test_v1_config_still_passes_tests(tmp_path):
    """A pure v0.1.x config must load and produce default v0.2.0 behaviours."""
    import yaml

    from ollama_queue_proxy.config import load_config

    data = {
        "ollama": {"hosts": [{"url": "http://ollama:11434", "name": "primary"}]},
        "auth": {
            "enabled": True,
            "keys": [{"key": "mykey", "client_id": "svc", "max_priority": "high"}],
        },
    }
    path = str(tmp_path / "config.yml")
    with open(path, "w") as f:
        yaml.safe_dump(data, f)

    cfg = load_config(path)
    assert cfg.routing.strategy == "round_robin"
    assert cfg.embedding_cache.enabled is False
    assert cfg.client_injection.listeners == []
    assert cfg.keep_alive.default == "5m"
    assert cfg.auth.keys[0].max_concurrent == 0
    assert cfg.ollama.hosts[0].weight == 1


# ---------------------------------------------------------------------------
# Prometheus label escaping — prevents label-injection via client-supplied model names
# ---------------------------------------------------------------------------


def test_pm_label_escapes_double_quote():
    assert _pm_label('evil",injected="x') == 'evil\\",injected=\\"x'


def test_pm_label_escapes_backslash():
    assert _pm_label("path\\to\\model") == "path\\\\to\\\\model"


def test_pm_label_escapes_newline():
    assert _pm_label("line1\nline2") == "line1\\nline2"


def test_pm_label_plain_string_passthrough():
    assert _pm_label("nomic-embed-text") == "nomic-embed-text"


def test_pm_label_backslash_before_quote():
    # Backslash must be escaped before quote, so \" (escaped quote) does not
    # become \\" (literal backslash + broken quote).
    assert _pm_label('\\"') == '\\\\\\"'


# ---------------------------------------------------------------------------
# Metadata fast path (0.4.0)
#
# /api/tags and friends carry no inference cost, so spending an admission
# decision on them buys nothing and costs a wait. They used to enter the
# priority queue through the catch-all like everything else, so with
# max_concurrent small (2 on the deployment where this was found) a UI polling
# /api/tags queued behind a multi-minute generation and appeared to hang.
# ---------------------------------------------------------------------------


def _fake_request(path: str, method: str = "GET"):
    from fastapi import Request

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)
    request.state.request_id = "fp-test"
    return request


def _state_with_spy_queue(cfg):
    """AppState whose queue manager records any enqueue attempt and nothing else."""
    from ollama_queue_proxy.main import AppState

    state = MagicMock(spec=AppState)
    state.config = cfg
    state.embedding_cache = None
    state.concurrency_manager = None
    state.http_client = AsyncMock()
    state.routing_table = MagicMock()
    state.client_stats = {}
    state.queue_manager = MagicMock()
    state.queue_manager.enqueue = AsyncMock(side_effect=AssertionError("must not enqueue"))
    return state


@pytest.mark.parametrize("path", ["/api/tags", "/api/version", "/api/ps", "/api/show", "/"])
@pytest.mark.asyncio
async def test_metadata_paths_bypass_the_queue(path):
    from ollama_queue_proxy import main
    from tests.conftest import make_config

    cfg = make_config()
    state = _state_with_spy_queue(cfg)
    sentinel = JSONResponse(status_code=200, content={"ok": True})

    with (
        patch("ollama_queue_proxy.main.read_body", new=AsyncMock(return_value=(b"", None))),
        patch(
            "ollama_queue_proxy.main.dispatch_request",
            new=AsyncMock(return_value=sentinel),
        ) as mock_dispatch,
    ):
        result = await main._enqueue_request(
            request=_fake_request(path),
            client_id="someone",
            tier="normal",
            state=state,
        )

    assert result is sentinel
    mock_dispatch.assert_awaited_once()
    state.queue_manager.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_inference_paths_still_enqueue():
    """CONTROL. Without it, a fast path that swallowed EVERY request would satisfy
    every assertion above — the queue would never be touched for any path, and the
    tests would read as a clean pass while the queue had been bypassed entirely."""
    from ollama_queue_proxy import main
    from tests.conftest import make_config

    cfg = make_config()
    state = _state_with_spy_queue(cfg)
    enqueued: list = []

    async def record(item):
        enqueued.append(item)
        item.future.set_result(JSONResponse(status_code=200, content={"ok": True}))
        return 1

    state.queue_manager.enqueue = AsyncMock(side_effect=record)

    with (
        patch(
            "ollama_queue_proxy.main.read_body",
            new=AsyncMock(return_value=(b'{"model":"llama3"}', None)),
        ),
        patch("ollama_queue_proxy.main.dispatch_request", new=AsyncMock()),
    ):
        await main._enqueue_request(
            request=_fake_request("/api/chat", method="POST"),
            client_id="someone",
            tier="normal",
            state=state,
        )

    assert len(enqueued) == 1, "/api/chat must still go through the queue"

    # The cap can only work if items carry their body size at all.
    #
    # Deliberately asserted as "at least the original", not an exact figure: /api/chat
    # is a keep_alive path, so the body is REWRITTEN with an injected keep_alive before
    # it is enqueued and arrives larger than it started (18 bytes in, 36 out). Counting
    # the post-injection size is the correct behaviour — that is the buffer actually
    # held in memory while the item waits — and pinning the exact number here would
    # just couple this test to the keep_alive default.
    original_len = len(b'{"model":"llama3"}')
    assert enqueued[0].nbytes >= original_len, (
        "queued items must carry the size of the body they retain"
    )


# ---------------------------------------------------------------------------
# Low-tier expiry semantics (confirmed, then documented — 0.4.0)
#
# At max_wait a queued item is NOT promoted, it FAILS. Under sustained
# high-tier load the low tier therefore does not merely wait, it errors. The
# plan for this change asked what status code the client actually receives
# before writing it into the README, rather than reading the raise site and
# assuming the handler passes it through — so this asserts the wire response.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_request_returns_503_to_the_client():
    from ollama_queue_proxy import main
    from ollama_queue_proxy.queue import QueueItem, RequestExpired
    from tests.conftest import make_config

    cfg = make_config()
    state = _state_with_spy_queue(cfg)

    async def expire(item: QueueItem):
        item.future.set_exception(RequestExpired(item.tier, item.request_id))
        return 1

    state.queue_manager.enqueue = AsyncMock(side_effect=expire)
    state.queue_manager.retry_after = MagicMock(return_value=5)

    with (
        patch(
            "ollama_queue_proxy.main.read_body",
            new=AsyncMock(return_value=(b'{"model":"llama3"}', None)),
        ),
        patch("ollama_queue_proxy.main.dispatch_request", new=AsyncMock()),
    ):
        response = await main._enqueue_request(
            request=_fake_request("/api/chat", method="POST"),
            client_id="batch-worker",
            tier="low",
            state=state,
        )

    assert response.status_code == 503
    assert b"request expired in queue" in bytes(response.body)

    # Not a promotion, and not a retry hint either — the client is told the request
    # failed, with nothing indicating it will fare better later. Priority aging is
    # deliberately not implemented (vikunja#787); this asserts the behaviour that
    # exists so the README can describe it accurately.
    assert "Retry-After" not in response.headers
