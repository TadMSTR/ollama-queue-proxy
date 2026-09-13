"""API key authentication, priority ceiling enforcement, and rate limiting."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import ApiKeyConfig, AuthConfig

logger = logging.getLogger(__name__)

PRIORITY_ORDER = {"high": 2, "normal": 1, "low": 0}


def scope_denied(request: Request, required: str) -> JSONResponse:
    """Uniform 403 for a key whose scope sits below `required`.

    Names only the scope that was REQUIRED, never the one the caller holds. Reporting
    what a key has turns every refusal into an oracle for the credential presented, and
    a caller that is entitled to know its own scope can be told out of band.

    Distinct from the 401 in `authenticate` on purpose: 401 means "I do not know who you
    are", 403 means "I do, and it is not enough".
    """
    return JSONResponse(
        status_code=403,
        content={
            "error": f"{required} permission required",
            "request_id": getattr(request.state, "request_id", "unknown"),
        },
    )


async def require_scope(request: Request, required: str) -> JSONResponse | None:
    """Authenticate, then check scope. Returns an error response, or None to proceed.

    The single gate for every route that is not the proxy catch-all. With auth disabled
    there is no key, so there is no scope to check and the route is open — the existing
    documented auth-off caveat, which now covers scope as well as `management`.
    """
    state = request.app.state.oqp
    key_cfg, err = await state.auth_manager.authenticate(request)
    if err:
        return err
    if state.config.auth.enabled and (key_cfg is None or not key_cfg.allows(required)):
        return scope_denied(request, required)
    return None


class AuthManager:
    def __init__(self, config: AuthConfig) -> None:
        self._config = config
        # O(1) key lookup — built once at startup
        self._key_map: dict[str, ApiKeyConfig] = {k.key: k for k in config.keys}
        self._warn_deprecated_management(config)
        # Rate limiting: {ip: [(timestamp, ...), ...]}
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    @staticmethod
    def _warn_deprecated_management(config: AuthConfig) -> None:
        """Name every key still using `management:` so the operator knows what to edit.

        Per client_id, not once in aggregate: a config with eleven keys gives an
        operator no way to act on "some key uses a deprecated field".

        Warned only on `true`, matching the ollama.health_check_interval notice in
        routing.py. `management: false` is the field's default, so warning on it would
        fire for every operator who never used the feature — which is how a deprecation
        warning teaches people to filter it out.
        """
        for key in config.keys:
            if key.management:
                logger.warning(
                    "config.deprecated key=auth.keys[client_id=%s].management — "
                    "superseded by `scope: management`, which has been applied. The old "
                    "key is still honoured; replace it before it is removed.",
                    key.client_id,
                )

    def lookup_key(self, provided: str) -> ApiKeyConfig | None:
        """Look up key using constant-time comparison to prevent timing attacks."""
        for stored_key, cfg in self._key_map.items():
            if hmac.compare_digest(stored_key, provided):
                return cfg
        return None

    async def authenticate(
        self, request: Request
    ) -> tuple[ApiKeyConfig | None, JSONResponse | None]:
        """
        Returns (key_config, None) on success or (None, error_response) on failure.
        When auth is disabled, returns (None, None) — request is allowed through.
        """
        if not self._config.enabled:
            return None, None

        client_ip = request.client.host if request.client else "unknown"

        # Check rate limit first
        if await self._is_rate_limited(client_ip):
            return None, JSONResponse(
                status_code=429,
                content={
                    "error": "too many authentication failures",
                    "request_id": request.state.request_id,
                },
                headers={"Retry-After": str(self._config.rate_limit.window_seconds)},
            )

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            await self._record_failure(client_ip)
            return None, JSONResponse(
                status_code=401,
                content={
                    "error": "missing or invalid authorization header",
                    "request_id": request.state.request_id,
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        provided_key = auth_header[len("Bearer ") :]
        key_cfg = self.lookup_key(provided_key)
        if key_cfg is None:
            await self._record_failure(client_ip)
            return None, JSONResponse(
                status_code=401,
                content={"error": "invalid api key", "request_id": request.state.request_id},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Clear failures on successful auth
        async with self._lock:
            self._failures[client_ip].clear()

        return key_cfg, None

    def enforce_priority_ceiling(self, requested: str, key_cfg: ApiKeyConfig | None) -> str:
        """Cap priority to key's max_priority if auth is enabled and key is present."""
        if key_cfg is None:
            return requested if requested in PRIORITY_ORDER else "normal"
        if requested not in PRIORITY_ORDER:
            requested = "normal"
        if PRIORITY_ORDER[requested] > PRIORITY_ORDER[key_cfg.max_priority]:
            return key_cfg.max_priority
        return requested

    async def _is_rate_limited(self, ip: str) -> bool:
        async with self._lock:
            now = time.monotonic()
            window = self._config.rate_limit.window_seconds
            recent = [t for t in self._failures[ip] if now - t < window]
            self._failures[ip] = recent
            return len(recent) >= self._config.rate_limit.max_failures

    async def _record_failure(self, ip: str) -> None:
        async with self._lock:
            self._failures[ip].append(time.monotonic())
