"""Queue management endpoints: pause, resume, drain, flush."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from ..main import AppState

router = APIRouter()

_VALID_TIERS = {"high", "normal", "low"}


def _validate_tier(tier: str | None, request_id: str) -> JSONResponse | None:
    """Return error response if tier is provided but invalid."""
    if tier is not None and tier not in _VALID_TIERS:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"invalid tier: {tier!r} (must be high, normal, or low)",
                "request_id": request_id,
            },
        )
    return None


async def _require_management(request: Request) -> JSONResponse | None:
    """Check that the request has a management-capable key (or auth is disabled)."""
    state: AppState = request.app.state.oqp
    key_cfg, err = await state.auth_manager.authenticate(request)
    if err:
        return err
    # With auth disabled there is no key to carry a management flag, so the route is
    # open — see the auth-off caveat in the README.
    if state.config.auth.enabled and (key_cfg is None or not key_cfg.management):
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
    # Authorization BEFORE input validation. Reversed, an unauthenticated caller
    # probing these endpoints gets a 400 describing the accepted tier values rather
    # than a 401 — answering a question it was never entitled to ask. The disclosure
    # is small here (the tier names are in the README), but the ordering is the
    # thing: a privileged endpoint should not evaluate attacker-supplied input
    # before establishing who is asking.
    err = await _require_management(request)
    if err:
        return err
    request_id = getattr(request.state, "request_id", "unknown")
    tier_err = _validate_tier(tier, request_id)
    if tier_err:
        return tier_err
    state: AppState = request.app.state.oqp
    state.queue_manager.pause(tier)
    return {"status": "paused", "tier": tier or "all"}


@router.post("/queue/resume")
async def queue_resume(request: Request, tier: str | None = None):
    # Authorization before input validation — see queue_pause.
    err = await _require_management(request)
    if err:
        return err
    request_id = getattr(request.state, "request_id", "unknown")
    tier_err = _validate_tier(tier, request_id)
    if tier_err:
        return tier_err
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
    # Authorization before input validation — see queue_pause.
    err = await _require_management(request)
    if err:
        return err
    request_id = getattr(request.state, "request_id", "unknown")
    tier_err = _validate_tier(tier, request_id)
    if tier_err:
        return tier_err
    state: AppState = request.app.state.oqp
    dropped = await state.queue_manager.flush(tier)
    return {"status": "flushed", "tier": tier or "all", "dropped": dropped}
