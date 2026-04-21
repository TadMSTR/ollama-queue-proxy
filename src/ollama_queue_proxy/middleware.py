"""Request middleware: request ID injection, priority header parsing."""

from __future__ import annotations

import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Echo or generate X-Request-ID
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def parse_priority(request: Request) -> str:
    """Extract X-Queue-Priority header value; default to 'normal'."""
    raw = request.headers.get("X-Queue-Priority", "normal").lower()
    return raw if raw in ("high", "normal", "low") else "normal"


def get_client_id(request: Request) -> str | None:
    """Get caller-supplied X-Client-ID header (used when auth is disabled)."""
    return request.headers.get("X-Client-ID")
