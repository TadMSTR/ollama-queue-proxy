"""Queue management endpoints: pause, resume, drain, flush."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from ..main import AppState

router = APIRouter()


async def _require_management(request: Request) -> JSONResponse | None:
    """Check that the request has a management-capable key (or auth is disabled)."""
    state: AppState = request.app.state.oqp
    key_cfg, err = await state.auth_manager.authenticate(request)
    if err:
        return err
    if state.config.auth.enabled:
        # key_cfg is set (auth succeeded), now check management flag
        if key_cfg is None or not key_cfg.management:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "management permission required",
                    "request_id": getattr(request.state, "request_id", "unknown"),
                },
            )
    return None


@router.post("/queue/pause")
async def queue_pause(request: Request, tier: str | None = None):
    err = await _require_management(request)
    if err:
        return err
    state: AppState = request.app.state.oqp
    state.queue_manager.pause(tier)
    return {"status": "paused", "tier": tier or "all"}


@router.post("/queue/resume")
async def queue_resume(request: Request, tier: str | None = None):
    err = await _require_management(request)
    if err:
        return err
    state: AppState = request.app.state.oqp
    state.queue_manager.resume(tier)
    return {"status": "resumed", "tier": tier or "all"}


@router.post("/queue/drain")
async def queue_drain(request: Request):
    err = await _require_management(request)
    if err:
        return err
    state: AppState = request.app.state.oqp
    await state.queue_manager.drain()
    return {"status": "drained"}


@router.post("/queue/flush")
async def queue_flush(request: Request, tier: str | None = None):
    err = await _require_management(request)
    if err:
        return err
    state: AppState = request.app.state.oqp
    dropped = await state.queue_manager.flush(tier)
    return {"status": "flushed", "tier": tier or "all", "dropped": dropped}
