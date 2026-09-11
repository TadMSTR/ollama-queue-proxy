"""Authorization boundary on the management API.

`routes/queue.py` is the privileged surface — pause, resume, drain, flush — and it
sat at 26% coverage while being the thing that can stop the proxy serving anyone.
Under an admission-control framing the policy surface IS the product, so it gets
covered like one.

EVERY CHECK HERE IS TWO-SIDED. For each endpoint: a management key succeeds AND a
non-management key is refused. An assertion with no control is not a gate — a
`_require_management` that returned None unconditionally would satisfy every
"management key works" test in this file on its own, and a version that refused
everyone would satisfy every "non-management key is refused" test. Only the pair
distinguishes a working check from either failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ollama_queue_proxy.auth import AuthManager
from ollama_queue_proxy.config import ApiKeyConfig
from ollama_queue_proxy.middleware import RequestContextMiddleware
from ollama_queue_proxy.routes import queue as queue_routes
from ollama_queue_proxy.routes import status as status_routes
from tests.conftest import make_config

MANAGEMENT_KEY = "mgmt-key-0000000000000000"
PLAIN_KEY = "plain-key-111111111111111"

MANAGEMENT_KEY_CFG = ApiKeyConfig(
    key=MANAGEMENT_KEY, client_id="admin", max_priority="high", management=True
)
PLAIN_KEY_CFG = ApiKeyConfig(
    key=PLAIN_KEY, client_id="consumer", max_priority="normal", management=False
)

MANAGEMENT_ENDPOINTS = ["/queue/pause", "/queue/resume", "/queue/drain", "/queue/flush"]


def build_client(auth_enabled: bool = True) -> TestClient:
    cfg = make_config(
        auth_enabled=auth_enabled,
        keys=[MANAGEMENT_KEY_CFG, PLAIN_KEY_CFG],
    )
    cfg.auth.enabled = auth_enabled

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(queue_routes.router)
    app.include_router(status_routes.router)

    state = MagicMock()
    state.config = cfg
    state.auth_manager = AuthManager(cfg.auth)
    state.queue_manager = MagicMock()
    state.queue_manager.drain = AsyncMock()
    state.queue_manager.flush = AsyncMock(return_value=3)
    app.state.oqp = state

    client = TestClient(app)
    client.app_state = state  # type: ignore[attr-defined]
    return client


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# The boundary, both sides, for every privileged endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", MANAGEMENT_ENDPOINTS)
def test_management_key_is_accepted(endpoint):
    client = build_client()
    resp = client.post(endpoint, headers=_auth(MANAGEMENT_KEY))
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("endpoint", MANAGEMENT_ENDPOINTS)
def test_non_management_key_is_refused(endpoint):
    """The control for the test above. A valid credential is not an authorised one:
    this key authenticates fine and must still be refused 403, not 401."""
    client = build_client()
    resp = client.post(endpoint, headers=_auth(PLAIN_KEY))
    assert resp.status_code == 403, resp.text
    assert "management permission required" in resp.text


@pytest.mark.parametrize("endpoint", MANAGEMENT_ENDPOINTS)
def test_unauthenticated_is_refused(endpoint):
    client = build_client()
    resp = client.post(endpoint)
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("endpoint", MANAGEMENT_ENDPOINTS)
def test_invalid_key_is_refused(endpoint):
    client = build_client()
    resp = client.post(endpoint, headers=_auth("not-a-real-key-at-all"))
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("endpoint", MANAGEMENT_ENDPOINTS)
def test_refused_requests_do_not_reach_the_queue_manager(endpoint):
    """Status codes are not the whole boundary. A 403 returned AFTER the side effect
    already happened is not a refusal, and the response looks identical either way."""
    client = build_client()
    client.post(endpoint, headers=_auth(PLAIN_KEY))

    qm = client.app_state.queue_manager  # type: ignore[attr-defined]
    qm.pause.assert_not_called()
    qm.resume.assert_not_called()
    qm.drain.assert_not_awaited()
    qm.flush.assert_not_awaited()


def test_accepted_request_does_reach_the_queue_manager():
    """CONTROL for the above: proves those assert_not_called checks can fail, rather
    than passing because the mock is never wired to anything."""
    client = build_client()
    resp = client.post("/queue/pause?tier=low", headers=_auth(MANAGEMENT_KEY))
    assert resp.status_code == 200
    client.app_state.queue_manager.pause.assert_called_once_with("low")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Authorization is evaluated before attacker-supplied input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", ["/queue/pause", "/queue/resume", "/queue/flush"])
def test_bad_tier_from_an_unauthorised_caller_is_401_not_400(endpoint):
    """Tier validation used to run FIRST, so an unauthenticated caller probing these
    endpoints received a 400 enumerating the accepted tier values instead of a 401 —
    an answer to a question it was not entitled to ask. The disclosure is small (the
    tiers are in the README); the ordering is the point."""
    client = build_client()
    resp = client.post(f"{endpoint}?tier=bogus")
    assert resp.status_code == 401, resp.text
    assert "invalid tier" not in resp.text


@pytest.mark.parametrize("endpoint", ["/queue/pause", "/queue/resume", "/queue/flush"])
def test_bad_tier_from_an_authorised_caller_is_still_400(endpoint):
    """CONTROL: reordering must not have disabled tier validation outright."""
    client = build_client()
    resp = client.post(f"{endpoint}?tier=bogus", headers=_auth(MANAGEMENT_KEY))
    assert resp.status_code == 400, resp.text
    assert "invalid tier" in resp.text


# ---------------------------------------------------------------------------
# The auth-off caveat, asserted rather than described
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", MANAGEMENT_ENDPOINTS)
def test_management_endpoints_are_open_when_auth_is_disabled(endpoint):
    """This is the documented behaviour, not a bug — with auth off there is no key to
    carry a management flag. It is asserted here so the README's auth-off caveat is
    backed by a test: anyone who can reach the port can pause the queue. If this ever
    becomes a deliberate lockdown, this test is the one that should fail and force
    the README to be updated with it.
    """
    client = build_client(auth_enabled=False)
    resp = client.post(endpoint)
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


def test_health_is_unauthenticated():
    """Liveness must answer without a credential or it cannot serve as a healthcheck."""
    client = build_client()
    resp = client.get("/health")
    assert resp.status_code == 200


def test_queue_status_reports_hosts_from_the_routing_table():
    """/queue/status reads host state, which moved from HostManager to RoutingTable in
    0.4.0. The JSON keys `healthy` and `models` are a consumed HTTP surface and must
    survive that rename."""
    from ollama_queue_proxy.routing import HostRoutingState

    client = build_client()
    state = client.app_state  # type: ignore[attr-defined]
    host = HostRoutingState(
        url="http://ollama-test:11434", name="test", weight=1, model_sync_interval=30
    )
    host.installed_models = {"llama3", "mistral"}
    host.reachable = True
    state.routing_table.hosts = [host]
    state.queue_manager.queue_depths.return_value = {"high": 0, "normal": 0, "low": 0}
    state.queue_manager.stats.return_value = {
        t: MagicMock(processed=0, rejected=0, expired=0) for t in ("high", "normal", "low")
    }
    state.client_stats = {}

    resp = client.get("/queue/status", headers=_auth(MANAGEMENT_KEY))
    assert resp.status_code == 200, resp.text
    hosts = resp.json()["hosts"]
    assert hosts[0]["name"] == "test"
    assert hosts[0]["healthy"] is True, "the JSON key must stay `healthy` after the rename"
    assert sorted(hosts[0]["models"]) == ["llama3", "mistral"]
