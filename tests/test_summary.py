"""GET /queue/summary — the flat-scalar rollup that drives a Homepage tile.

The contract is FLATNESS, and it is the one thing here that must not regress. A nested
value does not raise: `customapi` renders it as a blank row, in someone else's dashboard,
weeks after the change that caused it. So the shape is asserted structurally rather than
field by field.

The rollups are tested with DISTINCT, NON-ZERO inputs throughout. An all-zeros fixture
makes sum(), max(), len() and `return 0` indistinguishable, and every one of them would
pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ollama_queue_proxy.auth import AuthManager
from ollama_queue_proxy.config import ApiKeyConfig
from ollama_queue_proxy.middleware import RequestContextMiddleware
from ollama_queue_proxy.routes import status as status_routes
from ollama_queue_proxy.routing import HostRoutingState
from tests.conftest import make_config

READ_KEY = "summary-read-key-00000000"
READ_KEY_CFG = ApiKeyConfig(key=READ_KEY, client_id="watcher", scope="read")

# Distinct and non-zero on every axis, so no two rollups can be confused and no
# accidental constant can pass for a sum.
DEPTHS = {"high": 3, "normal": 7, "low": 11}
PROCESSED = {"high": 100, "normal": 200, "low": 400}
REJECTED = {"high": 1, "normal": 2, "low": 4}
EXPIRED = {"high": 10, "normal": 20, "low": 40}


def _host(name: str, reachable: bool) -> HostRoutingState:
    h = HostRoutingState(
        url=f"http://{name}.internal:11434", name=name, weight=1, model_sync_interval=30
    )
    h.reachable = reachable
    return h


def build_client(auth_enabled: bool = True, hosts: list | None = None) -> TestClient:
    cfg = make_config(auth_enabled=auth_enabled, keys=[READ_KEY_CFG], max_concurrent=4)
    cfg.auth.enabled = auth_enabled

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(status_routes.router)

    state = MagicMock()
    state.config = cfg
    state.auth_manager = AuthManager(cfg.auth)
    state.start_time = datetime.now(UTC) - timedelta(seconds=18240)
    state.queue_manager = MagicMock()
    state.queue_manager.queue_depths.return_value = dict(DEPTHS)
    state.queue_manager.active_count.return_value = 2
    state.queue_manager.stats.return_value = {
        t: MagicMock(processed=PROCESSED[t], rejected=REJECTED[t], expired=EXPIRED[t])
        for t in ("high", "normal", "low")
    }
    state.queue_manager.drain = AsyncMock()
    # Two healthy of three, so hosts_healthy and hosts_total cannot coincide.
    state.routing_table.hosts = (
        hosts
        if hosts is not None
        else [_host("alpha", True), _host("beta", False), _host("gamma", True)]
    )
    state.client_stats = {"open-webui": {"description": "d", "processed": 5, "rejected": 0}}
    app.state.oqp = state

    client = TestClient(app)
    client.app_state = state  # type: ignore[attr-defined]
    return client


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _body(**kw) -> dict:
    resp = build_client(**kw).get("/queue/summary", headers=_auth(READ_KEY))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# The flatness contract
# ---------------------------------------------------------------------------


def test_every_value_is_a_scalar():
    """THE contract with customapi. A dict or list renders as a blank row rather than an
    error, so nothing upstream of the dashboard would ever report this."""
    body = _body()
    nested = {k: v for k, v in body.items() if isinstance(v, dict | list)}
    assert nested == {}, f"non-scalar fields break the widget: {nested}"


def test_the_flatness_check_can_actually_fail():
    """CONTROL for the assertion above, not for the endpoint. `isinstance(v, dict|list)`
    silently matches nothing if the expression is wrong, and then the test passes on any
    payload at all — including the nested one it exists to reject."""
    nested_sample = {"status": "ok", "queue": {"high": 1}}
    assert any(isinstance(v, dict | list) for v in nested_sample.values())


def test_queue_status_remains_nested_and_is_not_replaced():
    """CONTROL on the premise. If /queue/status were itself flat, this endpoint would
    have no reason to exist — and /queue/status is a consumed surface that v0.4.0 went
    out of its way to keep stable."""
    client = build_client()
    body = client.get("/queue/status", headers=_auth(READ_KEY)).json()
    assert isinstance(body["queue"], dict)
    assert isinstance(body["hosts"], list)


# ---------------------------------------------------------------------------
# The rollups — the four things customapi cannot derive for itself
# ---------------------------------------------------------------------------


def test_queued_sums_the_three_tier_depths():
    body = _body()
    assert body["queued"] == sum(DEPTHS.values()) == 21
    # The depths are distinct and non-zero, so a sum cannot be mistaken for any single
    # tier, for the max, or for the tier count.
    assert body["queued"] not in set(DEPTHS.values()) | {len(DEPTHS), max(DEPTHS.values())}


@pytest.mark.parametrize(
    "field,source",
    [("processed", PROCESSED), ("rejected", REJECTED), ("expired", EXPIRED)],
)
def test_counters_sum_across_tiers(field, source):
    body = _body()
    assert body[field] == sum(source.values())
    assert body[field] not in set(source.values())


def test_the_three_counters_are_not_interchangeable():
    """Distinct magnitudes per counter, so a handler that read `processed` three times —
    or wired the fields in the wrong order — cannot pass."""
    body = _body()
    assert body["processed"] != body["rejected"] != body["expired"]
    assert len({body["processed"], body["rejected"], body["expired"]}) == 3


def test_hosts_healthy_counts_the_predicate_and_not_the_array():
    """The mixed fixture is the point. Where every host is healthy, a correct predicate
    and a bare len(hosts) are indistinguishable — which is the bug `format: size` on the
    raw array already has."""
    body = _body()
    assert body["hosts_total"] == 3
    assert body["hosts_healthy"] == 2
    assert body["hosts_healthy"] < body["hosts_total"]


def test_hosts_healthy_equals_total_when_all_are_healthy():
    """CONTROL: the predicate must not simply be len(hosts) - 1."""
    body = _body(hosts=[_host("a", True), _host("b", True)])
    assert body["hosts_healthy"] == body["hosts_total"] == 2


def test_hosts_healthy_is_zero_when_none_are():
    body = _body(hosts=[_host("a", False), _host("b", False)])
    assert body["hosts_healthy"] == 0
    assert body["hosts_total"] == 2


def test_no_hosts_configured_reports_zero_rather_than_failing():
    body = _body(hosts=[])
    assert body["hosts_healthy"] == 0 and body["hosts_total"] == 0


def test_active_and_max_concurrent_are_reported_separately():
    body = _body()
    assert body["active"] == 2
    assert body["max_concurrent"] == 4


def test_uptime_is_reported_in_whole_seconds():
    """`format: duration` in the widget expects seconds."""
    body = _body()
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] == pytest.approx(18240, abs=5)


# ---------------------------------------------------------------------------
# No identity in the payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("auth_enabled", [True, False])
def test_the_payload_carries_no_identity(auth_enabled):
    """This is the response most likely to end up on a wall display, so it names nobody
    and nothing reachable — regardless of whether it was authenticated to get here."""
    client = build_client(auth_enabled=auth_enabled)
    resp = client.get("/queue/summary", headers=_auth(READ_KEY))
    body, raw = resp.json(), resp.text

    for forbidden in ("client_id", "clients", "security", "active_keys", "description"):
        assert forbidden not in body, f"{forbidden} must not be in the summary"
    # The fixture deliberately populates client_stats and gives hosts real names and
    # URLs, so these are absences the endpoint produces rather than an empty fixture.
    for leaked in ("open-webui", "watcher", "alpha", "internal", "11434", READ_KEY):
        assert leaked not in raw, f"{leaked!r} leaked into the summary payload"


def test_the_leak_fixture_is_not_vacuous():
    """CONTROL for the test above: those strings ARE present in /queue/status, so their
    absence from the summary is a property of the summary, not of the fixture."""
    client = build_client()
    raw = client.get("/queue/status", headers=_auth(READ_KEY)).text
    for present in ("open-webui", "alpha", "11434"):
        assert present in raw


def test_the_field_set_is_exactly_the_documented_contract():
    """Pins the README's widget sample and the dashboard's poll shape to the handler.
    An added field is a decision, not a diff nobody reads."""
    assert set(_body()) == {
        "status",
        "queued",
        "active",
        "max_concurrent",
        "hosts_healthy",
        "hosts_total",
        "processed",
        "rejected",
        "expired",
        "uptime_seconds",
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_a_read_key_is_accepted():
    assert build_client().get("/queue/summary", headers=_auth(READ_KEY)).status_code == 200


def test_no_key_is_refused_when_auth_is_enabled():
    assert build_client().get("/queue/summary").status_code == 401


def test_an_invalid_key_is_refused():
    assert build_client().get("/queue/summary", headers=_auth("nope-0000")).status_code == 401


def test_no_key_is_accepted_when_auth_is_disabled():
    """The documented auth-off caveat, which covers this endpoint like any other."""
    assert build_client(auth_enabled=False).get("/queue/summary").status_code == 200
