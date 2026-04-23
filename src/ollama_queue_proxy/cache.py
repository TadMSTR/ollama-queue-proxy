"""Embedding response cache backed by Valkey / any RESP-compatible store."""

from __future__ import annotations

import hashlib
import json
import logging
import time

import redis.asyncio as aioredis

from .config import EmbeddingCacheConfig

logger = logging.getLogger(__name__)

# Endpoints whose responses are cacheable (deterministic, compact, high repeat rate)
CACHEABLE_PATHS = frozenset({"/api/embed", "/api/embeddings"})

# Log RESP errors at most once per minute to avoid spam
_ERROR_LOG_COOLDOWN = 60.0

# Metric counters — updated in-place, read by /metrics
hits: dict[str, int] = {}    # keyed by (client, model, endpoint)
misses: dict[str, int] = {}
errors: dict[str, int] = {}


def _canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _cache_key(prefix: str, endpoint_ns: str, model: str, payload) -> str:
    """Build a cache key from model + normalised payload. No raw content logged."""
    preimage = model.encode("utf-8") + b"\x00" + _canonical_json(payload)
    digest = hashlib.sha256(preimage).hexdigest()[:32]
    return f"{prefix}v1:{endpoint_ns}:{digest}"


def _embed_key(prefix: str, model: str, body_data: dict) -> str:
    """Cache key for /api/embed. Normalises single-string 'input' to list."""
    raw_input = body_data.get("input", "")
    if isinstance(raw_input, str):
        raw_input = [raw_input]
    return _cache_key(prefix, "embed", model, raw_input)


def _embeddings_key(prefix: str, model: str, body_data: dict) -> str:
    """Cache key for /api/embeddings."""
    prompt = body_data.get("prompt", "")
    return _cache_key(prefix, "embeddings", model, prompt)


class EmbeddingCache:
    """
    Async embedding cache wrapping redis.asyncio.

    Startup: fail-fast if backend unreachable when enabled.
    Runtime: RESP errors log once/min and degrade gracefully — never fail a user request.
    Security (FLAG E): only key hash and model name are logged. No prompt/input content.
    """

    def __init__(self, config: EmbeddingCacheConfig) -> None:
        self._cfg = config
        self._client: aioredis.Redis | None = None
        self._last_error_log: float = 0.0
        self._enabled = config.enabled

    async def startup(self) -> None:
        """Connect and ping. Exits the process on failure when cache is enabled."""
        if not self._enabled:
            return
        import sys

        try:
            self._client = aioredis.from_url(
                self._cfg.backend,
                socket_connect_timeout=self._cfg.connect_timeout,
            )
            await self._client.ping()
            logger.info("embedding_cache.connected backend=%s", self._cfg.backend)
        except Exception as e:
            print(
                f"FATAL: embedding cache startup failed — could not connect to "
                f"'{self._cfg.backend}': {e}. "
                "Fix the backend address or set embedding_cache.enabled: false.",
                file=sys.stderr,
            )
            sys.exit(1)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    def _metric_key(self, client_id: str | None, model: str, endpoint: str) -> str:
        return f"{client_id or 'anon'},{model},{endpoint}"

    async def get(
        self,
        path: str,
        body_data: dict,
        model: str,
        client_id: str | None,
    ) -> bytes | None:
        """
        Return cached response bytes for the request, or None on miss/error.
        Logs model and cache key hash only — no request body content (FLAG E).
        """
        if not self._enabled or self._client is None:
            return None

        key = self._build_key(path, body_data, model)
        if key is None:
            return None

        mkey = self._metric_key(client_id, model, path)
        try:
            value = await self._client.get(key)
            if value is not None:
                hits[mkey] = hits.get(mkey, 0) + 1
                logger.debug(
                    "embedding_cache.hit endpoint=%s model=%s key_suffix=...%s",
                    path, model, key[-8:],
                )
                return value
            misses[mkey] = misses.get(mkey, 0) + 1
            return None
        except Exception as e:
            self._log_error("get", e)
            return None

    async def set(
        self,
        path: str,
        body_data: dict,
        model: str,
        response_bytes: bytes,
        client_id: str | None,
    ) -> None:
        """
        Cache a successful 2xx response. Skips if over max_entry_bytes.
        Logs model and key hash only (FLAG E).
        """
        if not self._enabled or self._client is None:
            return

        if len(response_bytes) > self._cfg.max_entry_bytes:
            logger.debug(
                "embedding_cache.skip_large endpoint=%s model=%s size=%d max=%d",
                path, model, len(response_bytes), self._cfg.max_entry_bytes,
            )
            return

        key = self._build_key(path, body_data, model)
        if key is None:
            return

        try:
            await self._client.setex(key, self._cfg.ttl, response_bytes)
            logger.debug(
                "embedding_cache.stored endpoint=%s model=%s key_suffix=...%s ttl=%d",
                path, model, key[-8:], self._cfg.ttl,
            )
        except Exception as e:
            self._log_error("set", e)

    def _build_key(self, path: str, body_data: dict, model: str) -> str | None:
        try:
            if path == "/api/embed":
                return _embed_key(self._cfg.key_prefix, model, body_data)
            elif path == "/api/embeddings":
                return _embeddings_key(self._cfg.key_prefix, model, body_data)
            return None
        except Exception:
            return None

    def _log_error(self, op: str, exc: Exception) -> None:
        now = time.monotonic()
        kind = type(exc).__name__
        errors[kind] = errors.get(kind, 0) + 1
        if now - self._last_error_log >= _ERROR_LOG_COOLDOWN:
            self._last_error_log = now
            logger.warning(
                "embedding_cache.error op=%s kind=%s error=%s (suppressing further logs for %ds)",
                op, kind, exc, int(_ERROR_LOG_COOLDOWN),
            )
