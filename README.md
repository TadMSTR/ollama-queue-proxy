# ollama-queue-proxy

[![Built with Claude Code](https://img.shields.io/badge/Built_with-Claude_Code-6B57FF?logo=claude&logoColor=white)](https://claude.ai/code)
[![CI](https://github.com/TadMSTR/ollama-queue-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/TadMSTR/ollama-queue-proxy/actions/workflows/ci.yml)
[![Python versions](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Multi-tenant admission control for a shared Ollama.** One endpoint that authenticates each consumer, binds its queue priority to its credential, caps its concurrency, and caches its embeddings — so that several tenants can share one GPU without the loudest one winning. Drop-in compatible: one config change in your consumers:

```
OLLAMA_HOST=http://localhost:11435
```

Everything else works as before. Streaming, `/api/tags`, `/api/version` — all pass through transparently, and metadata reads skip the queue entirely.

---

## What it does

| Feature | What it gives you |
|---|---|
| **Per-client API keys** | Each consumer gets its own key with a priority ceiling — no shared secret |
| **Authenticated priority** | The ceiling is enforced against the *key*, so a client cannot promote itself by sending a header |
| **Priority queuing** | Three tiers (high / normal / low) with per-tier depth limits and expiry |
| **Client injection** | Port-based auth bypass for clients that can't send Bearer headers |
| **Model-aware routing** | Prefer a host that already has the model *installed* (`/api/tags`), else weighted round-robin |
| **Embedding cache** | Hash-keyed Valkey cache for `/api/embed` and `/api/embeddings` — repeated RAG requests skip upstream |
| **keep_alive defaulting** | Prevent Ollama from unloading models between bursty requests |
| **Per-client concurrency caps** | Hard ceiling per client so batch workloads can't starve interactive ones |
| **Failover** | On host failure, retry on the next configured host transparently |

> **The product only exists with `auth.enabled: true`.** With auth disabled — the shipped
> default, and the quick-start path — `client_id` comes from a caller-supplied
> `X-Client-ID` header and the priority ceiling is not applied. Any client can then claim
> any identity and any priority, which also defeats the per-client concurrency caps. That
> is correct behaviour for a single-tenant quick start, but everything on this page
> describing *policy* assumes auth is on.

> **Just need auth?** See [ollama-auth-sidecar](https://github.com/TadMSTR/ollama-auth-sidecar) — a simpler tool if queuing, routing, and caching aren't needed.

---

## Why this exists

The top r/ollama post of the past year was someone's open Ollama being exploited for weeks. Ollama ships with no authentication. This proxy puts auth in front with per-client keys and priority ceilings — without requiring any changes to consumers like Open WebUI, LangChain, or Continue.dev.

The other reality: in a homelab, one Ollama host quickly becomes a shared resource. Open WebUI, agent swarms, embedding workers, and overnight batch jobs all hit the same GPU. Without per-client policy and a queue, the loudest workload wins. This proxy makes one Ollama into a multi-tenant endpoint with an admission policy.

**And the starvation problem:** if you run embeddings at night and interactive chat hits the same server, the chat waits. One header fixes this:

```
X-Queue-Priority: high
```

Background jobs send `low`. Interactive tools send `normal` or `high`. The queue handles the rest.

**The header is a request, not a grant.** Each API key carries a `max_priority`, and the
requested tier is capped to it before the request is enqueued. A key issued at `low`
cannot obtain `high` by asking, so a batch worker cannot promote itself out of the tier
it was given. That distinction is the point of the whole design — see below.

---

## How this compares to alternatives

The short version: **several tools in this space queue, and several authenticate. The
combination — a priority tier bound to an authenticated credential and enforced
server-side — is what is hard to find elsewhere.**

| | Inbound client auth | Priority | Priority bound to identity |
|---|---|---|---|
| **ollama-queue-proxy** | per-client API keys | 3 tiers, `X-Queue-Priority` | **yes** — `max_priority` per key |
| [Olla](https://github.com/thushan/olla) | none | — | n/a |
| ollamaMQ | none | VIP / Boost per user | no — `X-User-ID` is an unauthenticated header |
| LiteLLM | virtual keys | `priority` in request body (beta) | no — caller-declared |
| ollama_proxy_server, LoLLMs Hub | API keys | none | n/a |

**vs. Olla.** Olla is the strongest tool here for *fleet routing* — Go, ~12 native
backends, circuit breakers, sticky KV-cache sessions, sub-millisecond selection, under
50 MB RAM. If routing intelligence across many backends is your problem, use Olla. What
it does not do is authenticate inbound clients at all; its docs are explicit that the
`auth:` block "has no bearing on how clients authenticate to Olla". So it cannot express
a per-tenant policy, because it has no notion of which tenant is calling.

**vs. ollamaMQ.** Closest in spirit — it has a real priority queue with VIP/Boost tiers
and a decaying fair-share score (which is better than this project's anti-starvation
story today). But priority is keyed off `X-User-ID`, an ordinary request header with
nothing verifying it, so any client can claim any tier.

**vs. LiteLLM.** The enterprise default for multi-provider proxying. If you run mixed
providers, use LiteLLM. Its scheduler does have a `priority` field, but it is
caller-declared rather than bound to the virtual key, it is marked beta, and it covers
only `acompletion` / `atext_completion` — **not embeddings**, which is precisely the
workload that starves interactive chat on a shared GPU. LiteLLM issue #13405,
*"Support for Priority-Based Request Handling via API Keys"*, is an open request for
what this project already does.

**vs. DIY Nginx + Redis.** The "rate-limit Ollama with Nginx" pattern is in every blog
post, and it handles request-rate fine. It cannot express priority within a single key,
`keep_alive` injection, or an embedding cache — those need awareness of the Ollama
protocol, not just HTTP counts.

**vs. [ollama-auth-sidecar](https://github.com/TadMSTR/ollama-auth-sidecar).** The right
tool when you have one Ollama host and just want auth, with no queue or policy.

---

## How it works

```mermaid
flowchart TD
    subgraph Entry["Entry Points"]
        C1["Consumers\n(LibreChat, LangChain, agents)\nport 11435"]
        C2["No-auth clients\nport 11436 / 11437"]
    end

    C1 -->|"Bearer token"| AUTH
    C2 -->|"no header"| INJ["Client Injection\ninjects client_id + priority ceiling"]
    INJ --> AUTH

    AUTH{"Auth\ncheck"}
    AUTH -->|"invalid"| E401["401 Unauthorized"]
    AUTH -->|"pass"| EMBED

    EMBED{"Embedding\nrequest?\n/api/embed\n/api/embeddings"}
    EMBED -->|"yes"| CACHE{"Valkey cache\nSHA-256 key"}
    EMBED -->|"no"| PRI

    CACHE -->|"HIT"| HIT["Return cached response\nX-Cache: HIT\nskips queue + upstream"]
    CACHE -->|"MISS"| PRI

    PRI["Priority Queue\nhigh › normal › low\n(capped to key's max_priority)"]
    PRI -->|"depth exceeded"| E503["429 / 503 + Retry-After"]
    PRI --> WORKERS["Worker Pool\nmax_concurrent slots\nper-client semaphore"]

    WORKERS --> KA["Inject keep_alive\ninto request body"]
    KA --> ROUTER{"Model-aware\nrouter"}

    POLLER["Background poller\nGET /api/tags every 30 s\n(every host, every interval)"]
    POLLER -->|"installed model inventory"| ROUTER

    ROUTER -->|"model installed on host"| H1["Ollama Host A\nprimary · weight 2"]
    ROUTER -->|"model installed on host"| H2["Ollama Host B\nsecondary · weight 1"]
    ROUTER -->|"no match → weighted\nround-robin"| H1

    H1 -->|"connection failure"| H2
    H2 -->|"all hosts failed"| E502["503 X-Failover-Exhausted"]

    H1 --> RESP["Response\nX-Queue-Wait-Time\nX-Failover-Host\nX-Queue-Position"]
    H2 --> RESP
```

Requests from multiple consumers enter the proxy, are authenticated (or identity-injected for consumers without Bearer support), and placed into one of three priority tiers — capped to the ceiling on the presented key. A worker pool drains the tiers in order. For each request, the router prefers a host that has the model installed. Embedding requests check the cache first — hits skip the queue and upstream entirely. `keep_alive` is injected into request bodies so Ollama doesn't unload models between requests. Per-client concurrency caps prevent any single client from monopolizing the queue.

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
      scope: inference         # the default — may proxy, may not manage the queue
      max_concurrent: 0        # unlimited (subject to proxy.max_concurrent)
    - key: "sk-my-batch-key"
      client_id: "memsearch-watch"
      description: "Background embedding jobs"
      max_priority: low
      max_concurrent: 2        # cap at 2 concurrent so it can't starve interactive users
    - key: "sk-my-admin-key"
      client_id: "admin"
      description: "Admin"
      max_priority: high
      scope: management
    - key: "sk-my-readonly-key"
      client_id: "homepage"
      description: "Status widget — cannot spend GPU time"
      scope: read
```

Consumers pass their key as a Bearer token:

```
Authorization: Bearer sk-my-interactive-key
```

**Versus Ollama's built-in `OLLAMA_API_KEY`:** Ollama supports a single shared key — one key for all consumers, no per-client control. This proxy gives each consumer its own key with its own priority ceiling, concurrency cap, and optional management access.

**Priority ceilings:** a key with `max_priority: low` that sends `X-Queue-Priority: high` is silently capped to `low`. The caller doesn't know — it just gets queued at its allowed tier.

**Per-client concurrency caps:** `max_concurrent: N` limits a client to N simultaneous in-flight requests. Setting to `0` is unlimited. The cap must be ≤ `proxy.max_concurrent`. Different clients have independent semaphores — a capped batch client never blocks an interactive client.

### Key scopes

`scope` is one ordered axis with three cumulative levels. Each includes everything below it.

| Scope | Read status, summary, metrics, dashboard | Proxy inference to Ollama | Pause / resume / drain / flush |
|---|---|---|---|
| `read` | yes | no | no |
| `inference` *(default)* | yes | yes | no |
| `management` | yes | yes | yes |

`inference` is the default because it is what every key did before 0.5.0 — an existing config keeps working with no edit.

**`scope: read` is the point of the feature.** Before 0.5.0 there was no key that could not buy GPU time: `management: false` gated the four queue-control endpoints and nothing else, so any credential handed to a status widget also bought inference. A `read` key can see the proxy's own state and nothing upstream — it is refused at `/api/generate`, and also at cheap metadata reads like `/api/tags`, so the rule is "read sees the proxy, not Ollama" with no per-path exceptions to remember.

A valid key that is not authorized gets **403**, not 401 — authenticated, but not enough. The refusal names only the scope that was required, never the one the caller holds.

> **Deprecated in 0.5.0: `management: true`.** It still works and maps to `scope: management`, logging a warning at startup that names the `client_id`. Setting **both** `management: true` and a `scope` that is not `management` is a startup error rather than a silent precedence rule — a config that contradicts itself about a privilege should not boot and pick a winner. An explicit `management: false` is not a contradiction with any scope: it is the field's default, so it asserts nothing, and leaving those lines in place while adding `scope:` elsewhere is a supported migration.

> **With `auth.enabled: false` nothing is enforced.** No key is presented, so there is no scope to check — the same caveat that has always applied to `management`, now covering `scope` too. Every endpoint is open to anyone who can reach the port.

**MCP consumer support:** [jobsearch-mcp](https://github.com/TadMSTR/jobsearch-mcp) and [searxng-mcp](https://github.com/TadMSTR/searxng-mcp) both read `OLLAMA_API_KEY` from their environment and forward it as a Bearer token on all outgoing Ollama requests. Point them at the proxy and set their `OLLAMA_API_KEY` to their assigned key — no code changes required.

---

## Where API keys come from

A key may be given three ways, and **exactly one** per entry:

```yaml
auth:
  enabled: true
  keys:
    - key: "literal-key-in-this-file"       # simplest; fine for a private config
      client_id: open-webui
      max_priority: high

    - key_env: OQP_KEY_SEARXNG              # from the environment
      client_id: searxng-mcp
      max_priority: normal

    - key_file: /run/secrets/oqp-memsearch  # from a file — Docker/systemd secret style
      client_id: memsearch-watch
      max_priority: low
```

`key_file` strips trailing whitespace, because `echo secret > file` and most secret
managers leave a trailing newline, and a key that differs only by `\n` fails
authentication with nothing in the logs explaining why.

**Why this exists.** Every other setting can be supplied through the environment, but
`OQP_AUTH__KEYS__0__KEY` never worked: the override mechanism skips any path containing a
numeric component, and the list index trips it. So until 0.4.0 a literal in `config.yml`
was the only option — not as a design decision, but as a side effect. That is how API
keys end up committed to configuration repositories.

**Failure modes are loud and quiet in the right places.** A missing env var, an unreadable
file, zero sources, two sources, or a value that resolves to empty all fail at startup
with a message naming the `client_id` — and never the value. Resolved keys are excluded
from the model's `repr`, so a traceback or a debug dump of the config does not carry them.

> Migrating an existing deployment is a separate step from this mechanism. Keys already
> committed to a repository's history stay compromised until they are rotated; moving them
> to `key_env:` does not un-publish them.

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

When running multiple Ollama hosts (different GPUs or different model sets), the proxy can route each request to a host that already has the target model **installed**.

> **Installed, not loaded — and the difference matters.** The router reads `GET /api/tags`,
> which lists the models present *on disk*. It does not read `/api/ps`, which is what
> reports the models actually resident in VRAM. So this avoids sending a request to a host
> that would have to *pull* the model; it does **not** avoid cold-start latency, because a
> model installed on a host may still need loading into VRAM.
>
> The practical consequence: where every host has the same models pulled — the normal
> homelab case — `model_aware` has nothing to discriminate on and degenerates to weighted
> round-robin. Loaded-model routing via `/api/ps` is tracked as an enhancement, not a
> promise.

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

routing:
  strategy: model_aware            # model_aware | round_robin
  fallback: any_healthy            # when no host has the model: pick any healthy host
  model_poll_timeout: 3
```

**How it works:** a background poller hits `GET /api/tags` on each host every `model_sync_interval` seconds, maintaining a live `(host → installed_models)` map. Every host is polled every interval, whether or not it is currently reachable. Requests with a `model` field are routed to a host that has it. Weighted round-robin is deterministic (not stochastic) — a 2:1 weight ratio means exactly 2 requests to the heavy host for every 1 to the lighter host.

**Requests without a `model` field** use weighted round-robin across reachable hosts. If
*no* host is currently marked reachable, the proxy still picks one rather than refusing:
reachability is a cached observation, and one failed poll against a host that has since
recovered should not black-hole the proxy into 503s.

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

## Metadata fast path

`/api/tags`, `/api/version`, `/api/ps`, `/api/show` and `/` bypass the priority queue and
the worker pool. They are authenticated like everything else, then dispatched straight
upstream.

This matters more than it sounds. Before 0.4.0 every path entered the queue through the
catch-all handler, so with a small `max_concurrent` a UI polling `/api/tags` would queue
behind a multi-minute generation and appear to hang. Admission control should not be
spent on reads that carry no inference cost.

The trade-off: these paths are not subject to per-client concurrency caps. They are cheap
reads against a local daemon, bounded by the shared HTTP connection pool.

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

## What happens when a request waits too long

Each tier has a `max_wait`. **On reaching it a queued request fails — it is not promoted
to a higher tier.** The client receives:

```
HTTP 503
{"error": "request expired in queue", "request_id": "..."}
```

with no `Retry-After`. Expiry is evaluated when a worker picks the item up, so a request
is not cancelled while waiting — it is discarded at the moment it would otherwise have
been dispatched.

This is worth being deliberate about, because it is the behaviour that bites the exact
workload this proxy exists for: under sustained high-tier load, a `low`-tier background
indexer does not merely wait, it errors. For a background job that is often the right
answer — failing fast and retrying later beats holding a connection for ten minutes — but
it must be a choice, not a surprise. Set `queue.low.max_wait` to a value your batch client
is happy to fail at, and have it retry.

Priority aging (promoting a starved request rather than expiring it) is deliberately not
implemented; it is tracked as an enhancement.

---

## Memory ceiling on the queue

A queued request holds its entire buffered body until a worker takes it, so the real
memory ceiling is depth x body size, not depth. `queue.max_queued_mb` (default 512) caps
the **total bytes waiting across all tiers**; over it, the proxy answers `503` with a
`Retry-After`.

One deliberate exemption: when the queues are empty, a request is admitted even if its
body alone exceeds the cap. Otherwise a body larger than the ceiling could never be served
under any circumstances — refused against an empty queue forever, which is a deadlock
rather than backpressure.

Bytes are released when an item is dequeued. In-flight bodies are bounded separately by
`proxy.max_concurrent`.

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

Background polling (`GET /api/tags`, every `model_sync_interval` seconds) recovers
unhealthy hosts without a restart. Every host is polled on every interval, including
hosts that are currently healthy — so a model pulled on a live host becomes visible to
the router without a restart too.

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

## Migration from v0.2.x

v0.3.x is fully backward-compatible. No config changes required.

**v0.3.1** fixes a `Content-Length` off-by-one on non-streaming responses. Ollama appends a trailing newline to non-streaming JSON bodies; OQP's `JSONResponse` strips it, but Starlette only auto-computes `content-length` when the header is absent — the stale upstream value was winning, leaving `Content-Length` 1 byte too large. Consumers using httpx were seeing `RemoteProtocolError` on non-streaming calls. Fixed by stripping `content-length` and `transfer-encoding` from upstream headers before building the response.

**v0.3.0** adds the OpenAI-compat `/v1/embeddings` endpoint (documented under [Integration surface](#integration-surface)).

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

Returns full queue state, host health, per-client stats, routing decisions, and security config. The payload is nested — see `/queue/summary` below if you need flat values.

### `GET /queue/summary`

Flat scalars, nothing nested, no identity:

```json
{
  "status": "ok",
  "queued": 0,
  "active": 1,
  "max_concurrent": 4,
  "hosts_healthy": 1,
  "hosts_total": 1,
  "processed": 812,
  "rejected": 0,
  "expired": 0,
  "uptime_seconds": 18240
}
```

It exists because `/queue/status` cannot drive a status widget. Dashboard widgets map dot-paths to scalars; they cannot sum, filter, or count by predicate. Four of the six things you actually want on a tile are therefore underivable from `/queue/status`: total queued across tiers (three separate per-tier depths), healthy hosts versus total (`hosts` is an array, and counting `healthy: true` needs a predicate), total processed/rejected/expired (per-tier only), and active client count (`clients` is a map keyed by `client_id`).

Every value is a scalar and it carries no `client_id`, no host URL and no key count — it is the response most likely to end up on a wall display. `/queue/status` is unchanged; this is additive.

---

## Dashboard

A self-contained read-only HTML page at `/dashboard`, off by default:

```yaml
dashboard:
  enabled: true
  refresh_seconds: 5
```

```
http://localhost:11435/dashboard
```

It shows headline tiles (queued, active vs max, hosts up, processed, rejected, expired, uptime) and tables for per-tier depths, per-host health, and per-client counts. It polls `/queue/summary` and `/queue/status`, and stops polling while the browser tab is hidden.

- **Read-only.** It exposes no pause, resume, drain or flush control. Those need `management`, and this page is reachable with `read`, so a button would be dead UI for most callers and an escalation for the rest.
- **Disabled returns 404, not 401** — a feature you have turned off should not advertise itself to someone who cannot use it.
- **No new dependencies.** One HTML document from one route, inline CSS and JS, no build step and no CDN fetch, so it renders on an air-gapped host.
- **Not at `/`.** That path is proxied to Ollama, where it answers "Ollama is running"; taking it would break clients that probe the root.
- **Nonce-based CSP.** The page is sent with `default-src 'none'` and a per-response nonce for its two inline blocks — no `unsafe-inline`, so injected script is refused even if the page's own escaping were bypassed. Also `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `X-Frame-Options: DENY`.

**Reaching it with `auth.enabled: true`.** A browser cannot set an `Authorization` header on a normal navigation, so put the dashboard behind something that supplies the credential — a reverse proxy injecting the header, or a forward-auth layer that sets a session cookie. The page's own polls are same-origin relative requests sent with `credentials: same-origin`, so whatever authenticated the page authenticates them; no key is ever embedded in the HTML. With auth off it simply works in a browser.

---

## Homepage widget

[Homepage](https://gethomepage.dev) can render `/queue/summary` with the built-in `customapi` widget. Issue it a `scope: read` key so the credential in `services.yaml` cannot be used to spend GPU time:

```yaml
- Ollama Queue Proxy:
    icon: ollama.png
    href: http://forge:11435/dashboard
    widget:
      type: customapi
      url: http://forge:11435/queue/summary
      refreshInterval: 10000
      headers:
        Authorization: Bearer {{HOMEPAGE_FILE_OQP_READONLY_KEY}}
      mappings:
        - field: queued
          label: Queued
          format: number
        - field: active
          label: Active
          format: number
        - field: hosts_healthy
          label: Hosts up
          format: number
        - field: uptime_seconds
          label: Uptime
          format: duration
```

Notes that will otherwise cost you an afternoon:

- **Homepage substitutes only `{{HOMEPAGE_VAR_*}}` and `{{HOMEPAGE_FILE_*}}`, and the backing variable must carry that prefix.** A bare `${VAR}` is passed through verbatim, so the Bearer token becomes the literal string `${VAR}` and authentication fails. The `FILE_` form pairs with OQP's own `key_file:` — one secret on disk, read by both sides, present in neither config file.
- `format: duration` expects seconds, which is what `uptime_seconds` is.
- The `headers:` block is unnecessary when `auth.enabled: false`.

**Per-host list, no extra endpoint.** Homepage's `dynamic-list` display renders an array from the response, so `/queue/status`'s `hosts[]` maps directly:

```yaml
widget:
  type: customapi
  url: http://forge:11435/queue/status
  display: dynamic-list
  mappings:
    items: hosts
    name: name
    label: requests_handled
    format: number
```

`clients` **cannot** be rendered this way — `dynamic-list` requires an array and `clients` is a map keyed by `client_id`. It is not reshaped to enable this: `/queue/status` is a consumed HTTP surface and stability there is worth more than one widget.

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
| `GET /queue/status` | `read` | Full queue, host, client, security state (nested) |
| `GET /queue/summary` | `read` | Flat scalars for a dashboard tile — see below |
| `GET /metrics` | `read` | Prometheus text format |
| `GET /dashboard` | `read` | Embedded read-only HTML dashboard; 404 unless enabled |
| `POST /api/embed` | `inference` | Native Ollama embedding endpoint |
| `POST /v1/embeddings` | `inference` | OpenAI-compat embedding endpoint (see below) |
| `POST /queue/pause?tier=low` | `management` | Stop accepting requests for tier |
| `POST /queue/resume?tier=low` | `management` | Resume tier |
| `POST /queue/drain` | `management` | Wait for queues to empty |
| `POST /queue/flush?tier=low` | `management` | Drop all pending requests immediately |

Scopes apply only when `auth.enabled: true`. With auth off, every endpoint above is open.

### OpenAI-compat embeddings

OQP accepts `POST /v1/embeddings` using the OpenAI Embeddings API format and translates it to Ollama's `/api/embed` internally. The response is wrapped back into OpenAI format before returning. Auth, priority ceiling, and the embedding cache all apply identically to native `/api/embed` requests — the rewrite happens before any of those checks.

This lets clients that use the OpenAI SDK (e.g. Graphiti with `provider: openai`) route through OQP without changing their provider configuration. Point them at OQP's port instead of Ollama's:

```yaml
# Graphiti config — before
api_url: http://localhost:11434/v1

# Graphiti config — after (routes through OQP on port 11435)
api_url: http://localhost:11435/v1
```

The endpoint is always-on. No config toggle is required.

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

**Deprecated in 0.5.0:** `auth.keys[].management` is superseded by `auth.keys[].scope`.
`management: true` still works and maps to `scope: management`, logging a warning at
startup that names the `client_id`. See [Key scopes](#key-scopes).

**Deprecated in 0.4.0:** `ollama.health_check_interval` drove a second host-health loop
that no longer exists — `ollama.hosts[].model_sync_interval` is now the only poll
interval. Setting it logs a warning at startup and has no other effect.

All values can be overridden via env vars with `OQP_` prefix and `__` nesting:

```bash
OQP_PROXY__PORT=11435
OQP_OLLAMA__HOSTS__0__URL=http://ollama:11434
OQP_AUTH__ENABLED=true
OQP_ROUTING__STRATEGY=model_aware
OQP_EMBEDDING_CACHE__ENABLED=true
```

> **API keys cannot be set this way.** `_apply_env_overrides` skips any path with a
> numeric component, so `OQP_AUTH__KEYS__0__KEY` is silently ignored — the list index is
> what trips it. Use `key_env:` or `key_file:` instead; see
> [Where API keys come from](#where-api-keys-come-from).

See [`config.example.yml`](config.example.yml) for the full config with inline documentation.

---

## Building on top of this

**Prometheus scraping:**

Add a dedicated scraper key with `scope: read`. A scraper only ever reads `/metrics`, so it has no business being able to spend GPU time — and before 0.5.0 there was no way to express that:

```yaml
# config.yml
auth:
  enabled: true
  keys:
    - key: "sk-my-metrics-key"
      client_id: "prometheus-scraper"
      description: "Prometheus metrics scraper"
      scope: read
```

`max_priority` and `max_concurrent` are irrelevant on a `read` key — it never reaches the queue at all.

The recommended Docker pattern is a shared `prometheus-scrape` network so Prometheus reaches OQP by container name — no host port exposure required. Use `authorization.credentials` (not the legacy `bearer_token` field):

```yaml
# prometheus.yml
scrape_configs:
  - job_name: ollama-queue-proxy
    static_configs:
      - targets: ["ollama-queue-proxy:11435"]
    metrics_path: /metrics
    authorization:
      credentials: "sk-my-metrics-key"
```

Key metrics:
- `oqp_routing_decisions_total{reason}` — `model_match`, `round_robin`, `fallback`
- `oqp_host_models_installed{host}` — installed model count per host (renamed from `oqp_host_models_loaded` in 0.4.0)
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
