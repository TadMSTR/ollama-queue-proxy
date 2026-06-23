# Changelog

## [Unreleased]

### Fixed
- **`/v1/embeddings` returned Ollama-native shape instead of OpenAI shape** — Ollama's `/api/embed` sends `application/json` responses with `transfer-encoding: chunked`, which `dispatch_request` incorrectly classified as streaming. This caused `proxy_handler` to receive a `StreamingResponse` rather than a `JSONResponse`, so the `isinstance` guard before `wrap_response` was never entered and the Ollama-native body passed through unwrapped. Fixed by removing `transfer-encoding: chunked` from the streaming heuristic — chunked TE is a transport-layer concern and is not a reliable indicator of application-level streaming. True streaming responses are identified solely by their content-type (`text/event-stream`, `application/x-ndjson`).

## [0.3.1] - 2026-06-16

### Fixed
- **Content-Length off-by-one on non-streaming responses** — Ollama appends a trailing newline to non-streaming JSON response bodies. `JSONResponse` re-serialises the body without it, but Starlette only auto-computes `content-length` when the header is absent from the passed headers dict. The upstream (stale) value was winning, making `Content-Length` 1 byte too large. Fixed by stripping `content-length` and `transfer-encoding` from upstream headers before building the `JSONResponse`.

## [0.3.0] - 2026-05-28

### Added
- **OpenAI-compat `/v1/embeddings` endpoint** — translates requests to `/api/embed` internally and wraps the Ollama response in the OpenAI Embeddings API format. Enables clients using the OpenAI SDK (e.g. Graphiti) to route through OQP without reconfiguration. Always-on; no config toggle required.

### Fixed
- `asyncio.get_event_loop()` in `_enqueue_request` replaced with `get_running_loop()` — eliminates DeprecationWarning in Python 3.10+ and RuntimeError in 3.12+ when called outside an async context.
- Streaming response generator now closes the underlying httpx response in a `finally` block — prevents connection leaks when a client disconnects mid-stream.
- `_apply_env_overrides` now correctly skips env vars with numeric path components (e.g. `OQP_OLLAMA__HOSTS__0__URL`) instead of raising `TypeError: list indices must be integers`.

## [0.2.0] - 2026-04-22

### Added

- **Client injection** — port-based authentication bypass for clients that cannot send Bearer headers. Each injection listener binds to a configurable `listen_port` and injects a fixed `client_id` identity, granting the client its full `max_priority` / `max_concurrent` entitlements without requiring an `Authorization` header. Defaults to loopback-only (`127.0.0.1`); external binding requires `allow_public_injection: true`. A startup warning is emitted when `allow_public_injection: true` combined with `auth.enabled: false`, as this creates a fully unauthenticated endpoint on all interfaces.
- **Model-aware routing** — weighted round-robin routing across Ollama hosts that already have the requested model loaded. A `RoutingTable` background poller queries `GET /api/tags` on each host at configurable intervals to maintain a live `(host → loaded_models)` map. On a model-match miss, falls back per `routing.fallback` (default: `any_healthy`). Requests without a `model` field use weighted round-robin across all healthy hosts. Fast-path invalidation removes a `(host, model)` pair immediately when a host returns "model not found".
- **Embedding response cache** — SHA256-keyed Valkey (RESP-compatible) cache for `/api/embed` and `/api/embeddings`. Cache hits bypass the queue and upstream entirely. Runtime RESP errors degrade gracefully (log once/min, bypass cache). Startup fails fast if the backend is unreachable when `embedding_cache.enabled: true`. Dragonfly is a supported drop-in backend.
- **keep_alive defaulting** — proxy-level middleware that injects a `keep_alive` value into request bodies for `/api/generate`, `/api/chat`, `/api/embed`, and `/api/embeddings` when the client does not supply one (or always, when `override: true`). Prevents Ollama from unloading models between bursty requests.
- **Per-client concurrency caps** — `max_concurrent` field on `auth.keys[]` entries. Enforced via per-`client_id` async semaphore on top of the existing global `proxy.max_concurrent` ceiling. A fairness bound (3 secondary-queue re-entries) prevents a saturated capped client from blocking forward progress for other clients.
- New config sections: `client_injection`, `routing`, `embedding_cache`, `keep_alive`.
- New optional fields on existing config: `ollama.hosts[].weight`, `ollama.hosts[].model_sync_interval`, `auth.keys[].max_concurrent`.
- New metrics: `oqp_host_models_loaded`, `oqp_routing_decisions_total`, `oqp_embedding_cache_hits_total`, `oqp_embedding_cache_misses_total`, `oqp_embedding_cache_errors_total`, `oqp_client_inflight`, `oqp_client_cap_waiting`.

### Changed

- Version skew corrected: `__init__.py` (was 0.1.0) and `pyproject.toml` (was 0.1.1) both updated to match the `v0.1.2` release tag; all three now advance together to 0.2.0.
- `serve()` refactored to launch N+1 uvicorn `Server` instances via `asyncio.gather` (main port + one per injection listener). Graceful shutdown across all listeners on SIGTERM/SIGINT.
- `Dockerfile` `CMD` switched from bare `uvicorn` invocation to the `ollama-queue-proxy` console script so `main:run()` actually launches the injection listener orchestration in containerized deployments.

### Security

- `client_injection.listeners[].bind` is now validated against `allow_public_injection`: a non-loopback bind without `allow_public_injection: true` fails config validation at startup. The non-loopback warning also fires whenever a listener binds off-loopback, regardless of `auth.enabled`, because injection ports bypass Bearer auth by design.
- `/metrics` label values (`model`, `host`, `client`, `endpoint`, `reason`, `kind`) are now escaped before interpolation into the Prometheus exposition format, preventing label-injection via client-supplied model names.

### Notes

- All v0.1.x configs continue to work unchanged. New fields default to v0.1.x-equivalent behavior (`weight=1`, `model_sync_interval=30`, `max_concurrent=0`, `routing.strategy=round_robin`).

## [0.1.2] - 2026-04-21

### Fixed

- Streaming response detection now handles `application/x-ndjson` content-type — Ollama uses this
  for `/api/generate` and `/api/chat` streaming responses; the previous check only matched
  `text/event-stream` and `application/json` (chunked), causing streaming responses to be
  returned as a null JSONResponse body. (`proxy.py`)
- Webhook SSRF check now supports an `allowed_hosts` list in config — enables webhook delivery
  to internal hostnames (e.g., ntfy on a LAN IP) without disabling the SSRF guard entirely.
  Host bypass is logged at INFO level. (`config.py`, `webhooks.py`, `main.py`)

## [0.1.1] - 2026-04-21

### Fixed

- SSRF webhook validation bypass via hostnames — `validate_webhook_url()` previously only checked
  raw IP literals; hostnames (e.g., `http://localhost/hook`) bypassed the blocklist. Now resolves
  hostnames to IP via `socket.getaddrinfo()` before blocklist comparison. Added `169.254.0.0/16`
  (link-local / cloud metadata) and `fe80::/10` to `_PRIVATE_NETWORKS`. (`webhooks.py`)
- Dockerfile missing `USER` instruction — container now runs as `appuser` (non-root) by default,
  consistent with the compose `user: 1000:1000` override. Safe for standalone `docker run`.
- Queue management tier parameter now validated — `?tier=bogus` returns HTTP 400 instead of
  unhandled `KeyError` → 500. Accepts `high`, `normal`, `low`. (`routes/queue.py`)
- CI action versions updated — `actions/checkout` → v6.0.2, `actions/setup-python` → v6.2.0
  with correct SHA pins. (`.github/workflows/ci.yml`)

## [0.1.0] - 2026-04-21

### Added

- Drop-in HTTP proxy for Ollama — change one env var (`OLLAMA_HOST=http://localhost:11435`), nothing else
- Per-client API key authentication with Bearer token validation
  - Constant-time key comparison (`hmac.compare_digest`)
  - Per-key priority ceilings — silently caps `X-Queue-Priority` to the key's `max_priority`
  - `X-Client-ID` auto-populated from key config (authoritative when auth enabled)
  - Management keys with `management: true` flag for operational endpoints
- Three-tier priority queue (high / normal / low)
  - Three separate `asyncio.Queue` instances with event-based worker (no spin-wait)
  - Per-tier `max_depth`, `max_wait`, and `high_watermark_pct` configuration
  - `X-Queue-Priority` header sets tier; default is `normal`
  - Queue overflow returns 503/429 with `Retry-After` header
  - Stale requests dropped after `max_wait` seconds
- Model-aware failover across multiple Ollama hosts
  - Ordered host list with passive failure detection
  - Background health check recovery (`GET /api/tags` polling)
  - Per-host model inventory refresh on recovery
  - Failover scoped to pre-response-start only (mid-stream failures return error)
- Model management endpoint protection (blocked by default: `/api/pull`, `/api/push`, `/api/delete`, `/api/create`, `/api/copy`)
- Integration surface
  - `GET /health` — lightweight liveness probe, no auth required
  - `GET /queue/status` — full queue, host, client, and security state
  - `GET /metrics` — Prometheus text exposition format (no external library)
  - `POST /queue/pause`, `/queue/resume`, `/queue/drain`, `/queue/flush` — management endpoints
  - Webhook events: `queue.full`, `queue.high_watermark`, `queue.drained`, `host.unhealthy`, `host.recovered`
- Request/response headers: `X-Queue-Wait-Time`, `X-Queue-Position`, `X-Failover-Host`, `X-Failover-Exhausted`, `Retry-After`, `X-Request-ID`
- Auth failure rate limiting (configurable max failures per IP per window)
- SSRF guard on webhook URL (validated at startup; rejects RFC 1918 + loopback targets)
- Security warning log when `auth.enabled: false` with `host: 0.0.0.0` binding
- Graceful SIGTERM shutdown with configurable drain timeout
- Request body size limit with `Content-Length` pre-check
- Docker-first deployment with compose example (localhost-only port binding, read-only container)
- GitHub Actions CI (ruff + pytest + docker build)
