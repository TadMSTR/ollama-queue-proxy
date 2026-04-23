# ollama-queue-proxy

[![Built with Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-6B57FF?logo=claude&logoColor=white)](https://claude.ai/code)
[![CI](https://github.com/TadMSTR/ollama-queue-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/TadMSTR/ollama-queue-proxy/actions/workflows/ci.yml)
[![Python versions](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![SafeSkill 93/100](https://img.shields.io/badge/SafeSkill-93%2F100_Verified%20Safe-brightgreen)](https://safeskill.dev/scan/tadmstr-ollama-queue-proxy)

A **smart pool manager for Ollama** — one endpoint that authenticates, queues, routes, caches, and rate-limits across a whole Ollama fleet. Drop-in compatible: one config change in your consumers:

```
OLLAMA_HOST=http://localhost:11435
```

Everything else works as before. Streaming, `/api/tags`, `/api/version` — all pass through transparently.

---

## What it does

| Feature | What it gives you |
|---|---|
| **Per-client API keys** | Each consumer gets its own key with a priority ceiling — no shared secret |
| **Priority queuing** | Three tiers (high / normal / low) with per-tier depth limits and expiry |
| **Client injection** | Port-based auth bypass for clients that can't send Bearer headers |
| **Model-aware routing** | Route to the host that already has the model loaded — avoid cold-start latency |
| **Embedding cache** | Hash-keyed Valkey cache for `/api/embed` and `/api/embeddings` — repeated RAG requests skip upstream |
| **keep_alive defaulting** | Prevent Ollama from unloading models between bursty requests |
| **Per-client concurrency caps** | Hard ceiling per client so batch workloads can't starve interactive ones |
| **Failover** | On host failure, retry on the next configured host transparently |

> **Just need auth?** See [ollama-auth-sidecar](https://github.com/TadMSTR/ollama-auth-sidecar) — a simpler tool if queuing, routing, and caching aren't needed.

---

## Why this exists

The top r/ollama post of the past year was someone's open Ollama being exploited for weeks. Ollama ships with no authentication. This proxy puts auth in front with per-client keys and priority ceilings — without requiring any changes to consumers like Open WebUI, LangChain, or Continue.dev.

**And the starvation problem:** if you run embeddings at night and interactive chat hits the same server, the chat waits. One header fixes this:

```
X-Queue-Priority: high
```

Background jobs send `low`. Interactive tools send `normal` or `high`. The queue handles the rest.

---

## How it works

![Request flow diagram](docs/request-flow.drawio.png)

Requests from multiple consumers enter the proxy, are authenticated (or identity-injected for consumers without Bearer support), and placed into one of three priority tiers. A worker pool drains the tiers in order. For each request, the model-aware router picks the best host (one that already has the model loaded). Embedding requests check the cache first — hits skip the queue and upstream entirely. `keep_alive` is injected into request bodies so Ollama doesn't unload models between requests. Per-client concurrency caps prevent any single client from monopolizing the queue.

Proxy overhead is roughly 1–2ms per request in local testing — negligible compared to Ollama inference time.

---

## Quick start

```bash
git clone https://github.com/TadMSTR/ollama-queue-proxy
cd ollama-queue-proxy
cp config.example.yml config.yml
# Edit config.yml — set your Ollama host URL (see comment in file)
docker compose up -d
```

> **If Ollama runs natively (not in a container):** set the host URL to `http://host.docker.internal:11434` (Mac/Windows) or `http://172.17.0.1:11434` (Linux).

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
      max_concurrent: 0        # unlimited (subject to proxy.max_concurrent)
    - key: "sk-my-batch-key"
      client_id: "memsearch-watch"
      description: "Background embedding jobs"
      max_priority: low
      max_concurrent: 2        # cap at 2 concurrent so it can't starve interactive users
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

**Versus Ollama's built-in `OLLAMA_API_KEY`:** Ollama supports a single shared key — one key for all consumers, no per-client control. This proxy gives each consumer its own key with its own priority ceiling, concurrency cap, and optional management access.

**Priority ceilings:** a key with `max_priority: low` that sends `X-Queue-Priority: high` is silently capped to `low`. The caller doesn't know — it just gets queued at its allowed tier.

**Per-client concurrency caps:** `max_concurrent: N` limits a client to N simultaneous in-flight requests. Setting to `0` is unlimited. The cap must be ≤ `proxy.max_concurrent`. Different clients have independent semaphores — a capped batch client never blocks an interactive client.

**Management keys:** only keys with `management: true` can call `/queue/pause`, `/queue/resume`, `/queue/drain`, `/queue/flush`. A regular key calling a management endpoint gets 403, not 401 (authenticated but not authorized).

**MCP consumer support:** [jobsearch-mcp](https://github.com/TadMSTR/jobsearch-mcp) and [searxng-mcp](https://github.com/TadMSTR/searxng-mcp) both read `OLLAMA_API_KEY` from their environment and forward it as a Bearer token on all outgoing Ollama requests. Point them at the proxy and set their `OLLAMA_API_KEY` to their assigned key — no code changes required.

---

## Client injection

Some clients can't send a `Bearer` token — they're hardcoded to talk to Ollama directly with no auth header. Client injection solves this by binding extra ports that automatically inject a fixed identity, so those clients get full auth and priority enforcement without any client-side changes.

```yaml
client_injection:
  listeners:
    - listen_port: 11436
      inject_as: memsearch-watch   # must match an auth.keys[].client_id
      bind: 127.0.0.1              # default: loopback only
    - listen_port: 11437
      inject_as: localllm
  allow_public_injection: false    # must be true to bind injection ports to non-loopback
```

Point the client at the injection port. Its requests arrive with no `Authorization` header — the proxy fills in the identity and routes through the same queue, with the same priority ceiling and concurrency cap as the named key.

**Security notes:**
- Injection ports default to `127.0.0.1` — accessible only from the local host.
- Binding to a non-loopback address requires `allow_public_injection: true`. This is appropriate behind a trusted network; be cautious exposing it to untrusted hosts.
- If `allow_public_injection: true` AND `auth.enabled: false`, the proxy emits a startup security warning — any host on the network can consume GPU time with no credential.
- The `Authorization` header is stripped before forwarding to upstream — a token sent to an injection port is never relayed to Ollama.

---

## Model-aware routing

When running multiple Ollama hosts (different GPUs or different model sets), the proxy can route each request to the host that already has the target model loaded — avoiding the latency hit of loading a model that's been evicted.

```yaml
ollama:
  hosts:
    - url: "http://forge:11434"
      name: "primary"
      weight: 2                    # gets 2x the traffic of weight-1 hosts
      model_sync_interval: 30      # seconds between /api/tags polls
    - url: "http://helm:11434"
      name: "secondary"
      weight: 1
      model_sync_interval: 30
  health_check_interval: 30

routing:
  strategy: model_aware            # model_aware | round_robin
  fallback: any_healthy            # when no host has the model: pick any healthy host
  model_poll_timeout: 3
```

**How it works:** a background poller hits `GET /api/tags` on each host every `model_sync_interval` seconds, maintaining a live `(host → loaded_models)` map. Requests with a `model` field are routed to a host that already has it. Weighted round-robin is deterministic (not stochastic) — a 2:1 weight ratio means exactly 2 requests to the heavy host for every 1 to the lighter host.

**Requests without a `model` field** use weighted round-robin across all healthy hosts.

**Fast-path invalidation:** when a host returns "model not found" (404), the proxy immediately removes that `(host, model)` pair from the routing table — no waiting for the next poll cycle.

**Startup:** the proxy probes each host's `/api/tags` once before accepting requests. If no host responds, startup fails fast with a clear error.

---

## Embedding cache

Repeated embedding requests (common in RAG, semantic search, and agent workloads) often re-embed the same strings. The embedding cache stores successful responses in Valkey (or any RESP-compatible store — Dragonfly is a supported drop-in) and serves hits without touching the queue or upstream.

```yaml
embedding_cache:
  enabled: true
  backend: "redis://valkey:6379/0"   # Valkey recommended; Dragonfly works as a drop-in
  ttl: 86400                          # seconds
  max_entry_bytes: 32768              # skip caching responses larger than this
  key_prefix: "oqp:embed:"
  connect_timeout: 2
```

**Scope:** `/api/embed` and `/api/embeddings` only. `/api/generate` and `/api/chat` are never cached (non-deterministic, large, low repeat rate).

**Cache key:** SHA256 of `model + \0 + canonical_json(input)`, truncated to 32 hex chars. Per-endpoint namespaces prevent cross-endpoint collisions (same text via `/api/embed` and `/api/embeddings` get separate keys — their response shapes differ).

**Startup:** if `enabled: true`, the proxy pings the backend at startup. If unreachable, startup fails fast. After startup, any RESP error degrades gracefully: logged at most once per minute, the cache is bypassed for that request, and no user request fails.

**Metrics:** `oqp_embedding_cache_hits_total`, `oqp_embedding_cache_misses_total`, `oqp_embedding_cache_errors_total` — all at `/metrics` with `client`, `model`, and `endpoint` labels.

---

## keep_alive defaulting

Ollama unloads a model from GPU memory after 5 minutes of inactivity (configurable on the Ollama side). For bursty workloads — embeddings or agents that fire requests every few minutes — this causes expensive cold-load latency. The proxy injects a `keep_alive` value so the model stays loaded.

```yaml
keep_alive:
  default: "5m"       # injected when the client doesn't send keep_alive
  override: false     # if true, always replace the client's value with default
```

**Applies to:** `/api/generate`, `/api/chat`, `/api/embed`, `/api/embeddings`.

**Behavior:** if `override: false` and `keep_alive` is absent in the request body, inject `default`. If `override: true`, always replace. Non-JSON bodies and bodies over `max_request_body_mb` pass through untouched.

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

Background health checks (`GET /api/tags`) recover unhealthy hosts without a restart.

**Important:** failover only applies before any response bytes are sent. If a streaming response has already started, a mid-stream failure returns a connection error to the client — transparent retry isn't possible once streaming begins.

---

## Migration from v0.1.x

v0.2.0 is fully backward-compatible. All v0.1.x configs load without changes — new sections and fields default to v0.1.x-equivalent behavior:

| New field | Default |
|---|---|
| `ollama.hosts[].weight` | `1` (equal weight) |
| `ollama.hosts[].model_sync_interval` | `30` |
| `routing.strategy` | `round_robin` (v0.1.x behavior) |
| `embedding_cache.enabled` | `false` (disabled) |
| `keep_alive.default` | `"5m"` |
| `auth.keys[].max_concurrent` | `0` (unlimited) |
| `client_injection.listeners` | `[]` (no injection ports) |

No config changes required to upgrade.

---

## Queue visibility

Every response includes:

| Header | Value |
|--------|-------|
| `X-Queue-Wait-Time` | Milliseconds spent in queue |
| `X-Queue-Position` | Position at enqueue time (present only if request waited) |
| `X-Failover-Host` | Name of the Ollama host that handled the request |
| `X-Failover-Exhausted` | Present on 503 when all hosts failed |
| `X-Cache` | `HIT` when the response was served from the embedding cache |
| `Retry-After` | Seconds to wait (on 503/429 queue overflow) |

```
GET /queue/status
```

Returns full queue state, host health, per-client stats, routing decisions, and security config.

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
| `X-Cache` | Response | `HIT` when served from embedding cache |
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

`max_concurrent` controls how many requests the proxy dispatches to Ollama simultaneously. Set it to match Ollama's `OLLAMA_NUM_PARALLEL` environment variable (Ollama's default is 1; the proxy default of 2 assumes you've set `OLLAMA_NUM_PARALLEL=2` or higher on the Ollama side). They're independent settings — the proxy throttles at the queue layer, Ollama throttles internally. If they're mismatched, requests will either queue unnecessarily or pile up at Ollama.

All values can be overridden via env vars with `OQP_` prefix and `__` nesting:

```bash
OQP_PROXY__PORT=11435
OQP_OLLAMA__HOSTS__0__URL=http://ollama:11434
OQP_AUTH__ENABLED=true
OQP_ROUTING__STRATEGY=model_aware
OQP_EMBEDDING_CACHE__ENABLED=true
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

Key v0.2.0 metrics:
- `oqp_routing_decisions_total{reason}` — `model_match`, `round_robin`, `fallback`
- `oqp_host_models_loaded{host}` — live model inventory per host
- `oqp_embedding_cache_hits_total{client,model,endpoint}` — cache hit rate
- `oqp_client_inflight{client_id}` — per-client in-flight count
- `oqp_client_cap_waiting{client_id}` — per-client semaphore queue depth

**Grafana dashboard:** scrape `/metrics` into Prometheus, or query `/queue/status` directly from a JSON datasource panel.

**Agent orchestration:** use `/queue/pause` and `/queue/resume` to gate batch agent jobs during interactive sessions. Management key holders can hold a tier while running heavy jobs without starving interactive users.

---

## For users already running Nginx or Caddy

A generic reverse proxy gives you auth (one shared key) and TLS termination. This proxy adds what it can't:

- **Ollama-aware priority queuing** — queue high/normal/low tiers with per-tier depth limits and expiry
- **Per-client keys with priority ceilings** — not just auth, but who gets to run first
- **Model-aware routing** — requests for models only on certain hosts go to the right host
- **Embedding cache** — avoid redundant upstream calls for repeated RAG/search embedding requests
- **Queue visibility** — `X-Queue-Wait-Time`, `X-Queue-Position`, `Retry-After`, `/queue/status`
- **Failover** — if the primary Ollama host goes down, requests continue on the fallback

If you already have a reverse proxy, put this behind it rather than replacing it.

---

## Client compatibility

Any Ollama client works unchanged. The proxy forwards `GET /api/version`, `GET /api/tags`, streaming chat, streaming generate, and all other endpoints transparently. Clients that probe these endpoints on startup (Open WebUI, LangChain, Continue.dev) will connect successfully.

---

## Running without Docker

```bash
pip install git+https://github.com/TadMSTR/ollama-queue-proxy
cp config.example.yml config.yml
# Edit config.yml
ollama-queue-proxy
```

Or with an environment variable instead of a config file:

```bash
OQP_OLLAMA__HOSTS__0__URL=http://localhost:11434 ollama-queue-proxy
```

Python 3.11+ required.

---

## License

MIT — see [LICENSE](LICENSE).
