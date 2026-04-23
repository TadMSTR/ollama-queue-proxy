"""Per-client concurrency caps with fairness bound."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .config import ApiKeyConfig

logger = logging.getLogger(__name__)

# Requests from a capped client that have been deferred this many times are
# allowed through unconditionally to prevent livelock.
FAIRNESS_MAX_REENTRIES = 3


@dataclass
class ClientState:
    client_id: str
    cap: int  # 0 = unlimited
    _semaphore: asyncio.Semaphore | None = field(default=None, repr=False)
    inflight: int = 0
    cap_waiting: int = 0

    def __post_init__(self):
        if self.cap > 0:
            self._semaphore = asyncio.Semaphore(self.cap)

    @property
    def is_capped(self) -> bool:
        return self.cap > 0

    async def acquire(self) -> None:
        if self._semaphore is None:
            self.inflight += 1
            return
        self.cap_waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self.cap_waiting = max(0, self.cap_waiting - 1)
        self.inflight += 1

    def release(self) -> None:
        self.inflight = max(0, self.inflight - 1)
        if self._semaphore is not None:
            self._semaphore.release()


class ClientConcurrencyManager:
    """
    Tracks per-client concurrency via async semaphores.

    Clients with max_concurrent=0 (unlimited) are tracked for metrics but never blocked.
    Clients with max_concurrent>0 are blocked at the per-client cap, which must be ≤
    proxy.max_concurrent (validated at config load time).

    Fairness: a request that has been deferred FAIRNESS_MAX_REENTRIES times bypasses
    the semaphore to prevent livelock when a capped client floods its secondary queue.
    """

    def __init__(self, key_configs: list[ApiKeyConfig]) -> None:
        self._states: dict[str, ClientState] = {}
        for key in key_configs:
            self._states[key.client_id] = ClientState(
                client_id=key.client_id,
                cap=key.max_concurrent,
            )

    def get_state(self, client_id: str | None) -> ClientState | None:
        if client_id is None:
            return None
        return self._states.get(client_id)

    async def acquire(self, client_id: str | None, reentries: int = 0) -> None:
        """
        Acquire a concurrency slot for client_id.

        If reentries >= FAIRNESS_MAX_REENTRIES, bypass the semaphore (fairness bound).
        No-op for unknown or unlimited clients.
        """
        state = self.get_state(client_id)
        if state is None:
            return
        if not state.is_capped:
            state.inflight += 1
            return
        if reentries >= FAIRNESS_MAX_REENTRIES:
            # Bypass semaphore to prevent livelock
            state.inflight += 1
            logger.debug(
                "concurrency.fairness_bypass client_id=%s reentries=%d",
                client_id, reentries,
            )
            return
        await state.acquire()

    def release(self, client_id: str | None) -> None:
        state = self.get_state(client_id)
        if state is None:
            return
        state.release()

    def inflight_counts(self) -> dict[str, int]:
        return {cid: s.inflight for cid, s in self._states.items()}

    def cap_waiting_counts(self) -> dict[str, int]:
        return {cid: s.cap_waiting for cid, s in self._states.items()}

    def is_at_cap(self, client_id: str | None) -> bool:
        """Returns True if the client currently has no available semaphore slots."""
        state = self.get_state(client_id)
        if state is None or not state.is_capped:
            return False
        return state.inflight >= state.cap
