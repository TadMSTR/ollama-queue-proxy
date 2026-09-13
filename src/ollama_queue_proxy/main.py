"""FastAPI application entry point and lifespan management."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.config
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import AuthManager, scope_denied
from .cache import EmbeddingCache
from .concurrency import ClientConcurrencyManager
from .config import ApiKeyConfig, Config, load_config
from .middleware import RequestContextMiddleware, get_client_id, parse_priority
from .openai_compat import is_openai_compat_path, rewrite_path, wrap_response
from .proxy import dispatch_request, read_body
from .queue import (
    PriorityQueueManager,
    QueueFull,
    QueueItem,
    QueueOverCapacity,
    QueuePaused,
    RequestExpired,
)
from .routes.queue import router as queue_router
from .routes.status import router as status_router
from .routing import RoutingTable
from .webhooks import WebhookManager, validate_webhook_url

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    config: Config
    auth_manager: AuthManager
    queue_manager: PriorityQueueManager
    webhook_manager: WebhookManager
    http_client: httpx.AsyncClient
    routing_table: RoutingTable
    embedding_cache: EmbeddingCache | None = None
    concurrency_manager: ClientConcurrencyManager | None = None
    start_time: datetime = field(default_factory=lambda: datetime.now(UTC))
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

    # Built unconditionally. It is the only per-host state there is; `routing.strategy`
    # selects how pick() chooses, not whether host state exists. Building it only for
    # model_aware was what left the DEFAULT (round_robin) deployment with no background
    # host polling at all — see the module docstring in routing.py.
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

    queue_manager.start_workers()
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
    except TimeoutError:
        logger.warning("shutdown: drain timeout after %ds", drain_timeout)

    await queue_manager.stop_workers()
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


_KEEP_ALIVE_PATHS = frozenset({"/api/generate", "/api/chat", "/api/embed", "/api/embeddings"})

# Metadata reads: no inference cost, so no reason to spend an admission decision on
# them. These bypass the priority queue and the worker semaphore entirely.
#
# They did not before, and the effect was visible: with max_concurrent small (it is 2
# on the deployment this was found on), a UI polling /api/tags queued behind whatever
# multi-minute generation happened to be running, and appeared to hang. Policy
# machinery must not block reads that cost nothing to serve.
#
# These still AUTHENTICATE — the bypass is of the queue, not of the auth check, which
# runs before this point in proxy_handler. What is given up is the per-client
# concurrency cap on these paths; the ceiling that remains is the shared httpx
# connection pool, and these are cheap reads against a local daemon.
_METADATA_FAST_PATH = frozenset({"/api/tags", "/api/version", "/api/ps", "/api/show", "/"})


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
    path_override: str | None = None,
    key_cfg: ApiKeyConfig | None = None,
) -> JSONResponse:
    """
    Buffer the request body, enqueue it, and await dispatch. Used by both the main
    proxy handler and injection port handlers to share queue/worker logic.

    Before enqueueing:
    - Refuses any key below `inference` scope.
    - Injects keep_alive into request body for the four supported endpoints.
    - Checks embedding cache; cache hits bypass the queue entirely.
    After dispatch:
    - Populates embedding cache on successful 2xx JSONResponse.
    Per-client concurrency cap is enforced inside dispatch_fn via ClientConcurrencyManager.
    """
    from .cache import CACHEABLE_PATHS
    from .proxy import extract_model

    request_id = getattr(request.state, "request_id", "unknown")

    # THE gate that makes `scope: read` mean anything. Every byte that reaches Ollama
    # passes through this function — the main catch-all and every injection listener
    # both funnel here, and dispatch_request has no other caller — so this is one
    # enforcement point rather than one per entry path, and a new entry path cannot
    # quietly acquire an exemption by forgetting to add a check.
    #
    # The rule is simply "if there is a key, its scope is enforced", which lands
    # correctly on all three paths with no flag to get wrong:
    #   - main port, auth off  -> key_cfg is None; nothing to enforce (the documented
    #     auth-off caveat, unchanged from how `management` already behaved)
    #   - main port, auth on   -> authenticate() always yields a key or an error
    #   - injection listener   -> key_cfg is ALWAYS a real config entry, resolved from
    #     `inject_as` at startup and independent of auth.enabled. Enforced there too,
    #     for the same reason max_priority already is: that identity is declared by the
    #     operator, not asserted by the caller, so the policy on it is meaningful even
    #     with auth off. Skipping it would leave a read-only key able to buy inference
    #     merely by being pointed at a listener port.
    #
    # Note the metadata fast-path below is NOT an exception. `read` cannot reach Ollama
    # at all, not even /api/tags: the rule an operator has to hold in their head is
    # "read sees the proxy's own state, nothing upstream", and a carve-out for cheap
    # reads would make it "...except these five paths" for no use case this build has.
    #
    # Placed before read_body so an unauthorised request is refused without buffering
    # its body — the same authorisation-before-input ordering as routes/queue.py.
    if key_cfg is not None and not key_cfg.allows("inference"):
        return scope_denied(request, "inference")

    body, body_err = await read_body(request, state.config.proxy.max_request_body_mb)
    if body_err:
        return body_err

    path = path_override if path_override is not None else request.url.path

    # Metadata fast path — straight to dispatch, no queue, no semaphore. Placed here
    # rather than in proxy_handler so the injection ports get it too: they share this
    # function precisely so queue behaviour cannot diverge between the two entry points.
    if path in _METADATA_FAST_PATH:
        return await dispatch_request(
            request=request,
            body=body,
            client_id=client_id,
            config=state.config,
            client=state.http_client,
            routing_table=state.routing_table,
            path_override=path_override,
        )

    # keep_alive injection — runs before cache check so cached responses also reflect
    # the injected value (though for embeddings keep_alive has no effect upstream)
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
            cached = await state.embedding_cache.get(path, cache_body_data, cache_model, client_id)
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
    # get_running_loop() replaces deprecated get_event_loop() — the latter raises
    # RuntimeError in Python 3.12+ when called outside a running event loop.
    future: asyncio.Future = asyncio.get_running_loop().create_future()

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
                client=state.http_client,
                routing_table=state.routing_table,
                path_override=path_override,
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
        nbytes=len(body) if body else 0,
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
    except QueueOverCapacity as e:
        retry_after = state.queue_manager.retry_after(e.tier)
        if client_id:
            cs = state.client_stats.setdefault(
                client_id, {"description": None, "processed": 0, "rejected": 0}
            )
            cs["rejected"] = cs.get("rejected", 0) + 1
        return JSONResponse(
            status_code=e.status_code,
            content={"error": "queue over capacity (bytes)", "request_id": request_id},
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
        # Never fail a user request because of a cache write error.
        with suppress(Exception):
            await state.embedding_cache.set(
                path, cache_body_data, cache_model, response.body, client_id
            )

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

    # OpenAI-compat path handling: rewrite path before enqueue, wrap response after
    compat_path = "/" + path
    if is_openai_compat_path(compat_path):
        native_path = rewrite_path(compat_path)
        response = await _enqueue_request(
            request=request,
            client_id=client_id,
            tier=tier,
            state=state,
            path_override=native_path,
            key_cfg=key_cfg,
        )
        # Only wrap successful JSON responses; pass through errors unchanged
        if isinstance(response, JSONResponse) and response.status_code == 200:
            ollama_body = json.loads(response.body)
            wrapped = wrap_response(ollama_body)
            return JSONResponse(content=wrapped, status_code=200)
        return response

    return await _enqueue_request(
        request=request,
        client_id=client_id,
        tier=tier,
        state=state,
        key_cfg=key_cfg,
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

    all_servers = [main_server, *injection_servers]

    async def serve_all():
        tasks = [asyncio.create_task(s.serve()) for s in all_servers]
        # When any server exits (e.g. SIGTERM to main), signal all to stop
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for s in all_servers:
            s.should_exit = True
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(serve_all())
