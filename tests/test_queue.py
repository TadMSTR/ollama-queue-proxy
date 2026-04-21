"""Tests for priority queue: ordering, overflow, expiry, pause/flush."""

from __future__ import annotations

import asyncio
import time

import pytest

from ollama_queue_proxy.config import QueueConfig, TierConfig
from ollama_queue_proxy.queue import (
    PriorityQueueManager,
    QueueFull,
    QueueFlushed,
    QueueItem,
    QueuePaused,
)


def make_queue_mgr(max_concurrent: int = 2, high_depth=5, normal_depth=10, low_depth=20) -> PriorityQueueManager:
    config = QueueConfig(
        high=TierConfig(max_depth=high_depth, max_wait=60),
        normal=TierConfig(max_depth=normal_depth, max_wait=120),
        low=TierConfig(max_depth=low_depth, max_wait=300),
    )
    return PriorityQueueManager(config, max_concurrent)


def make_item(tier: str, request_id: str = "test") -> QueueItem:
    loop = asyncio.get_event_loop()
    future = loop.create_future()

    async def noop():
        return "ok"

    return QueueItem(
        tier=tier,
        enqueue_time=time.monotonic(),
        request_id=request_id,
        future=future,
        dispatch_fn=noop,
    )


@pytest.mark.asyncio
async def test_enqueue_returns_position():
    mgr = make_queue_mgr()
    item = make_item("normal", "req1")
    pos = await mgr.enqueue(item)
    assert pos == 1


@pytest.mark.asyncio
async def test_enqueue_position_increments():
    mgr = make_queue_mgr()
    pos1 = await mgr.enqueue(make_item("normal", "req1"))
    pos2 = await mgr.enqueue(make_item("normal", "req2"))
    assert pos2 == 2


@pytest.mark.asyncio
async def test_queue_full_raises():
    mgr = make_queue_mgr(normal_depth=2)
    await mgr.enqueue(make_item("normal", "r1"))
    await mgr.enqueue(make_item("normal", "r2"))
    with pytest.raises(QueueFull) as exc_info:
        await mgr.enqueue(make_item("normal", "r3"))
    assert exc_info.value.tier == "normal"


@pytest.mark.asyncio
async def test_queue_full_increments_rejected():
    mgr = make_queue_mgr(normal_depth=1)
    await mgr.enqueue(make_item("normal", "r1"))
    with pytest.raises(QueueFull):
        await mgr.enqueue(make_item("normal", "r2"))
    assert mgr.stats()["normal"].rejected == 1


@pytest.mark.asyncio
async def test_pause_raises_queue_paused():
    mgr = make_queue_mgr()
    mgr.pause("low")
    with pytest.raises(QueuePaused) as exc_info:
        await mgr.enqueue(make_item("low", "r1"))
    assert exc_info.value.tier == "low"


@pytest.mark.asyncio
async def test_resume_after_pause():
    mgr = make_queue_mgr()
    mgr.pause("low")
    mgr.resume("low")
    pos = await mgr.enqueue(make_item("low", "r1"))
    assert pos == 1


@pytest.mark.asyncio
async def test_flush_drops_items():
    mgr = make_queue_mgr()
    item = make_item("low", "r1")
    await mgr.enqueue(item)
    dropped = await mgr.flush("low")
    assert dropped == 1
    assert item.future.exception().__class__.__name__ == "QueueFlushed"


@pytest.mark.asyncio
async def test_queue_depths():
    mgr = make_queue_mgr()
    await mgr.enqueue(make_item("high", "r1"))
    await mgr.enqueue(make_item("normal", "r2"))
    depths = mgr.queue_depths()
    assert depths["high"] == 1
    assert depths["normal"] == 1
    assert depths["low"] == 0


@pytest.mark.asyncio
async def test_priority_ordering():
    """High-tier items should be dispatched before normal and low."""
    mgr = make_queue_mgr(max_concurrent=1)
    dispatched = []

    async def make_dispatch(label):
        async def fn():
            dispatched.append(label)
            return label
        return fn

    low_item = make_item("low", "low")
    low_item.dispatch_fn = await make_dispatch("low")

    normal_item = make_item("normal", "normal")
    normal_item.dispatch_fn = await make_dispatch("normal")

    high_item = make_item("high", "high")
    high_item.dispatch_fn = await make_dispatch("high")

    await mgr.enqueue(low_item)
    await mgr.enqueue(normal_item)
    await mgr.enqueue(high_item)

    mgr.start_workers()
    # Give workers time to process
    await asyncio.sleep(0.2)
    await mgr.stop_workers()

    # High should be processed before normal before low
    assert dispatched[0] == "high"
    assert dispatched[1] == "normal"
    assert dispatched[2] == "low"
