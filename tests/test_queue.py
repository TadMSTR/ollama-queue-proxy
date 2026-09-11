"""Tests for priority queue: ordering, overflow, expiry, pause/flush."""

from __future__ import annotations

import asyncio
import time

import pytest

from ollama_queue_proxy.config import QueueConfig, TierConfig
from ollama_queue_proxy.queue import (
    PriorityQueueManager,
    QueueFull,
    QueueItem,
    QueueOverCapacity,
    QueuePaused,
    RequestExpired,
)


def make_queue_mgr(
    max_concurrent: int = 2, high_depth=5, normal_depth=10, low_depth=20, max_queued_mb=512
) -> PriorityQueueManager:
    config = QueueConfig(
        high=TierConfig(max_depth=high_depth, max_wait=60),
        normal=TierConfig(max_depth=normal_depth, max_wait=120),
        low=TierConfig(max_depth=low_depth, max_wait=300),
        max_queued_mb=max_queued_mb,
    )
    return PriorityQueueManager(config, max_concurrent)


def make_item(tier: str, request_id: str = "test", nbytes: int = 0) -> QueueItem:
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
        nbytes=nbytes,
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
    assert pos1 == 1
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


# ---------------------------------------------------------------------------
# Global queued-bytes cap (0.4.0)
#
# Depth limits bound the NUMBER of waiting requests. A queued request holds its
# whole buffered body until a worker takes it, so the memory ceiling was really
# depth x body size: 350 queued requests at the 50 MB per-request limit permits
# ~17 GB of resident bodies. Only the per-request size was bounded before.
# ---------------------------------------------------------------------------

MB = 1024 * 1024


@pytest.mark.asyncio
async def test_queued_bytes_accumulate_and_are_reported():
    mgr = make_queue_mgr()
    assert mgr.queued_bytes() == 0
    await mgr.enqueue(make_item("normal", "a", nbytes=3 * MB))
    await mgr.enqueue(make_item("normal", "b", nbytes=2 * MB))
    assert mgr.queued_bytes() == 5 * MB


@pytest.mark.asyncio
async def test_rejects_when_total_queued_bytes_would_exceed_the_cap():
    mgr = make_queue_mgr(max_queued_mb=10)
    await mgr.enqueue(make_item("normal", "a", nbytes=8 * MB))

    with pytest.raises(QueueOverCapacity):
        await mgr.enqueue(make_item("normal", "b", nbytes=5 * MB))


@pytest.mark.asyncio
async def test_admits_under_the_cap():
    """CONTROL for the rejection above. Without it, a cap that rejected everything
    unconditionally would pass the test that matters and fail nothing."""
    mgr = make_queue_mgr(max_queued_mb=10)
    await mgr.enqueue(make_item("normal", "a", nbytes=8 * MB))
    assert await mgr.enqueue(make_item("normal", "b", nbytes=1 * MB)) == 2


@pytest.mark.asyncio
async def test_oversized_body_is_admitted_when_the_queue_is_empty():
    """The exemption that stops the cap becoming a deadlock.

    A body larger than the entire ceiling must still be admitted when nothing else
    is waiting. Without this it could never be served under any circumstances — it
    would be refused against an empty queue forever, which is not backpressure, it
    is a permanent refusal that no amount of waiting resolves.
    """
    mgr = make_queue_mgr(max_queued_mb=1)
    assert await mgr.enqueue(make_item("normal", "huge", nbytes=50 * MB)) == 1


@pytest.mark.asyncio
async def test_bytes_are_released_when_an_item_is_dequeued():
    """Released at dequeue, so a long-running proxy does not ratchet its own ceiling
    down to zero. This is the leak that would make the cap look correct on day one
    and refuse everything by day two."""
    mgr = make_queue_mgr(max_queued_mb=10)
    await mgr.enqueue(make_item("normal", "a", nbytes=8 * MB))
    assert mgr.queued_bytes() == 8 * MB

    mgr.start_workers()
    try:
        for _ in range(200):
            await asyncio.sleep(0)
            if mgr.queued_bytes() == 0:
                break
    finally:
        await mgr.stop_workers()

    assert mgr.queued_bytes() == 0, "dequeued bytes must be released back to the ceiling"
    # And the ceiling is genuinely usable again, not merely reported as zero.
    assert await mgr.enqueue(make_item("normal", "b", nbytes=9 * MB)) == 1


@pytest.mark.asyncio
async def test_expired_item_also_releases_its_bytes():
    """An item that expires never reaches the success path. Releasing only there
    would leak the ceiling by exactly the traffic that is already struggling."""
    mgr = make_queue_mgr(max_queued_mb=10)
    item = make_item("normal", "stale", nbytes=8 * MB)
    item.enqueue_time = time.monotonic() - 10_000  # older than max_wait
    await mgr.enqueue(item)
    assert mgr.queued_bytes() == 8 * MB

    mgr.start_workers()
    try:
        for _ in range(200):
            await asyncio.sleep(0)
            if item.future.done():
                break
    finally:
        await mgr.stop_workers()

    assert mgr.queued_bytes() == 0
    with pytest.raises(RequestExpired):
        item.future.result()
