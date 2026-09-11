"""Host state and model-aware routing: the single source of truth for per-host facts.

WHY THIS IS ONE STRUCTURE AND NOT TWO
-------------------------------------
Until 0.4.0 this module and `hosts.py` both tracked per-host URL, reachability and
model inventory, with *different refresh rules*, and `proxy.py` selected through one
while failing over through the other. They disagreed in normal operation:

  - `HostManager._health_loop` re-probed a host only `if not host.healthy`. A host
    healthy at startup was therefore never polled again and its model list was frozen
    for the lifetime of the process — pull a new model, and the proxy could not see it
    without a restart.
  - `RoutingTable` polled every host on its own interval, so its view stayed current.
  - `RoutingTable` was built *only* when `routing.strategy != "round_robin"`. Since
    `round_robin` is the DEFAULT, the default deployment had no polling table at all:
    its only host state was the one that never refreshed.

So the two structures were not merely redundant, they were wrong in opposite
directions depending on a config value. Patching the `_health_loop` predicate alone
would have fixed one symptom and left the duplication that produced it, and the
divergence returns on the next change. `hosts.py` is gone; this is the only host state.

A consequence worth stating: the routing table is now built unconditionally. Strategy
selects how `pick()` chooses, not whether host state exists.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from .config import OllamaConfig, RoutingConfig

logger = logging.getLogger(__name__)


@dataclass
class HostRoutingState:
    """Everything tracked per Ollama host."""

    url: str
    name: str
    weight: int
    model_sync_interval: int

    # Models INSTALLED on the host — what `/api/tags` reports, i.e. present on disk.
    #
    # This was called `loaded_models`, which is a different thing: "loaded" means
    # resident in VRAM, which is `/api/ps`, which this project does not call anywhere.
    # The name is why the gap went unnoticed for so long — it matched what the README
    # claimed, so nothing looked wrong. A name that lies is a defect even when the
    # behaviour behind it is acceptable. Routing on installed models is a reasonable
    # thing to do; it is just not cold-start avoidance, and must not be described as it.
    installed_models: set[str] = field(default_factory=set)

    reachable: bool = True
    last_checked: datetime | None = None
    requests_handled: int = 0
    failures: int = 0


class RoutingTable:
    """
    Maintains a live map of (host → installed_models) via per-host background pollers,
    and owns per-host reachability and counters.

    Weighted round-robin is deterministic: a counter advances on each pick call
    and wraps around the weight-expanded host list.
    """

    def __init__(
        self,
        ollama_config: OllamaConfig,
        routing_config: RoutingConfig,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._routing_cfg = routing_config
        self._client = http_client
        self._poll_timeout = routing_config.model_poll_timeout
        self._lock = asyncio.Lock()

        self._states: dict[str, HostRoutingState] = {
            h.name: HostRoutingState(
                url=h.url,
                name=h.name,
                weight=h.weight,
                model_sync_interval=h.model_sync_interval,
            )
            for h in ollama_config.hosts
        }

        # `ollama.health_check_interval` drove the old HostManager loop. There is now one
        # poller per host on `model_sync_interval`, so that key no longer does anything.
        # Warned about rather than silently ignored: a config value that stopped being
        # read is indistinguishable from one being honoured, and the operator who set it
        # deliberately is exactly the person who will not find out otherwise.
        if ollama_config.health_check_interval != 30:
            logger.warning(
                "config.deprecated key=ollama.health_check_interval value=%d — superseded "
                "by ollama.hosts[].model_sync_interval, which now drives the only host "
                "poller. This value is not used.",
                ollama_config.health_check_interval,
            )

        # Deterministic weighted round-robin: counter increments on every pick
        self._rr_counter: int = 0

        # Metrics counters
        self.routing_decisions: dict[str, int] = {
            "model_match": 0,
            "round_robin": 0,
            "fallback": 0,
        }

        self._poll_tasks: list[asyncio.Task] = []

    # -- accessors -----------------------------------------------------------

    @property
    def hosts(self) -> list[HostRoutingState]:
        """All host states, in configured order."""
        return list(self._states.values())

    def get(self, host_name: str) -> HostRoutingState | None:
        """Look up one host's state. Public: proxy.py reached into `_states` before."""
        return self._states.get(host_name)

    # -- polling -------------------------------------------------------------

    async def startup_probe(self) -> None:
        """
        Synchronous initial poll of all hosts. Fail-fast if no host responds.
        Called before accepting requests.
        """
        await asyncio.gather(
            *[self._poll_host(state) for state in self._states.values()],
            return_exceptions=True,
        )
        reachable_count = sum(1 for state in self._states.values() if state.reachable)
        if reachable_count == 0:
            import sys

            print(
                "FATAL: routing startup probe — no Ollama host responded to /api/tags. "
                "Check that at least one host in ollama.hosts is reachable.",
                file=sys.stderr,
            )
            sys.exit(1)

        for state in self._states.values():
            logger.info(
                "routing.startup_probe host=%s reachable=%s models=%d",
                state.name,
                state.reachable,
                len(state.installed_models),
            )

    def start_background_pollers(self) -> None:
        for state in self._states.values():
            task = asyncio.create_task(self._poll_loop(state))
            self._poll_tasks.append(task)

    async def stop(self) -> None:
        for task in self._poll_tasks:
            task.cancel()
        if self._poll_tasks:
            await asyncio.gather(*self._poll_tasks, return_exceptions=True)
        self._poll_tasks.clear()

    async def _poll_loop(self, state: HostRoutingState) -> None:
        # Every host, every interval, regardless of current reachability. The old
        # health loop's `if not host.healthy` predicate is the defect this replaces:
        # it made recovery observable but never refreshed a host that stayed up.
        while True:
            await asyncio.sleep(state.model_sync_interval)
            await self._poll_host(state)

    async def _poll_host(self, state: HostRoutingState) -> None:
        try:
            resp = await self._client.get(
                f"{state.url}/api/tags",
                timeout=self._poll_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            models = {m["name"] for m in data.get("models", [])}
            async with self._lock:
                was_unreachable = not state.reachable
                state.installed_models = models
                state.reachable = True
                state.last_checked = datetime.now(UTC)
            if was_unreachable:
                logger.warning("host.recovered name=%s models=%d", state.name, len(models))
            else:
                logger.debug("routing.poll host=%s models=%d", state.name, len(models))
        except Exception as e:
            async with self._lock:
                was_reachable = state.reachable
                state.reachable = False
                state.last_checked = datetime.now(UTC)
            if was_reachable:
                logger.warning("host.unhealthy name=%s error=%s", state.name, e)
            else:
                logger.debug("routing.poll_failed host=%s error=%s", state.name, e)

    # -- mutation ------------------------------------------------------------

    def mark_unhealthy(self, state: HostRoutingState, error: str) -> None:
        """Record an upstream failure observed by the request path."""
        state.reachable = False
        state.failures += 1
        logger.warning("host.failure name=%s error=%s", state.name, error)

    def invalidate(self, host_name: str, model: str) -> None:
        """
        Fast-path invalidation: remove model from host's installed set immediately
        when upstream returns 'model not found', so the next request routes elsewhere
        rather than waiting for the next poll.
        """
        state = self._states.get(host_name)
        if state:
            state.installed_models.discard(model)
            logger.debug("routing.invalidated host=%s model=%s", host_name, model)

    # -- selection -----------------------------------------------------------

    def _candidates(self) -> list[HostRoutingState]:
        """
        Reachable hosts, or ALL hosts if none are reachable.

        The fallback is deliberate. `reachable` is a cached observation and can be
        stale-false — a single failed poll against a host that has since recovered.
        Returning no candidate in that state would black-hole the proxy into 503s
        while a working host sat idle. Handing back every host instead costs one
        failed attempt in a genuine total outage, which then 503s anyway, and
        costs nothing when the flag is merely out of date.
        """
        reachable = [s for s in self._states.values() if s.reachable]
        return reachable or list(self._states.values())

    def pick(self, model: str | None) -> HostRoutingState | None:
        """
        Pick a host using the configured strategy.

        - model_aware: prefer hosts with the model installed; fall back per
          routing.fallback
        - round_robin: weighted round-robin across candidates (ignores the model table)

        Returns None only if there are no hosts configured at all.
        """
        strategy = self._routing_cfg.strategy

        if strategy == "model_aware" and model:
            return self._pick_model_aware(model)

        # Previously this passed every host, reachable or not, so round_robin handed
        # back known-down hosts as readily as live ones and burned a failover attempt
        # on each. Preferring reachable candidates is the same rule model_aware
        # already applied.
        result = self._pick_round_robin(self._candidates())
        if result:
            self.routing_decisions["round_robin"] += 1
        return result

    def _pick_model_aware(self, model: str) -> HostRoutingState | None:
        candidates = self._candidates()
        with_model = [s for s in candidates if model in s.installed_models]

        if with_model:
            result = self._pick_round_robin(with_model)
            if result:
                self.routing_decisions["model_match"] += 1
            return result

        # Fall back — no candidate has the model installed
        fallback = self._routing_cfg.fallback
        if fallback == "any_healthy":
            result = self._pick_round_robin(candidates)
            if result:
                self.routing_decisions["fallback"] += 1
            return result

        return None

    def _pick_round_robin(self, candidates: list[HostRoutingState]) -> HostRoutingState | None:
        """
        Deterministic weighted round-robin over the given candidates.
        Builds a weight-expanded sequence and selects by counter modulo total weight.
        """
        if not candidates:
            return None

        # Build the weighted sequence (deterministic, not stochastic)
        weighted: list[HostRoutingState] = []
        for state in candidates:
            weighted.extend([state] * state.weight)

        if not weighted:
            return None

        idx = self._rr_counter % len(weighted)
        self._rr_counter += 1
        return weighted[idx]

    # -- metrics -------------------------------------------------------------

    def host_model_counts(self) -> dict[str, int]:
        """Return {host_name: installed_model_count} for metrics."""
        return {name: len(s.installed_models) for name, s in self._states.items()}

    def installed_models_by_host(self) -> dict[str, set[str]]:
        """Return {host_name: set_of_model_names} snapshot for metrics."""
        return {name: set(s.installed_models) for name, s in self._states.items()}
