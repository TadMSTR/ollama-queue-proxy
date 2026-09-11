"""Tests for model-aware routing table."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from ollama_queue_proxy.config import HostConfig, OllamaConfig, RoutingConfig
from ollama_queue_proxy.routing import RoutingTable


def make_ollama_config(hosts: list[dict], health_check_interval: int = 30) -> OllamaConfig:
    return OllamaConfig(
        hosts=[HostConfig(**h) for h in hosts],
        health_check_interval=health_check_interval,
    )


def make_routing_config(strategy: str = "model_aware") -> RoutingConfig:
    return RoutingConfig(strategy=strategy, fallback="any_healthy", model_poll_timeout=3)  # type: ignore[arg-type]


def make_table(
    hosts: list[dict], strategy: str = "model_aware", health_check_interval: int = 30
) -> RoutingTable:
    ollama_cfg = make_ollama_config(hosts, health_check_interval)
    routing_cfg = make_routing_config(strategy)
    mock_client = MagicMock()
    return RoutingTable(ollama_cfg, routing_cfg, mock_client)


# ---------------------------------------------------------------------------
# Weighted round-robin
# ---------------------------------------------------------------------------


def test_round_robin_single_host():
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    for state in table._states.values():
        state.installed_models = {"llama3"}
        state.reachable = True

    results = [table.pick("llama3").name for _ in range(5)]
    assert all(r == "a" for r in results)


def test_round_robin_two_hosts_equal_weight():
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 1},
            {"url": "http://b:11434", "name": "b", "weight": 1},
        ]
    )
    for state in table._states.values():
        state.installed_models = {"llama3"}
        state.reachable = True

    results = [table.pick("llama3").name for _ in range(4)]
    assert results.count("a") == 2
    assert results.count("b") == 2


def test_round_robin_weighted_2_to_1():
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 2},
            {"url": "http://b:11434", "name": "b", "weight": 1},
        ]
    )
    for state in table._states.values():
        state.installed_models = {"llama3"}
        state.reachable = True

    results = [table.pick("llama3").name for _ in range(9)]
    assert results.count("a") == 6
    assert results.count("b") == 3


# ---------------------------------------------------------------------------
# Model-aware routing choices
# ---------------------------------------------------------------------------


def test_routes_to_host_with_model():
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 1},
            {"url": "http://b:11434", "name": "b", "weight": 1},
        ]
    )
    table._states["a"].installed_models = {"llama3"}
    table._states["a"].reachable = True
    table._states["b"].installed_models = {"mistral"}
    table._states["b"].reachable = True

    # All requests for llama3 should go to host a
    results = {table.pick("llama3").name for _ in range(5)}
    assert results == {"a"}

    # All requests for mistral should go to host b
    results = {table.pick("mistral").name for _ in range(5)}
    assert results == {"b"}


def test_falls_back_when_no_host_has_model():
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 1},
            {"url": "http://b:11434", "name": "b", "weight": 1},
        ]
    )
    table._states["a"].installed_models = set()
    table._states["a"].reachable = True
    table._states["b"].installed_models = set()
    table._states["b"].reachable = True

    result = table.pick("llama3")
    assert result is not None  # fallback returns a healthy host
    assert table.routing_decisions["fallback"] == 1


def test_skips_unreachable_host():
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 1},
            {"url": "http://b:11434", "name": "b", "weight": 1},
        ]
    )
    table._states["a"].installed_models = {"llama3"}
    table._states["a"].reachable = False  # unreachable
    table._states["b"].installed_models = {"llama3"}
    table._states["b"].reachable = True

    result = table.pick("llama3")
    assert result is not None
    assert result.name == "b"


def test_still_picks_when_all_hosts_are_marked_unreachable():
    """
    Changed contract in 0.4.0. This test previously asserted pick() returns None when
    every host is unreachable, and it is retargeted rather than dropped because the
    situation it covers still needs a defined answer.

    `reachable` is a cached observation, not ground truth: one failed poll against a
    host that has since recovered leaves it False until the next interval. Returning
    None there black-holes the proxy into 503s while a working host sits idle. Handing
    back a candidate costs one failed attempt in a genuine outage — which then 503s
    anyway via the failover loop — and costs nothing when the flag is merely stale.
    """
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 1},
        ]
    )
    table._states["a"].reachable = False

    result = table.pick("llama3")
    assert result is not None, "a stale reachable=False must not black-hole the proxy"
    assert result.name == "a"


def test_prefers_reachable_host_over_unreachable_without_a_model():
    """
    The reachable preference must hold on the round_robin path too, not just
    model_aware. It did not: pick() passed every host, up or down, straight into
    weighted round-robin, so a known-dead host was handed out as readily as a live
    one and burned a failover attempt every other request.
    """
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 1},
            {"url": "http://b:11434", "name": "b", "weight": 1},
        ],
        strategy="round_robin",
    )
    table._states["a"].reachable = False
    table._states["b"].reachable = True

    picked = {table.pick(None).name for _ in range(6)}
    assert picked == {"b"}, "round_robin must not hand out a host known to be down"


def test_no_model_field_uses_round_robin():
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 1},
            {"url": "http://b:11434", "name": "b", "weight": 1},
        ]
    )
    for state in table._states.values():
        state.reachable = True

    results = [table.pick(None).name for _ in range(4)]
    assert table.routing_decisions["round_robin"] == 4
    # The counter alone says only that the branch was taken, not that it rotated.
    # _pick_round_robin is deterministic, so two equal-weight hosts over four picks
    # must alternate and land twice each.
    assert sorted(results) == ["a", "a", "b", "b"]
    assert results[0] != results[1]


# ---------------------------------------------------------------------------
# Fast-path invalidation
# ---------------------------------------------------------------------------


def test_invalidate_removes_model_from_host():
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    table._states["a"].installed_models = {"llama3", "mistral"}

    table.invalidate("a", "llama3")

    assert "llama3" not in table._states["a"].installed_models
    assert "mistral" in table._states["a"].installed_models  # other models unaffected


def test_invalidate_unknown_host_no_error():
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    table.invalidate("nonexistent", "llama3")  # must not raise


def test_invalidate_model_not_present_no_error():
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    table._states["a"].installed_models = {"mistral"}
    table.invalidate("a", "llama3")  # must not raise
    assert "mistral" in table._states["a"].installed_models


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

    assert state.installed_models == {"llama3", "mistral"}
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
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 1},
            {"url": "http://b:11434", "name": "b", "weight": 1},
        ]
    )

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
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 1},
            {"url": "http://b:11434", "name": "b", "weight": 1},
        ]
    )
    table._states["a"].installed_models = {"llama3", "mistral"}
    table._states["b"].installed_models = {"phi3"}

    counts = table.host_model_counts()
    assert counts["a"] == 2
    assert counts["b"] == 1


def test_routing_decisions_incremented():
    table = make_table(
        [
            {"url": "http://a:11434", "name": "a", "weight": 1},
        ]
    )
    table._states["a"].installed_models = {"llama3"}
    table._states["a"].reachable = True

    table.pick("llama3")
    assert table.routing_decisions["model_match"] == 1
    assert table.routing_decisions["round_robin"] == 0


# ---------------------------------------------------------------------------
# Host-state unification (0.4.0) — regression guards
#
# These cover the defects that existed because host state lived in two structures
# with different refresh rules. They are written against the LOOP, not against
# _poll_host, because _poll_host was never the broken part: the old
# HostManager._health_loop refreshed a host only `if not host.healthy`, so a host
# that stayed up was never re-read and its model list was frozen for the lifetime
# of the process. A test calling _poll_host directly passes under both the old and
# the new behaviour and would prove nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_loop_refreshes_a_host_that_is_and_stays_reachable():
    """Pull a new model on a healthy host and the proxy must see it without a restart."""
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    state = table._states["a"]
    state.reachable = True
    state.installed_models = {"llama3"}
    state.model_sync_interval = 0  # poll immediately, repeatedly

    responses = [
        {"models": [{"name": "llama3"}]},
        {"models": [{"name": "llama3"}, {"name": "mistral"}]},
    ]

    def next_response(*_args, **_kwargs):
        payload = responses[0] if len(responses) == 1 else responses.pop(0)
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()
        return resp

    table._client = AsyncMock()
    table._client.get.side_effect = next_response

    task = asyncio.create_task(table._poll_loop(state))
    try:
        for _ in range(200):
            await asyncio.sleep(0)
            if "mistral" in state.installed_models:
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert state.installed_models == {"llama3", "mistral"}, (
        "a host that never went unreachable must still be re-polled — this is the "
        "frozen-inventory defect the HostManager/RoutingTable unification removes"
    )
    assert state.reachable is True


@pytest.mark.asyncio
async def test_poll_records_last_checked():
    """`last_checked` drives /queue/status. It was only ever set by the deleted
    HostManager, so after unification the poller has to set it or the field reads
    null forever while the host is polled fine."""
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    state = table._states["a"]
    assert state.last_checked is None

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"models": [{"name": "llama3"}]}
    mock_resp.raise_for_status = MagicMock()
    table._client = AsyncMock()
    table._client.get.return_value = mock_resp

    await table._poll_host(state)
    assert state.last_checked is not None


@pytest.mark.asyncio
async def test_poll_records_last_checked_on_failure_too():
    """A host that cannot be reached is still a host that was checked. If only the
    success path stamped it, an unreachable host would report a last_checked frozen
    at its final success — which reads as 'fine, just quiet'."""
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    state = table._states["a"]
    table._client = AsyncMock()
    table._client.get.side_effect = RuntimeError("connection refused")

    await table._poll_host(state)
    assert state.last_checked is not None
    assert state.reachable is False


def test_mark_unhealthy_counts_the_failure():
    """The request path's view of a failure and the poller's view now land on one
    structure. Previously mark_unhealthy() updated HostManager's copy while proxy.py
    separately reached into RoutingTable._states to set `reachable`."""
    table = make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    state = table._states["a"]
    assert state.failures == 0

    table.mark_unhealthy(state, "connect timeout")

    assert state.reachable is False
    assert state.failures == 1
    assert table.get("a") is state, "get() must return the same object the caller mutated"


def test_deprecated_health_check_interval_is_warned_about(caplog):
    """`ollama.health_check_interval` drove the deleted HostManager loop and now does
    nothing. A config key that silently stopped being read is indistinguishable from
    one being honoured, so setting it has to say so."""
    with caplog.at_level(logging.WARNING, logger="ollama_queue_proxy.routing"):
        make_table(
            [{"url": "http://a:11434", "name": "a", "weight": 1}],
            health_check_interval=17,
        )
    assert any("health_check_interval" in r.getMessage() for r in caplog.records)


def test_default_health_check_interval_is_not_warned_about(caplog):
    """CONTROL for the above. Warning on the default would fire for every operator who
    never set the key, which trains people to ignore the warning."""
    with caplog.at_level(logging.WARNING, logger="ollama_queue_proxy.routing"):
        make_table([{"url": "http://a:11434", "name": "a", "weight": 1}])
    assert not any("health_check_interval" in r.getMessage() for r in caplog.records)
