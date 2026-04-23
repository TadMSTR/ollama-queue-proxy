"""Core async HTTP proxy with streaming, body buffering, and failover."""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Config
from .hosts import HostManager, OllamaHost
from .routing import RoutingTable

logger = logging.getLogger(__name__)

# Ollama model management endpoints — blocked by default
_MODEL_MANAGEMENT_PATHS = {
    "/api/pull",
    "/api/push",
    "/api/delete",
    "/api/create",
    "/api/copy",
}

# Headers to strip before forwarding to Ollama
_STRIP_REQUEST_HEADERS = {
    "x-queue-priority",
    "authorization",  # proxy auth must NOT reach Ollama
    "host",
    "content-length",  # httpx will recalculate
    "transfer-encoding",
}


def extract_model(body: bytes) -> str | None:
    """Extract the 'model' field from a JSON request body."""
    if not body:
        return None
    try:
        data = json.loads(body)
        return data.get("model") if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


async def read_body(request: Request, max_mb: int) -> tuple[bytes, JSONResponse | None]:
    """
    Buffer the full request body. Returns (body_bytes, None) or (b'', error_response).
    Checks Content-Length first; falls back to incremental read with size check.
    """
    max_bytes = max_mb * 1024 * 1024
    request_id = getattr(request.state, "request_id", "unknown")

    content_length = request.headers.get("Content-Length")
    if content_length:
        try:
            cl = int(content_length)
            if cl > max_bytes:
                return b"", JSONResponse(
                    status_code=413,
                    content={"error": "request body too large", "request_id": request_id},
                )
        except ValueError:
            pass

    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return b"", JSONResponse(
                status_code=413,
                content={"error": "request body too large", "request_id": request_id},
            )
        chunks.append(chunk)
    return b"".join(chunks), None


async def _proxy_to_host(
    host: OllamaHost,
    method: str,
    path: str,
    query: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
    client: httpx.AsyncClient,
) -> AsyncIterator[bytes] | tuple[int, dict, bytes]:
    """
    Send request to a specific host. Returns a streaming iterator for streaming responses,
    or (status_code, headers, body) for non-streaming.
    This is used internally by dispatch_request.
    """
    url = f"{host.url}{path}"
    if query:
        url = f"{url}?{query}"

    # Override X-Client-ID in forwarded headers
    forward_headers = {k: v for k, v in headers.items()}

    resp = await client.request(
        method=method,
        url=url,
        headers=forward_headers,
        content=body or None,
        timeout=timeout,
    )
    return resp


async def dispatch_request(
    request: Request,
    body: bytes,
    client_id: str | None,
    config: Config,
    host_manager: HostManager,
    client: httpx.AsyncClient,
    routing_table: RoutingTable | None = None,
) -> StreamingResponse | JSONResponse:
    """
    Dispatch a buffered request to the appropriate Ollama host with failover.
    Failover only applies before any response bytes are sent to the client.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    path = request.url.path
    query = request.url.query
    method = request.method

    # Check model management protection
    if path in _MODEL_MANAGEMENT_PATHS and not config.proxy.allow_model_management:
        return JSONResponse(
            status_code=403,
            content={
                "error": (
                    "model management endpoints are disabled; "
                    "set allow_model_management: true in config to enable"
                ),
                "request_id": request_id,
            },
        )

    # Build forwarded headers — strip proxy-specific ones
    forward_headers: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() not in _STRIP_REQUEST_HEADERS:
            forward_headers[k.lower()] = v
    if client_id:
        forward_headers["x-client-id"] = client_id

    model = extract_model(body)

    # Build candidate host list — routing table (model_aware) or HostManager fallback
    def _next_host() -> OllamaHost | None:
        if routing_table is not None:
            rt_state = routing_table.pick(model)
            if rt_state is None:
                return None
            # Map routing state back to OllamaHost object for failover tracking
            for h in host_manager.hosts:
                if h.name == rt_state.name:
                    return h
            return None
        # Default: first healthy host (HostManager order, v0.1.x behaviour)
        for h in host_manager.hosts:
            if not h.healthy:
                continue
            if model and h.models and model not in h.models:
                continue
            return h
        return None

    last_error: str | None = None
    attempted: set[str] = set()

    while True:
        host = _next_host()
        if host is None or host.name in attempted:
            break
        attempted.add(host.name)

        try:
            resp = await client.request(
                method=method,
                url=f"{host.url}{path}" + (f"?{query}" if query else ""),
                headers=forward_headers,
                content=body or None,
                timeout=config.ollama.request_timeout,
            )
            host.requests_handled += 1

            # Fast-path routing invalidation: if Ollama says the model isn't loaded,
            # remove it from the routing table immediately so next request routes elsewhere.
            if resp.status_code == 404 and model and routing_table is not None:
                try:
                    err_body = resp.json()
                    if "not found" in err_body.get("error", "").lower():
                        routing_table.invalidate(host.name, model)
                        logger.info(
                            "routing.model_not_found host=%s model=%s — invalidated",
                            host.name,
                            model,
                        )
                except Exception:
                    pass

            # Check if this is a streaming response.
            # Ollama uses application/x-ndjson for streaming generate/chat,
            # text/event-stream for some endpoints, and application/json (chunked)
            # for others. Treat any chunked transfer or ndjson content as streaming.
            content_type = resp.headers.get("content-type", "")
            is_streaming = (
                "text/event-stream" in content_type
                or "application/x-ndjson" in content_type
                or resp.headers.get("transfer-encoding", "").lower() == "chunked"
            )

            response_headers = {
                "X-Failover-Host": host.name,
            }

            if is_streaming:
                async def stream_gen(r=resp):
                    async for chunk in r.aiter_bytes():
                        yield chunk

                return StreamingResponse(
                    stream_gen(),
                    status_code=resp.status_code,
                    headers={
                        **dict(resp.headers),
                        **response_headers,
                    },
                    media_type=resp.headers.get("content-type"),
                )
            else:
                ct = resp.headers.get("content-type", "")
                return JSONResponse(
                    status_code=resp.status_code,
                    content=resp.json() if ct.startswith("application/json") else None,
                    headers={**dict(resp.headers), **response_headers},
                )

        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
            last_error = str(e)
            host_manager.mark_unhealthy(host, last_error)
            if routing_table is not None:
                # Mark host unreachable in routing table too
                rt_state = routing_table._states.get(host.name)
                if rt_state:
                    rt_state.reachable = False
            logger.warning(
                "proxy.failover host=%s error=%s trying_next=true", host.name, last_error
            )
            continue

    return JSONResponse(
        status_code=503,
        content={"error": "all upstream hosts failed", "request_id": request_id},
        headers={"X-Failover-Exhausted": "true"},
    )
