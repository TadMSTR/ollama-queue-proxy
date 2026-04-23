"""Tests for model-aware routing table."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ollama_queue_proxy.config import HostConfig, OllamaConfig, RoutingConfig
from ollama_queue_proxy.routing import HostRoutingState, RoutingTable


def make_ollama_config(hosts: list[dict]) -> OllamaConfig:
    return OllamaConfig(
        hosts=[HostConfig(**h) for h in hosts],
        health_check_interval=30,
    )


def make_routing_config(strategy: str = "model_aware") -> RoutingConfig:
    return RoutingConfig(strategy=strategy, fallback="any_healthy", model_poll_timeout=3)  # type: ignore[arg-type]


def make_table(hosts: list[dict], strategy: str = "model_aware") -> RoutingTable:
    ollama_cfg = make_ollama_config(hosts)
    routing_cfg = make_routing_config(strategy)
    mock_client = MagicMock()
    return RoutingTable(ollama_cfg, routing_cfg, mock_client)


# ---------------------------------------------------------------------------
# Weighted round-robin
# ---------------------------------------------------------------------------


def test_round_robin_single_host():
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    for state in table._states.values():
        state.loaded_models = {"llama3"}
        state.reachable = True

    results = [table.pick("llama3").name for _ in range(5)]
    assert all(r == "a" for r in results)


def test_round_robin_two_hosts_equal_weight():
    table = make_table([
        {"url": "http://a:11434", "name": "a", "weight": 1},
        {"url": "http://b:11434", "name": "b", "weight": 1},
    ])
    for state in table._states.values():
        state.loaded_models = {"llama3"}
        state.reachable = True

    results = [table.pick("llama3").name for _ in range(4)]
    assert results.count("a") == 2
    assert results.count("b") == 2


def test_round_robin_weighted_2_to_1():
    table = make_table([
        {"url": "http://a:11434", "name": "a", "weight": 2},
        {"url": "http://b:11434", "name": "b", "weight": 1},
    ])
    for state in table._states.values():
        state.loaded_models = {"llama3"}
        state.reachable = True

    results = [table.pick("llama3").name for _ in range(9)]
    assert results.count("a") == 6
    assert results.count("b") == 3


# ---------------------------------------------------------------------------
# Model-aware routing choices
# ---------------------------------------------------------------------------


def test_routes_to_host_with_model():
    table = make_table([
        {"url": "http://a:11434", "name": "a", "weight": 1},
        {"url": "http://b:11434", "name": "b", "weight": 1},
    ])
    table._states["a"].loaded_models = {"llama3"}
    table._states["a"].reachable = True
    table._states["b"].loaded_models = {"mistral"}
    table._states["b"].reachable = True

    # All requests for llama3 should go to host a
    results = {table.pick("llama3").name for _ in range(5)}
    assert results == {"a"}

    # All requests for mistral should go to host b
    results = {table.pick("mistral").name for _ in range(5)}
    assert results == {"b"}


def test_falls_back_when_no_host_has_model():
    table = make_table([
        {"url": "http://a:11434", "name": "a", "weight": 1},
        {"url": "http://b:11434", "name": "b", "weight": 1},
    ])
    table._states["a"].loaded_models = set()
    table._states["a"].reachable = True
    table._states["b"].loaded_models = set()
    table._states["b"].reachable = True

    result = table.pick("llama3")
    assert result is not None  # fallback returns a healthy host
    assert table.routing_decisions["fallback"] == 1


def test_skips_unreachable_host():
    table = make_table([
        {"url": "http://a:11434", "name": "a", "weight": 1},
        {"url": "http://b:11434", "name": "b", "weight": 1},
    ])
    table._states["a"].loaded_models = {"llama3"}
    table._states["a"].reachable = False  # unreachable
    table._states["b"].loaded_models = {"llama3"}
    table._states["b"].reachable = True

    result = table.pick("llama3")
    assert result is not None
    assert result.name == "b"


def test_returns_none_when_all_unreachable():
    table = make_table([
        {"url": "http://a:11434", "name": "a", "weight": 1},
    ])
    table._states["a"].reachable = False

    result = table.pick("llama3")
    assert result is None


def test_no_model_field_uses_round_robin():
    table = make_table([
        {"url": "http://a:11434", "name": "a", "weight": 1},
        {"url": "http://b:11434", "name": "b", "weight": 1},
    ])
    for state in table._states.values():
        state.reachable = True

    results = [table.pick(None).name for _ in range(4)]
    assert table.routing_decisions["round_robin"] == 4


# ---------------------------------------------------------------------------
# Fast-path invalidation
# ---------------------------------------------------------------------------


def test_invalidate_removes_model_from_host():
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    table._states["a"].loaded_models = {"llama3", "mistral"}

    table.invalidate("a", "llama3")

    assert "llama3" not in table._states["a"].loaded_models
    assert "mistral" in table._states["a"].loaded_models  # other models unaffected


def test_invalidate_unknown_host_no_error():
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    table.invalidate("nonexistent", "llama3")  # must not raise


def test_invalidate_model_not_present_no_error():
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    table._states["a"].loaded_models = {"mistral"}
    table.invalidate("a", "llama3")  # must not raise
    assert "mistral" in table._states["a"].loaded_models


# ---------------------------------------------------------------------------
# Background poller (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_host_updates_models():
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    state = table._states["a"]

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"models": [{"name": "llama3"}, {"name": "mistral"}]}
    mock_resp.raise_for_status = MagicMock()

    table._client = AsyncMock()
    table._client.get.return_value = mock_resp

    await table._poll_host(state)

    assert state.loaded_models == {"llama3", "mistral"}
    assert state.reachable is True


@pytest.mark.asyncio
async def test_poll_host_marks_unreachable_on_error():
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    state = table._states["a"]
    state.reachable = True

    table._client = AsyncMock()
    table._client.get.side_effect = Exception("connection refused")

    await table._poll_host(state)

    assert state.reachable is False


# ---------------------------------------------------------------------------
# Startup probe fail-fast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_probe_exits_when_all_unreachable(capsys):
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    table._client = AsyncMock()
    table._client.get.side_effect = Exception("refused")

    with pytest.raises(SystemExit):
        await table.startup_probe()


@pytest.mark.asyncio
async def test_startup_probe_succeeds_with_one_reachable():
    table = make_table([
        {"url": "http://a:11434", "name": "a", "weight": 1},
        {"url": "http://b:11434", "name": "b", "weight": 1},
    ])

    good_resp = MagicMock()
    good_resp.json.return_value = {"models": [{"name": "llama3"}]}
    good_resp.raise_for_status = MagicMock()

    async def get_side_effect(url, **kwargs):
        if "//a:" in url:
            raise Exception("refused")
        return good_resp

    table._client = AsyncMock()
    table._client.get.side_effect = get_side_effect

    await table.startup_probe()  # must not raise — host b is reachable
    assert table._states["b"].reachable is True
    assert table._states["a"].reachable is False


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_host_model_counts():
    table = make_table([
        {"url": "http://a:11434", "name": "a", "weight": 1},
        {"url": "http://b:11434", "name": "b", "weight": 1},
    ])
    table._states["a"].loaded_models = {"llama3", "mistral"}
    table._states["b"].loaded_models = {"phi3"}

    counts = table.host_model_counts()
    assert counts["a"] == 2
    assert counts["b"] == 1


def test_routing_decisions_incremented():
    table = make_table([
        {"url": "http://a:11434", "name": "a", "weight": 1},
    ])
    table._states["a"].loaded_models = {"llama3"}
    table._states["a"].reachable = True

    table.pick("llama3")
    assert table.routing_decisions["model_match"] == 1
    assert table.routing_decisions["round_robin"] == 0
