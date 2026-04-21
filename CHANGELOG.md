# Changelog

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
