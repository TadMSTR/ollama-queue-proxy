"""FastAPI application entry point and lifespan management."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.config
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import AuthManager
from .cache import EmbeddingCache
from .concurrency import ClientConcurrencyManager
from .config import Config, load_config
from .hosts import HostManager
from .middleware import RequestContextMiddleware, get_client_id, parse_priority
from .proxy import dispatch_request, read_body
from .queue import PriorityQueueManager, QueueFull, QueueItem, QueuePaused, RequestExpired
from .routing import RoutingTable
from .routes.queue import router as queue_router
from .routes.status import router as status_router
from .webhooks import WebhookManager, validate_webhook_url

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    config: Config
    auth_manager: AuthManager
    host_manager: HostManager
    queue_manager: PriorityQueueManager
    webhook_manager: WebhookManager
    http_client: httpx.AsyncClient
    routing_table: RoutingTable | None = None
    embedding_cache: EmbeddingCache | None = None
    concurrency_manager: ClientConcurrencyManager | None = None
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    client_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    shutting_down: bool = False


def _configure_logging(config: Config) -> None:
    level = config.logging.level.upper()
    if config.logging.format == "json":
        fmt = '{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":"%(message)s"}'
    else:
        fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt)


def _warn_open_binding(config: Config) -> None:
    if not config.auth.enabled and config.proxy.host == "0.0.0.0":
        logger.warning(
            "SECURITY WARNING: auth.enabled is false and proxy is binding to 0.0.0.0. "
            "Any host that can reach port %d has unauthenticated Ollama access. "
            "Set auth.enabled: true if exposing beyond localhost.",
            config.proxy.port,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .injection import set_shared_state

    config = load_config()
    _configure_logging(config)

    # Validate webhook URL for SSRF at startup
    if config.webhooks.enabled and config.webhooks.url:
        try:
            validate_webhook_url(config.webhooks.url, config.webhooks.allowed_hosts)
        except ValueError as e:
            import sys
            print(f"FATAL: {e}", file=sys.stderr)
            sys.exit(1)

    _warn_open_binding(config)

    http_client = httpx.AsyncClient()
    host_manager = HostManager(config.ollama)
    auth_manager = AuthManager(config.auth)
    queue_manager = PriorityQueueManager(config.queue, config.proxy.max_concurrent)
    webhook_manager = WebhookManager(config.webhooks, http_client)

    # Wire webhook events from queue
    async def on_queue_event(event: str, tier: str | None = None, **kwargs):
        await webhook_manager.fire(event, tier=tier, **kwargs)

    queue_manager.add_event_callback(on_queue_event)

    # Pre-populate client stats descriptions from key config
    client_stats: dict[str, dict] = {}
    for key in config.auth.keys:
        client_stats[key.client_id] = {
            "description": key.description,
            "processed": 0,
            "rejected": 0,
        }

    # Build routing table if model-aware strategy is configured
    routing_table: RoutingTable | None = None
    if config.routing.strategy != "round_robin":
        routing_table = RoutingTable(config.ollama, config.routing, http_client)
        await routing_table.startup_probe()

    # Build embedding cache if enabled
    embedding_cache: EmbeddingCache | None = None
    if config.embedding_cache.enabled:
        embedding_cache = EmbeddingCache(config.embedding_cache)
        await embedding_cache.startup()

    concurrency_manager: ClientConcurrencyManager | None = None
    if any(k.max_concurrent > 0 for k in config.auth.keys):
        concurrency_manager = ClientConcurrencyManager(config.auth.keys)

    state = AppState(
        config=config,
        auth_manager=auth_manager,
        host_manager=host_manager,
        queue_manager=queue_manager,
        webhook_manager=webhook_manager,
        http_client=http_client,
        routing_table=routing_table,
        embedding_cache=embedding_cache,
        concurrency_manager=concurrency_manager,
        client_stats=client_stats,
    )
    app.state.oqp = state
    set_shared_state(state)  # make available to injection apps

    await host_manager.startup_check(http_client)
    queue_manager.start_workers()
    await host_manager.start_background_checks(http_client)
    if routing_table:
        routing_table.start_background_pollers()

    logger.info(
        "ollama-queue-proxy started host=%s port=%d auth=%s injection_listeners=%d",
        config.proxy.host,
        config.proxy.port,
        config.auth.enabled,
        len(config.client_injection.listeners),
    )

    yield

    # Graceful shutdown
    logger.info("shutdown: stopping new requests")
    state.shutting_down = True

    drain_timeout = config.proxy.drain_timeout
    logger.info("shutdown: draining in-flight requests (timeout=%ds)", drain_timeout)
    try:
        await asyncio.wait_for(queue_manager.drain(), timeout=drain_timeout)
    except asyncio.TimeoutError:
        logger.warning("shutdown: drain timeout after %ds", drain_timeout)

    await queue_manager.stop_workers()
    await host_manager.stop()
    if routing_table:
        await routing_table.stop()
    if embedding_cache:
        await embedding_cache.close()
    await http_client.aclose()
    set_shared_state(None)
    logger.info("shutdown: complete")


app = FastAPI(
    title="ollama-queue-proxy",
    description="Drop-in HTTP proxy for Ollama with priority queuing, auth, and failover",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(RequestContextMiddleware)
app.include_router(status_router)
app.include_router(queue_router)


_KEEP_ALIVE_PATHS = frozenset({
    "/api/generate", "/api/chat", "/api/embed", "/api/embeddings"
})


def _inject_keep_alive(body: bytes, cfg_default: str, override: bool, max_body_mb: int) -> bytes:
    """
    Parse JSON body and inject keep_alive if needed.
    Returns the (possibly modified) body. Never logs body content (FLAG E).
    Skips mutation if body exceeds max_body_mb to avoid memory pressure.
    """
    max_bytes = max_body_mb * 1024 * 1024
    if len(body) > max_bytes:
        return body
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body
    if not isinstance(data, dict):
        return body
    if override or "keep_alive" not in data:
        data["keep_alive"] = cfg_default
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


async def _enqueue_request(
    request: Request,
    client_id: str | None,
    tier: str,
    state: AppState,
    reentries: int = 0,
) -> JSONResponse:
    """
    Buffer the request body, enqueue it, and await dispatch. Used by both the main
    proxy handler and injection port handlers to share queue/worker logic.

    Before enqueueing:
    - Injects keep_alive into request body for the four supported endpoints.
    - Checks embedding cache; cache hits bypass the queue entirely.
    After dispatch:
    - Populates embedding cache on successful 2xx JSONResponse.
    Per-client concurrency cap is enforced inside dispatch_fn via ClientConcurrencyManager.
    """
    from .cache import CACHEABLE_PATHS
    from .proxy import extract_model

    request_id = getattr(request.state, "request_id", "unknown")

    body, body_err = await read_body(request, state.config.proxy.max_request_body_mb)
    if body_err:
        return body_err

    # keep_alive injection — runs before cache check so cached responses also reflect
    # the injected value (though for embeddings keep_alive has no effect upstream)
    path = request.url.path
    ka_cfg = state.config.keep_alive
    if path in _KEEP_ALIVE_PATHS:
        body = _inject_keep_alive(
            body, ka_cfg.default, ka_cfg.override, state.config.proxy.max_request_body_mb
        )

    # Embedding cache — parsed body and model extracted once, reused for set on miss
    cache_body_data: dict | None = None
    cache_model: str = ""

    if state.embedding_cache is not None and path in CACHEABLE_PATHS:
        try:
            parsed = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            cache_body_data = parsed
            cache_model = extract_model(body) or ""
            cached = await state.embedding_cache.get(
                path, cache_body_data, cache_model, client_id
            )
            if cached is not None:
                # Cache hit — still track stats, skip queue
                if client_id:
                    cs = state.client_stats.setdefault(
                        client_id, {"description": None, "processed": 0, "rejected": 0}
                    )
                    cs["processed"] = cs.get("processed", 0) + 1
                return JSONResponse(
                    status_code=200,
                    content=json.loads(cached),
                    headers={"X-Cache": "HIT"},
                )

    enqueue_time = time.monotonic()
    future: asyncio.Future = asyncio.get_event_loop().create_future()

    conc_mgr = state.concurrency_manager

    async def dispatch_fn():
        # Per-client concurrency cap: acquire slot before upstream, release after
        if conc_mgr is not None:
            await conc_mgr.acquire(client_id, reentries=reentries)
        try:
            return await dispatch_request(
                request=request,
                body=body,
                client_id=client_id,
                config=state.config,
                host_manager=state.host_manager,
                client=state.http_client,
                routing_table=state.routing_table,
            )
        finally:
            if conc_mgr is not None:
                conc_mgr.release(client_id)

    item = QueueItem(
        tier=tier,
        enqueue_time=enqueue_time,
        request_id=request_id,
        future=future,
        dispatch_fn=dispatch_fn,
    )

    try:
        position = await state.queue_manager.enqueue(item)
    except QueueFull as e:
        retry_after = state.queue_manager.retry_after(e.tier)
        if client_id:
            cs = state.client_stats.setdefault(
                client_id, {"description": None, "processed": 0, "rejected": 0}
            )
            cs["rejected"] = cs.get("rejected", 0) + 1
        return JSONResponse(
            status_code=e.status_code,
            content={"error": "queue full", "request_id": request_id},
            headers={"Retry-After": str(retry_after)},
        )
    except QueuePaused as e:
        return JSONResponse(
            status_code=503,
            content={"error": f"queue tier '{e.tier}' is paused", "request_id": request_id},
        )

    try:
        response = await future
    except RequestExpired as e:
        return JSONResponse(
            status_code=503,
            content={"error": "request expired in queue", "request_id": e.request_id},
        )
    except Exception as e:
        logger.error("dispatch.error request_id=%s error=%s", request_id, e)
        return JSONResponse(
            status_code=503,
            content={"error": "upstream error", "request_id": request_id},
        )

    wait_ms = int((time.monotonic() - enqueue_time) * 1000)
    waited = wait_ms > 0 and position > 1

    if client_id:
        cs = state.client_stats.setdefault(
            client_id, {"description": None, "processed": 0, "rejected": 0}
        )
        cs["processed"] = cs.get("processed", 0) + 1

    # Cache successful embedding responses for future hits
    if (
        state.embedding_cache is not None
        and cache_body_data is not None
        and response.status_code == 200
        and isinstance(response, JSONResponse)
    ):
        try:
            await state.embedding_cache.set(
                path, cache_body_data, cache_model, response.body, client_id
            )
        except Exception:
            pass  # never fail a user request due to cache write errors

    response.headers["X-Queue-Wait-Time"] = str(wait_ms)
    if waited:
        response.headers["X-Queue-Position"] = str(position)

    return response


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_handler(request: Request, path: str):
    """Catch-all proxy handler — forwards all Ollama API requests."""
    state: AppState = app.state.oqp
    request_id = getattr(request.state, "request_id", "unknown")

    if state.shutting_down:
        return JSONResponse(
            status_code=503,
            content={"error": "proxy is shutting down", "request_id": request_id},
        )

    # Authenticate
    key_cfg, auth_err = await state.auth_manager.authenticate(request)
    if auth_err:
        return auth_err

    # Resolve client ID — from key config (authoritative) or caller header
    client_id: str | None
    if state.config.auth.enabled and key_cfg:
        client_id = key_cfg.client_id
    else:
        client_id = get_client_id(request)

    # Parse and enforce priority ceiling
    requested_priority = parse_priority(request)
    tier = state.auth_manager.enforce_priority_ceiling(requested_priority, key_cfg)

    return await _enqueue_request(
        request=request,
        client_id=client_id,
        tier=tier,
        state=state,
    )


def run():
    import uvicorn

    config = load_config()

    # Build the set of uvicorn servers: 1 main + N injection listeners
    main_cfg = uvicorn.Config(
        "ollama_queue_proxy.main:app",
        host=config.proxy.host,
        port=config.proxy.port,
        log_config=None,
    )
    main_server = uvicorn.Server(main_cfg)

    injection_servers: list[uvicorn.Server] = []
    if config.client_injection.listeners:
        from .injection import make_injection_app

        key_map = {k.client_id: k for k in config.auth.keys}
        for listener in config.client_injection.listeners:
            key_cfg = key_map[listener.inject_as]
            inj_app = make_injection_app(listener.inject_as, key_cfg)
            inj_cfg = uvicorn.Config(
                inj_app,
                host=listener.bind,
                port=listener.listen_port,
                log_config=None,
            )
            injection_servers.append(uvicorn.Server(inj_cfg))
            logger.info(
                "injection.listener registered inject_as=%s port=%d bind=%s",
                listener.inject_as,
                listener.listen_port,
                listener.bind,
            )

    all_servers = [main_server] + injection_servers

    async def serve_all():
        tasks = [asyncio.create_task(s.serve()) for s in all_servers]
        # When any server exits (e.g. SIGTERM to main), signal all to stop
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for s in all_servers:
            s.should_exit = True
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(serve_all())
