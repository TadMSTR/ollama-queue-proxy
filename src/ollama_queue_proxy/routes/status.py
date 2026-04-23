"""GET /queue/status and GET /health endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

if TYPE_CHECKING:
    from ..main import AppState

router = APIRouter()


def _pm_label(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@router.get("/health")
async def health():
    """Lightweight liveness probe — no auth required, no upstream check."""
    return {"status": "ok"}


@router.get("/queue/status")
async def queue_status(request: Request):
    state: AppState = request.app.state.oqp

    # Auth check (same as any other endpoint when enabled)
    _, err = await state.auth_manager.authenticate(request)
    if err:
        return err

    q_mgr = state.queue_manager
    depths = q_mgr.queue_depths()
    stats = q_mgr.stats()
    tier_cfgs = {
        "high": state.config.queue.high,
        "normal": state.config.queue.normal,
        "low": state.config.queue.low,
    }

    queue_data = {}
    for tier in ("high", "normal", "low"):
        s = stats[tier]
        queue_data[tier] = {
            "depth": depths[tier],
            "max_depth": tier_cfgs[tier].max_depth,
            "processed": s.processed,
            "rejected": s.rejected,
            "expired": s.expired,
        }

    hosts_data = []
    for host in state.host_manager.hosts:
        hosts_data.append({
            "name": host.name,
            "url": host.url,
            "healthy": host.healthy,
            "models": host.models,
            "last_checked": host.last_checked.isoformat() if host.last_checked else None,
            "requests_handled": host.requests_handled,
            "failures": host.failures,
        })

    uptime = (datetime.now(timezone.utc) - state.start_time).total_seconds()

    # Client stats
    clients_data = {}
    for client_id, cs in state.client_stats.items():
        clients_data[client_id] = {
            "description": cs.get("description"),
            "processed": cs.get("processed", 0),
            "rejected": cs.get("rejected", 0),
        }

    security_data = {
        "auth_enabled": state.config.auth.enabled,
        "model_management_allowed": state.config.proxy.allow_model_management,
        "active_keys": len(state.config.auth.keys),
    }

    return {
        "status": "ok",
        "uptime_seconds": int(uptime),
        "queue": queue_data,
        "concurrency": {
            "active": q_mgr.active_count(),
            "max": state.config.proxy.max_concurrent,
        },
        "hosts": hosts_data,
        "clients": clients_data,
        "security": security_data,
    }


@router.get("/metrics")
async def metrics(request: Request):
    """Prometheus text exposition format."""
    state: AppState = request.app.state.oqp

    # Auth mirrors /queue/status
    _, err = await state.auth_manager.authenticate(request)
    if err:
        return err

    q_mgr = state.queue_manager
    depths = q_mgr.queue_depths()
    stats = q_mgr.stats()
    uptime = (datetime.now(timezone.utc) - state.start_time).total_seconds()

    lines = [
        "# HELP oqp_queue_depth Current number of requests waiting in queue",
        "# TYPE oqp_queue_depth gauge",
    ]
    for tier in ("high", "normal", "low"):
        lines.append(f'oqp_queue_depth{{tier="{tier}"}} {depths[tier]}')

    lines += [
        "# HELP oqp_queue_processed_total Total requests dispatched from queue",
        "# TYPE oqp_queue_processed_total counter",
    ]
    for tier in ("high", "normal", "low"):
        lines.append(f'oqp_queue_processed_total{{tier="{tier}"}} {stats[tier].processed}')

    lines += [
        "# HELP oqp_queue_rejected_total Requests rejected due to full queue",
        "# TYPE oqp_queue_rejected_total counter",
    ]
    for tier in ("high", "normal", "low"):
        lines.append(f'oqp_queue_rejected_total{{tier="{tier}"}} {stats[tier].rejected}')

    lines += [
        "# HELP oqp_queue_expired_total Requests dropped due to max_wait timeout",
        "# TYPE oqp_queue_expired_total counter",
    ]
    for tier in ("high", "normal", "low"):
        lines.append(f'oqp_queue_expired_total{{tier="{tier}"}} {stats[tier].expired}')

    lines += [
        "# HELP oqp_concurrency_active Current active upstream requests",
        "# TYPE oqp_concurrency_active gauge",
        f"oqp_concurrency_active {q_mgr.active_count()}",
    ]

    lines += [
        "# HELP oqp_host_healthy Whether the host is currently healthy (1=healthy, 0=unhealthy)",
        "# TYPE oqp_host_healthy gauge",
    ]
    for host in state.host_manager.hosts:
        name = _pm_label(host.name)
        lines.append(f'oqp_host_healthy{{name="{name}"}} {1 if host.healthy else 0}')

    lines += [
        "# HELP oqp_host_requests_total Total requests handled by host",
        "# TYPE oqp_host_requests_total counter",
    ]
    for host in state.host_manager.hosts:
        name = _pm_label(host.name)
        lines.append(f'oqp_host_requests_total{{name="{name}"}} {host.requests_handled}')

    lines += [
        "# HELP oqp_host_failures_total Total upstream failures for host",
        "# TYPE oqp_host_failures_total counter",
    ]
    for host in state.host_manager.hosts:
        name = _pm_label(host.name)
        lines.append(f'oqp_host_failures_total{{name="{name}"}} {host.failures}')

    # Per-client concurrency metrics
    if state.concurrency_manager is not None:
        cm = state.concurrency_manager
        lines += [
            "# HELP oqp_client_inflight Current in-flight requests per client",
            "# TYPE oqp_client_inflight gauge",
        ]
        for cid, count in cm.inflight_counts().items():
            lines.append(f'oqp_client_inflight{{client_id="{_pm_label(cid)}"}} {count}')

        lines += [
            "# HELP oqp_client_cap_waiting Requests waiting on per-client concurrency cap",
            "# TYPE oqp_client_cap_waiting gauge",
        ]
        for cid, count in cm.cap_waiting_counts().items():
            lines.append(f'oqp_client_cap_waiting{{client_id="{_pm_label(cid)}"}} {count}')

    # Embedding cache metrics
    if state.embedding_cache is not None:
        from ..cache import errors as cache_errors
        from ..cache import hits as cache_hits
        from ..cache import misses as cache_misses

        lines += [
            "# HELP oqp_embedding_cache_hits_total Embedding cache hits",
            "# TYPE oqp_embedding_cache_hits_total counter",
        ]
        for label, count in cache_hits.items():
            client_id, model, endpoint = label.split(",", 2)
            lines.append(
                f'oqp_embedding_cache_hits_total{{client="{_pm_label(client_id)}",model="{_pm_label(model)}",'
                f'endpoint="{_pm_label(endpoint)}"}} {count}'
            )

        lines += [
            "# HELP oqp_embedding_cache_misses_total Embedding cache misses",
            "# TYPE oqp_embedding_cache_misses_total counter",
        ]
        for label, count in cache_misses.items():
            client_id, model, endpoint = label.split(",", 2)
            lines.append(
                f'oqp_embedding_cache_misses_total{{client="{_pm_label(client_id)}",model="{_pm_label(model)}",'
                f'endpoint="{_pm_label(endpoint)}"}} {count}'
            )

        lines += [
            "# HELP oqp_embedding_cache_errors_total Embedding cache RESP errors by kind",
            "# TYPE oqp_embedding_cache_errors_total counter",
        ]
        for kind, count in cache_errors.items():
            lines.append(f'oqp_embedding_cache_errors_total{{kind="{_pm_label(kind)}"}} {count}')

    # Routing table metrics (model_aware strategy only)
    if state.routing_table is not None:
        rt = state.routing_table
        lines += [
            "# HELP oqp_host_models_loaded Number of models currently loaded on each host",
            "# TYPE oqp_host_models_loaded gauge",
        ]
        for host_name, count in rt.host_model_counts().items():
            lines.append(f'oqp_host_models_loaded{{host="{_pm_label(host_name)}"}} {count}')

        lines += [
            "# HELP oqp_routing_decisions_total Routing decisions by reason",
            "# TYPE oqp_routing_decisions_total counter",
        ]
        for reason, count in rt.routing_decisions.items():
            lines.append(f'oqp_routing_decisions_total{{reason="{_pm_label(reason)}"}} {count}')

    lines += [
        "# HELP oqp_uptime_seconds Proxy uptime in seconds since last start",
        "# TYPE oqp_uptime_seconds gauge",
        f"oqp_uptime_seconds {int(uptime)}",
    ]

    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
