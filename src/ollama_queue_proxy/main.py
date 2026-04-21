"""FastAPI application entry point and lifespan management."""

from __future__ import annotations

import asyncio
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
from .config import Config, load_config
from .hosts import HostManager
from .middleware import RequestContextMiddleware, get_client_id, parse_priority
from .proxy import dispatch_request, read_body
from .queue import PriorityQueueManager, QueueFull, QueueItem, QueuePaused, RequestExpired
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

    state = AppState(
        config=config,
        auth_manager=auth_manager,
        host_manager=host_manager,
        queue_manager=queue_manager,
        webhook_manager=webhook_manager,
        http_client=http_client,
        client_stats=client_stats,
    )
    app.state.oqp = state

    await host_manager.startup_check(http_client)
    queue_manager.start_workers()
    await host_manager.start_background_checks(http_client)

    logger.info(
        "ollama-queue-proxy started host=%s port=%d auth=%s",
        config.proxy.host,
        config.proxy.port,
        config.auth.enabled,
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
    await http_client.aclose()
    logger.info("shutdown: complete")


app = FastAPI(
    title="ollama-queue-proxy",
    description="Drop-in HTTP proxy for Ollama with priority queuing, auth, and failover",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(RequestContextMiddleware)
app.include_router(status_router)
app.include_router(queue_router)


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

    # Parse and enforce priority
    requested_priority = parse_priority(request)
    tier = state.auth_manager.enforce_priority_ceiling(requested_priority, key_cfg)

    # Buffer request body
    body, body_err = await read_body(request, state.config.proxy.max_request_body_mb)
    if body_err:
        return body_err

    enqueue_time = time.monotonic()
    future: asyncio.Future = asyncio.get_event_loop().create_future()

    async def dispatch_fn():
        return await dispatch_request(
            request=request,
            body=body,
            client_id=client_id,
            config=state.config,
            host_manager=state.host_manager,
            client=state.http_client,
        )

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

    # Wait for dispatch
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

    # Track client stats
    if client_id:
        cs = state.client_stats.setdefault(
            client_id, {"description": None, "processed": 0, "rejected": 0}
        )
        cs["processed"] = cs.get("processed", 0) + 1

    # Inject queue timing headers (both JSONResponse and StreamingResponse inherit Response)
    response.headers["X-Queue-Wait-Time"] = str(wait_ms)
    if waited:
        response.headers["X-Queue-Position"] = str(position)

    return response


def run():
    import uvicorn
    config = load_config()
    uvicorn.run(
        "ollama_queue_proxy.main:app",
        host=config.proxy.host,
        port=config.proxy.port,
        log_config=None,
    )
