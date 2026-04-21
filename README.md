# ollama-queue-proxy

A drop-in HTTP proxy for [Ollama](https://ollama.com) that adds per-client API key authentication, priority queuing, and model-aware failover. One config change in your consumers:

```
OLLAMA_HOST=http://localhost:11435
```

Everything else works as before. Streaming, `/api/tags`, `/api/version` — all pass through transparently.

---

## Why this exists

The top r/ollama post of the past year was someone's open Ollama being exploited for weeks. Ollama ships with no authentication. This proxy puts auth in front with per-client keys and priority ceilings — without requiring any changes to consumers like Open WebUI, LangChain, or Continue.dev.

**And the starvation problem:** if you run embeddings at night and interactive chat hits the same server, the chat waits. One header fixes this:

```
X-Queue-Priority: high
```

Background jobs send `low`. Interactive tools send `normal` or `high`. The queue handles the rest.

---

## Quick start

```yaml
# docker-compose.yml
services:
  ollama-queue-proxy:
    image: ghcr.io/tadmstr/ollama-queue-proxy:latest
    ports:
      - "127.0.0.1:11435:11435"
    volumes:
      - ./config.yml:/app/config.yml:ro
```

```bash
cp config.example.yml config.yml
# Edit config.yml — set your Ollama host URL
docker compose up -d
```

Then point your consumers at `http://localhost:11435` instead of `http://localhost:11434`.

> **Warning:** Default config has no authentication. If exposing beyond localhost, set `auth.enabled: true` and configure API keys. The docker-compose example binds to `127.0.0.1` for this reason.

---

## Authentication

Set `auth.enabled: true` and add keys to `config.yml`:

```yaml
auth:
  enabled: true
  keys:
    - key: "sk-my-interactive-key"
      client_id: "openwebui"
      description: "Open WebUI"
      max_priority: high
      management: false
    - key: "sk-my-batch-key"
      client_id: "memsearch-watch"
      description: "Background embedding jobs"
      max_priority: low
      management: false
    - key: "sk-my-admin-key"
      client_id: "admin"
      description: "Admin"
      max_priority: high
      management: true
```

Consumers pass their key as a Bearer token:

```
Authorization: Bearer sk-my-interactive-key
```

**Versus Ollama's built-in `OLLAMA_API_KEY`:** Ollama supports a single shared key — one key for all consumers, no per-client control. This proxy gives each consumer its own key with its own priority ceiling and optional management access.

**Priority ceilings:** a key with `max_priority: low` that sends `X-Queue-Priority: high` is silently capped to `low`. The caller doesn't know — it just gets queued at its allowed tier.

**Management keys:** only keys with `management: true` can call `/queue/pause`, `/queue/resume`, `/queue/drain`, `/queue/flush`. A regular key calling a management endpoint gets 403, not 401 (authenticated but not authorized).

---

## For users already running Nginx or Caddy

A generic reverse proxy gives you auth (one shared key) and TLS termination. This proxy adds what it can't:

- **Ollama-aware priority queuing** — queue high/normal/low tiers with per-tier depth limits and expiry
- **Per-client keys with priority ceilings** — not just auth, but who gets to run first
- **Model-aware routing** — requests for models only on certain hosts go to the right host
- **Queue visibility** — `X-Queue-Wait-Time`, `X-Queue-Position`, `Retry-After`, `/queue/status`
- **Failover** — if the primary Ollama host goes down, requests continue on the fallback

If you already have a reverse proxy, put this behind it rather than replacing it.

---

## Priority queuing

Three tiers: `high`, `normal` (default), `low`. Set the tier per-request:

```
X-Queue-Priority: low
```

Workers dispatch high before normal before low. Each tier has its own depth limit, max wait timeout, and high-watermark threshold for webhook events.

**Consumer example:**
```python
# Background embedding job — uses low priority
import httpx
client = httpx.Client(
    base_url="http://localhost:11435",
    headers={
        "Authorization": "Bearer sk-my-batch-key",
        "X-Queue-Priority": "low",
    }
)
```

The proxy caps the priority to the key's `max_priority` — a batch key configured with `max_priority: low` can't elevate itself to `high` regardless of what header it sends.

---

## Queue visibility

Every response includes:

| Header | Value |
|--------|-------|
| `X-Queue-Wait-Time` | Milliseconds spent in queue |
| `X-Queue-Position` | Position at enqueue time (present only if request waited) |
| `X-Failover-Host` | Name of the Ollama host that handled the request |
| `Retry-After` | Seconds to wait (on 503/429 queue overflow) |

```
GET /queue/status
```

Returns full queue state, host health, per-client stats, and security config. See [Integration surface](#integration-surface) for the full schema.

---

## Failover

Configure multiple hosts in order:

```yaml
ollama:
  hosts:
    - url: "http://ollama-primary:11434"
      name: "primary"
    - url: "http://ollama-fallback:11434"
      name: "fallback"
```

On connection failure or timeout, the proxy marks the host unhealthy, logs it, and retries on the next host. The response includes `X-Failover-Host` showing which host handled it.

**Model-aware routing:** if a request specifies a model that isn't on the primary host, the proxy skips to a host that has it.

**Important:** failover only applies before any response bytes are sent. If a streaming response has already started, a mid-stream failure returns a connection error to the client — transparent retry isn't possible once streaming begins.

Background health checks (`GET /api/tags`) recover unhealthy hosts and refresh their model inventory without a restart.

---

## Integration surface

### Headers

| Header | Direction | Purpose |
|--------|-----------|---------|
| `X-Queue-Priority` | Request | Set tier: `high`, `normal`, `low` |
| `X-Client-ID` | Request | Client attribution (overridden by key config when auth enabled) |
| `X-Request-ID` | Request | Echo or generate; included in all error bodies |
| `X-Queue-Wait-Time` | Response | Milliseconds in queue |
| `X-Queue-Position` | Response | Position at enqueue (omitted if dispatched immediately) |
| `X-Failover-Host` | Response | Host name that handled the request |
| `X-Failover-Exhausted` | Response | Present on 503 when all hosts failed |
| `Retry-After` | Response | Seconds on 503/429 overflow |

### Endpoints

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /health` | None | Liveness probe — always open |
| `GET /queue/status` | Token (when enabled) | Full queue, host, client, security state |
| `GET /metrics` | Token (when enabled) | Prometheus text format |
| `POST /queue/pause?tier=low` | Management key | Stop accepting requests for tier |
| `POST /queue/resume?tier=low` | Management key | Resume tier |
| `POST /queue/drain` | Management key | Wait for queues to empty |
| `POST /queue/flush?tier=low` | Management key | Drop all pending requests immediately |

### Webhook events

```yaml
webhooks:
  enabled: true
  url: "https://hooks.example.com/ollama-alerts"
  events:
    - queue.full
    - queue.high_watermark
    - queue.drained
    - host.unhealthy
    - host.recovered
```

Payload:
```json
{
  "event": "host.unhealthy",
  "tier": null,
  "timestamp": "2026-04-21T08:00:00Z",
  "name": "primary"
}
```

Delivery is fire-and-forget (5s timeout). Failed deliveries are logged at WARNING; never retried.

---

## Config reference

All values can be overridden via env vars with `OQP_` prefix and `__` nesting:

```bash
OQP_PROXY__PORT=11435
OQP_OLLAMA__HOSTS__0__URL=http://ollama:11434
OQP_AUTH__ENABLED=true
```

See [`config.example.yml`](config.example.yml) for the full config with inline documentation.

---

## Building on top of this

**Prometheus scraping:**
```yaml
# prometheus.yml
scrape_configs:
  - job_name: ollama-queue-proxy
    static_configs:
      - targets: ["localhost:11435"]
    metrics_path: /metrics
    bearer_token: "sk-my-metrics-key"
```

**Grafana dashboard:** scrape `/metrics` into Prometheus, or query `/queue/status` directly from a JSON datasource panel.

**NATS/Redis from webhooks:** forward `queue.full` and `host.unhealthy` events from the webhook URL to a message bus for real-time alerting without log scraping.

**Agent orchestration (Helm platform):** use `/queue/pause` and `/queue/resume` to gate batch agent jobs during interactive sessions. Management key holders can hold a tier while running heavy jobs without starving interactive users.

---

## What's not in v1

These are intentionally out of scope. Community contributions welcome:

- Per-key rate limiting (queue depth limits already throttle; rate limiting adds stateful complexity)
- OAuth/JWT (wrong scope for a local proxy tool)
- IP allowlisting (better handled at the network/SWAG layer)
- Live key rotation API (manage keys via config file + restart)
- Load balancing across healthy hosts (current behavior is ordered failover)
- Persistent queue (SQLite-backed — survives restarts)
- Per-caller default priority from source IP

---

## Client compatibility

Any Ollama client works unchanged. The proxy forwards `GET /api/version`, `GET /api/tags`, streaming chat, streaming generate, and all other endpoints transparently. Clients that probe these endpoints on startup (Open WebUI, LangChain, Continue.dev) will connect successfully.

---

## License

MIT — see [LICENSE](LICENSE).
