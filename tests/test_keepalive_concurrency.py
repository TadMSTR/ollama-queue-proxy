"""Tests for keep_alive injection and per-client concurrency caps."""

from __future__ import annotations

import asyncio
import json

import pytest

from ollama_queue_proxy.config import ApiKeyConfig
from ollama_queue_proxy.concurrency import ClientConcurrencyManager, FAIRNESS_MAX_REENTRIES
from ollama_queue_proxy.main import _inject_keep_alive


# ---------------------------------------------------------------------------
# keep_alive injection
# ---------------------------------------------------------------------------


def _body(**kwargs) -> bytes:
    return json.dumps(kwargs, separators=(",", ":")).encode()


def _parsed(b: bytes) -> dict:
    return json.loads(b)


def test_inject_keep_alive_when_missing():
    body = _body(model="llama3", prompt="hello")
    result = _inject_keep_alive(body, "5m", override=False, max_body_mb=50)
    data = _parsed(result)
    assert data["keep_alive"] == "5m"
    assert data["model"] == "llama3"  # existing fields preserved


def test_inject_keep_alive_respected_when_present_no_override():
    body = _body(model="llama3", prompt="hello", keep_alive="10m")
    result = _inject_keep_alive(body, "5m", override=False, max_body_mb=50)
    data = _parsed(result)
    assert data["keep_alive"] == "10m"  # client value preserved


def test_inject_keep_alive_replaced_when_override_true():
    body = _body(model="llama3", prompt="hello", keep_alive="10m")
    result = _inject_keep_alive(body, "5m", override=True, max_body_mb=50)
    data = _parsed(result)
    assert data["keep_alive"] == "5m"  # proxy default wins


def test_inject_keep_alive_non_json_passthrough():
    body = b"not json at all"
    result = _inject_keep_alive(body, "5m", override=False, max_body_mb=50)
    assert result == body


def test_inject_keep_alive_empty_body_passthrough():
    result = _inject_keep_alive(b"", "5m", override=False, max_body_mb=50)
    assert result == b""


def test_inject_keep_alive_oversized_body_skipped():
    big_body = json.dumps({"model": "llama3", "data": "x" * 1000}).encode()
    result = _inject_keep_alive(big_body, "5m", override=False, max_body_mb=0)
    # max_body_mb=0 means 0 bytes threshold → body not mutated
    assert result == big_body


def test_inject_keep_alive_non_dict_json_passthrough():
    body = json.dumps([1, 2, 3]).encode()
    result = _inject_keep_alive(body, "5m", override=False, max_body_mb=50)
    assert result == body


# ---------------------------------------------------------------------------
# ClientConcurrencyManager — unlimited client
# ---------------------------------------------------------------------------


def make_key(client_id: str, max_concurrent: int = 0) -> ApiKeyConfig:
    return ApiKeyConfig(key="k", client_id=client_id, max_concurrent=max_concurrent)


@pytest.mark.asyncio
async def test_unlimited_client_never_blocks():
    mgr = ClientConcurrencyManager([make_key("svc", max_concurrent=0)])
    # Should return immediately without blocking
    for _ in range(10):
        await mgr.acquire("svc")
    assert mgr.inflight_counts()["svc"] == 10
    for _ in range(10):
        mgr.release("svc")
    assert mgr.inflight_counts()["svc"] == 0


@pytest.mark.asyncio
async def test_unknown_client_acquire_no_error():
    mgr = ClientConcurrencyManager([])
    await mgr.acquire("ghost")  # must not raise or block
    mgr.release("ghost")


# ---------------------------------------------------------------------------
# ClientConcurrencyManager — capped client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capped_client_blocks_nth_request():
    mgr = ClientConcurrencyManager([make_key("batch", max_concurrent=2)])

    await mgr.acquire("batch")
    await mgr.acquire("batch")
    # 3rd acquire should block — test with a timeout to prove it doesn't return immediately
    blocked = False

    async def try_acquire():
        nonlocal blocked
        await mgr.acquire("batch")
        blocked = True

    task = asyncio.create_task(try_acquire())
    await asyncio.sleep(0.05)  # give it time to block
    assert not blocked, "3rd acquire should be blocked at cap=2"

    mgr.release("batch")
    await asyncio.sleep(0.05)
    assert blocked, "3rd acquire should unblock after a slot is released"
    task.cancel()


@pytest.mark.asyncio
async def test_cap_waiting_increments_while_blocked():
    mgr = ClientConcurrencyManager([make_key("batch", max_concurrent=1)])
    await mgr.acquire("batch")  # fill the cap

    acquired = asyncio.Event()

    async def waiter():
        await mgr.acquire("batch")
        acquired.set()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert mgr.cap_waiting_counts()["batch"] >= 1

    mgr.release("batch")
    await asyncio.sleep(0.05)
    assert acquired.is_set()
    task.cancel()


@pytest.mark.asyncio
async def test_release_decrements_inflight():
    mgr = ClientConcurrencyManager([make_key("svc", max_concurrent=3)])
    await mgr.acquire("svc")
    await mgr.acquire("svc")
    assert mgr.inflight_counts()["svc"] == 2
    mgr.release("svc")
    assert mgr.inflight_counts()["svc"] == 1


# ---------------------------------------------------------------------------
# Fairness bound — bypass after FAIRNESS_MAX_REENTRIES
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fairness_bypass_after_max_reentries():
    mgr = ClientConcurrencyManager([make_key("batch", max_concurrent=1)])
    await mgr.acquire("batch")  # fill the cap

    # With reentries >= FAIRNESS_MAX_REENTRIES, acquire must NOT block
    acquired = asyncio.Event()

    async def fairness_acquire():
        await mgr.acquire("batch", reentries=FAIRNESS_MAX_REENTRIES)
        acquired.set()

    task = asyncio.create_task(fairness_acquire())
    await asyncio.sleep(0.05)
    assert acquired.is_set(), "Fairness bypass should allow past-cap acquire"
    task.cancel()


# ---------------------------------------------------------------------------
# Priority isolation: different clients don't share semaphores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_clients_independent_semaphores():
    mgr = ClientConcurrencyManager([
        make_key("batch", max_concurrent=1),
        make_key("interactive", max_concurrent=2),
    ])

    await mgr.acquire("batch")  # fill batch cap

    # interactive client should NOT be blocked by batch being at cap
    interactive_acquired = asyncio.Event()

    async def interactive_acquire():
        await mgr.acquire("interactive")
        interactive_acquired.set()

    task = asyncio.create_task(interactive_acquire())
    await asyncio.sleep(0.05)
    assert interactive_acquired.is_set(), "Interactive client must not be blocked by batch cap"
    task.cancel()
