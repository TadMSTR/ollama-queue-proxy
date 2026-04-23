"""Client injection — port-based auth bypass for clients that can't send Bearer headers."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from .config import ApiKeyConfig
from .middleware import RequestContextMiddleware, parse_priority

logger = logging.getLogger(__name__)

# Set by main.py lifespan after AppState is fully initialised.
# Injection apps read from this reference — they have no lifespan of their own.
_shared_state = None  # type: ignore[assignment]


def set_shared_state(state) -> None:  # type: ignore[type-arg]
    global _shared_state
    _shared_state = state


@asynccontextmanager
async def _null_lifespan(app: FastAPI):
    yield


def make_injection_app(inject_as: str, key_cfg: ApiKeyConfig) -> FastAPI:
    """
    Return a lightweight FastAPI app that injects a fixed client identity for all requests.

    The app shares queue, workers, and host manager with the main app via _shared_state.
    No Bearer token is required on this port. The Authorization header is stripped before
    forwarding to upstream by the existing _STRIP_REQUEST_HEADERS set in proxy.py.
    """
    from .main import _enqueue_request  # imported late to avoid circular import

    inj_app = FastAPI(
        title=f"ollama-queue-proxy injection ({inject_as})",
        lifespan=_null_lifespan,
    )
    inj_app.add_middleware(RequestContextMiddleware)

    @inj_app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
    async def injection_handler(request: Request, path: str):
        from fastapi.responses import JSONResponse

        state = _shared_state
        if state is None:
            return JSONResponse(
                status_code=503,
                content={"error": "proxy is starting up, retry shortly"},
            )
        if state.shutting_down:
            request_id = getattr(request.state, "request_id", "unknown")
            return JSONResponse(
                status_code=503,
                content={"error": "proxy is shutting down", "request_id": request_id},
            )

        logger.debug(
            "injection.request client_id=%s port=%s path=/%s",
            inject_as,
            request.url.port,
            path,
        )

        # Honour the key's max_priority as a ceiling (same logic as main port)
        requested_priority = parse_priority(request)
        from .auth import PRIORITY_ORDER

        if PRIORITY_ORDER.get(requested_priority, 0) > PRIORITY_ORDER.get(
            key_cfg.max_priority, 1
        ):
            tier = key_cfg.max_priority
        else:
            tier = requested_priority

        return await _enqueue_request(
            request=request,
            client_id=inject_as,
            tier=tier,
            state=state,
        )

    return inj_app
