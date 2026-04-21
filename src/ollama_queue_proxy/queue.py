"""Priority queue: three-tier asyncio queues with event-based worker."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import QueueConfig

logger = logging.getLogger(__name__)

TIERS = ("high", "normal", "low")


@dataclass
class QueueItem:
    tier: str
    enqueue_time: float
    request_id: str
    future: asyncio.Future
    dispatch_fn: Callable[[], Awaitable[Any]]
    position: int = 0


@dataclass
class TierStats:
    processed: int = 0
    rejected: int = 0
    expired: int = 0
    # Rolling window of recent wait times (last 20)
    recent_waits: deque = field(default_factory=lambda: deque(maxlen=20))

    def mean_wait(self) -> float:
        if len(self.recent_waits) < 3:
            return 5.0
        return sum(self.recent_waits) / len(self.recent_waits)


class PriorityQueueManager:
    def __init__(self, config: QueueConfig, max_concurrent: int) -> None:
        self._config = config
        tier_cfgs = {
            "high": config.high,
            "normal": config.normal,
            "low": config.low,
        }
        self._queues: dict[str, asyncio.Queue] = {
            t: asyncio.Queue(maxsize=tier_cfgs[t].max_depth) for t in TIERS
        }
        self._max_waits = {t: tier_cfgs[t].max_wait for t in TIERS}
        self._watermark_pcts = {t: tier_cfgs[t].high_watermark_pct for t in TIERS}
        self._paused: set[str] = set()
        self._stats: dict[str, TierStats] = {t: TierStats() for t in TIERS}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._active = 0
        self._active_lock = asyncio.Lock()
        self._has_items = asyncio.Event()
        self._worker_tasks: list[asyncio.Task] = []
        self._overflow_code = config.overflow_status_code
        self._watermark_fired: set[str] = set()
        self._event_callbacks: list[Callable] = []

    def add_event_callback(self, cb: Callable) -> None:
        self._event_callbacks.append(cb)

    async def _fire_event(self, event: str, tier: str | None = None, **kwargs) -> None:
        for cb in self._event_callbacks:
            asyncio.create_task(cb(event, tier=tier, **kwargs))

    def start_workers(self) -> None:
        for _ in range(self._max_concurrent):
            t = asyncio.create_task(self._worker())
            self._worker_tasks.append(t)

    async def stop_workers(self) -> None:
        for t in self._worker_tasks:
            t.cancel()
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)

    async def enqueue(self, item: QueueItem) -> int:
        """
        Enqueue item. Returns queue position (1-based) or raises QueueFull.
        Raises QueuePaused if tier is paused.
        """
        tier = item.tier
        q = self._queues[tier]

        if tier in self._paused:
            raise QueuePaused(tier)

        if q.full():
            self._stats[tier].rejected += 1
            await self._fire_event("queue.full", tier=tier, client_id=item.request_id)
            raise QueueFull(tier, self._overflow_code)

        position = q.qsize() + 1
        item.position = position
        await q.put(item)
        self._has_items.set()

        # Check high watermark
        tier_cfg = getattr(self._config, tier)
        pct = (q.qsize() / tier_cfg.max_depth) * 100
        if pct >= self._watermark_pcts[tier] and tier not in self._watermark_fired:
            self._watermark_fired.add(tier)
            await self._fire_event("queue.high_watermark", tier=tier, queue_depth=q.qsize())
        elif pct < self._watermark_pcts[tier]:
            self._watermark_fired.discard(tier)

        return position

    async def _worker(self) -> None:
        while True:
            await self._has_items.wait()
            async with self._semaphore:
                item = None
                for tier in TIERS:
                    try:
                        item = self._queues[tier].get_nowait()
                        break
                    except asyncio.QueueEmpty:
                        pass

                if item is None:
                    # Check if all queues truly empty
                    if all(q.empty() for q in self._queues.values()):
                        self._has_items.clear()
                    continue

                async with self._active_lock:
                    self._active += 1

                tier = item.tier
                age = time.monotonic() - item.enqueue_time
                if age > self._max_waits[tier]:
                    self._stats[tier].expired += 1
                    logger.warning(
                        "queue.expired tier=%s request_id=%s age=%.1fs",
                        tier, item.request_id, age,
                    )
                    item.future.set_exception(
                        RequestExpired(tier, item.request_id)
                    )
                else:
                    try:
                        result = await item.dispatch_fn()
                        if not item.future.done():
                            item.future.set_result(result)
                        wait_ms = (time.monotonic() - item.enqueue_time) * 1000
                        self._stats[tier].recent_waits.append(wait_ms / 1000)
                        self._stats[tier].processed += 1
                    except Exception as e:
                        if not item.future.done():
                            item.future.set_exception(e)

                async with self._active_lock:
                    self._active -= 1

                # Check if queues drained
                if all(q.empty() for q in self._queues.values()):
                    await self._fire_event("queue.drained", tier=None)

    def queue_depths(self) -> dict[str, int]:
        return {t: self._queues[t].qsize() for t in TIERS}

    def active_count(self) -> int:
        return self._active

    def stats(self) -> dict[str, TierStats]:
        return self._stats

    def retry_after(self, tier: str) -> int:
        q = self._queues[tier]
        depth = q.qsize()
        mean_wait = self._stats[tier].mean_wait()
        return math.ceil(depth / max(self._max_concurrent, 1) * mean_wait)

    def pause(self, tier: str | None) -> None:
        tiers = TIERS if tier is None else (tier,)
        for t in tiers:
            self._paused.add(t)

    def resume(self, tier: str | None) -> None:
        tiers = TIERS if tier is None else (tier,)
        for t in tiers:
            self._paused.discard(t)

    async def flush(self, tier: str | None) -> int:
        """Drop all pending items in tier(s). Returns count of dropped items."""
        tiers = TIERS if tier is None else (tier,)
        dropped = 0
        for t in tiers:
            while not self._queues[t].empty():
                try:
                    item = self._queues[t].get_nowait()
                    item.future.set_exception(QueueFlushed(t))
                    dropped += 1
                except asyncio.QueueEmpty:
                    break
        return dropped

    async def drain(self) -> None:
        """Wait until all queues are empty."""
        while any(not q.empty() for q in self._queues.values()):
            await asyncio.sleep(0.1)


class QueueFull(Exception):
    def __init__(self, tier: str, status_code: int) -> None:
        self.tier = tier
        self.status_code = status_code


class QueuePaused(Exception):
    def __init__(self, tier: str) -> None:
        self.tier = tier


class QueueFlushed(Exception):
    def __init__(self, tier: str) -> None:
        self.tier = tier


class RequestExpired(Exception):
    def __init__(self, tier: str, request_id: str) -> None:
        self.tier = tier
        self.request_id = request_id
