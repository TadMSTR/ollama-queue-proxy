"""The `scope` axis on API keys: read < inference < management.

`scope: read` is a NEW SECURITY CLAIM, not a refactor of the old `management: bool`.
Before 0.5.0 there was no key that could not buy GPU time — `management: false` gated
the four POST management endpoints and nothing else, so every credential could proxy
inference. A read-only key is only true if EVERY path to Ollama refuses it, and a
partially-wired scope ships a guarantee the code does not keep, which is worse than not
shipping the feature.

So the denials are the assertions that matter here, and each one has its control. A
matrix that only checked grants would pass against an `allows()` that returned True
unconditionally; one that only checked denials would pass against one that returned
False. Only both together distinguish a working gate from either failure.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from ollama_queue_proxy.auth import AuthManager
from ollama_queue_proxy.config import ApiKeyConfig
from ollama_queue_proxy.middleware import RequestContextMiddleware
from ollama_queue_proxy.routes import queue as queue_routes
from ollama_queue_proxy.routes import status as status_routes
from tests.conftest import make_config

SCOPES = ("read", "inference", "management")

KEYS: dict[str, ApiKeyConfig] = {
    "read": ApiKeyConfig(key="scope-read-key-000000000", client_id="watcher", scope="read"),
    "inference": ApiKeyConfig(
        key="scope-infer-key-11111111", client_id="consumer", scope="inference"
    ),
    "management": ApiKeyConfig(
        key="scope-mgmt-key-222222222", client_id="admin", scope="management"
    ),
}

# The table from the build plan, transcribed as DATA rather than derived from
# SCOPE_ORDER. Computing the expectation from the same constant the implementation
# reads would let a reordering of SCOPE_ORDER move both sides together and still pass —
# the grid would agree with the code about a permission model neither of them got right.
GRANTS: dict[str, dict[str, bool]] = {
    #  surface        read    inference  management
    "status": {"read": True, "inference": True, "management": True},
    "summary": {"read": True, "inference": True, "management": True},
    "metrics": {"read": True, "inference": True, "management": True},
    "inference": {"read": False, "inference": True, "management": True},
    "management": {"read": False, "inference": False, "management": True},
}
SURFACES = tuple(GRANTS)


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def build_route_client(keys: list[ApiKeyConfig] | None = None, auth_enabled: bool = True):
    """TestClient over the status + queue routers with a mocked AppState."""
    cfg = make_config(auth_enabled=auth_enabled, keys=keys or list(KEYS.values()))
    cfg.auth.enabled = auth_enabled

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(status_routes.router)
    app.include_router(queue_routes.router)

    state = MagicMock()
    state.config = cfg
    state.auth_manager = AuthManager(cfg.auth)
    state.queue_manager = MagicMock()
    state.queue_manager.drain = AsyncMock()
    state.queue_manager.flush = AsyncMock(return_value=0)
    state.queue_manager.queue_depths.return_value = {"high": 0, "normal": 0, "low": 0}
    state.queue_manager.stats.return_value = {
        t: MagicMock(processed=0, rejected=0, expired=0) for t in ("high", "normal", "low")
    }
    state.queue_manager.active_count.return_value = 0
    state.start_time = datetime.now(UTC) - timedelta(seconds=60)
    state.routing_table.hosts = []
    state.routing_table.host_model_counts.return_value = {}
    state.routing_table.routing_decisions = {}
    state.client_stats = {}
    state.embedding_cache = None
    state.concurrency_manager = None
    app.state.oqp = state

    client = TestClient(app)
    client.app_state = state  # type: ignore[attr-defined]
    return client


def _fake_request(path: str = "/api/chat", method: str = "POST"):
    from fastapi import Request

    request = Request(
        {"type": "http", "method": method, "path": path, "query_string": b"", "headers": []}
    )
    request.state.request_id = "scope-test"
    return request


async def _attempt_inference(key_cfg: ApiKeyConfig | None) -> int:
    """Drive the proxy chokepoint with `key_cfg` and report the status code.

    Calls the real `_enqueue_request`. Upstream dispatch is stubbed, so a 200 means the
    request was ALLOWED THROUGH to Ollama — which is exactly what a read key must never
    achieve.
    """
    from ollama_queue_proxy import main

    cfg = make_config(auth_enabled=True, keys=list(KEYS.values()))
    state = MagicMock()
    state.config = cfg
    state.client_stats = {}
    state.embedding_cache = None
    state.concurrency_manager = None
    state.queue_manager = MagicMock()

    async def _run(item):
        item.future.set_result(JSONResponse(status_code=200, content={"ok": True}))
        return 1

    state.queue_manager.enqueue = AsyncMock(side_effect=_run)

    with (
        patch(
            "ollama_queue_proxy.main.read_body",
            new=AsyncMock(return_value=(b'{"model":"llama3"}', None)),
        ),
        patch("ollama_queue_proxy.main.dispatch_request", new=AsyncMock()),
    ):
        resp = await main._enqueue_request(
            request=_fake_request(),
            client_id="whoever",
            tier="normal",
            state=state,
            key_cfg=key_cfg,
        )
    return resp.status_code


async def _attempt(surface: str, scope: str) -> int:
    if surface == "inference":
        return await _attempt_inference(KEYS[scope])
    client = build_route_client()
    headers = _auth(KEYS[scope].key)
    if surface == "status":
        return client.get("/queue/status", headers=headers).status_code
    if surface == "summary":
        return client.get("/queue/summary", headers=headers).status_code
    if surface == "metrics":
        return client.get("/metrics", headers=headers).status_code
    if surface == "management":
        return client.post("/queue/pause", headers=headers).status_code
    raise AssertionError(f"unknown surface {surface!r}")


# ---------------------------------------------------------------------------
# The matrix — 3 scopes x 5 surfaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.asyncio
async def test_scope_matrix(scope: str, surface: str):
    """3 scopes x 5 surfaces. Read the GRANTS table above for the expected outcome."""
    expected_ok = GRANTS[surface][scope]
    code = await _attempt(surface, scope)
    if expected_ok:
        assert code == 200, f"scope={scope} must be granted {surface}, got {code}"
    else:
        assert code == 403, f"scope={scope} must be REFUSED {surface}, got {code}"


def test_the_matrix_contains_both_outcomes():
    """Guards the grid itself. An all-True or all-False table would still produce twelve
    green assertions above while testing nothing — the failure mode is in the fixture,
    not the code, so it cannot be caught by the parametrised test."""
    cells = [v for row in GRANTS.values() for v in row.values()]
    assert True in cells and False in cells
    assert len(cells) == 15


# ---------------------------------------------------------------------------
# `scope: read` cannot reach Ollama AT ALL — including the metadata fast path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/tags", "/api/version", "/api/ps", "/api/show", "/"])
@pytest.mark.asyncio
async def test_read_scope_is_refused_even_on_metadata_fast_path(path):
    """The fast path skips the queue and the semaphore, and it would be easy to let it
    skip authorisation with them — it is the branch that returns before every other
    check in the function. `read` means "the proxy's own state, nothing upstream", with
    no per-path carve-out to remember."""
    from ollama_queue_proxy import main

    cfg = make_config(auth_enabled=True, keys=list(KEYS.values()))
    state = MagicMock()
    state.config = cfg
    state.client_stats = {}

    with (
        patch(
            "ollama_queue_proxy.main.read_body", new=AsyncMock(return_value=(b"", None))
        ) as mock_read,
        patch("ollama_queue_proxy.main.dispatch_request", new=AsyncMock()) as mock_dispatch,
    ):
        resp = await main._enqueue_request(
            request=_fake_request(path, method="GET"),
            client_id="watcher",
            tier="normal",
            state=state,
            key_cfg=KEYS["read"],
        )

    assert resp.status_code == 403
    mock_dispatch.assert_not_awaited(), "a refused request must not reach a host"
    (
        mock_read.assert_not_awaited(),
        (
            "authorisation must precede body buffering — otherwise a read key can still "
            "make the proxy allocate memory for a body it will never dispatch"
        ),
    )


@pytest.mark.asyncio
async def test_inference_scope_reaches_the_metadata_fast_path():
    """CONTROL for the above: proves the fast path is reachable at all, so the five
    assertions there are measuring the scope gate rather than a broken fixture."""
    from ollama_queue_proxy import main

    cfg = make_config(auth_enabled=True, keys=list(KEYS.values()))
    state = MagicMock()
    state.config = cfg
    state.client_stats = {}
    sentinel = JSONResponse(status_code=200, content={"ok": True})

    with (
        patch("ollama_queue_proxy.main.read_body", new=AsyncMock(return_value=(b"", None))),
        patch(
            "ollama_queue_proxy.main.dispatch_request", new=AsyncMock(return_value=sentinel)
        ) as mock_dispatch,
    ):
        resp = await main._enqueue_request(
            request=_fake_request("/api/tags", method="GET"),
            client_id="consumer",
            tier="normal",
            state=state,
            key_cfg=KEYS["inference"],
        )

    assert resp is sentinel
    mock_dispatch.assert_awaited_once()


# ---------------------------------------------------------------------------
# The injection listener — the easiest bypass to leave open
# ---------------------------------------------------------------------------


def _injection_client(key_cfg: ApiKeyConfig, auth_enabled: bool):
    from ollama_queue_proxy import injection as inj_mod

    cfg = make_config(auth_enabled=auth_enabled, keys=[key_cfg])
    cfg.auth.enabled = auth_enabled
    state = MagicMock()
    state.shutting_down = False
    state.config = cfg
    state.client_stats = {}
    state.embedding_cache = None
    state.concurrency_manager = None
    state.queue_manager = MagicMock()

    async def _run(item):
        item.future.set_result(JSONResponse(status_code=200, content={"ok": True}))
        return 1

    state.queue_manager.enqueue = AsyncMock(side_effect=_run)
    inj_mod.set_shared_state(state)
    app = inj_mod.make_injection_app(key_cfg.client_id, key_cfg)
    return TestClient(app), inj_mod


@pytest.mark.parametrize("auth_enabled", [True, False])
def test_injection_listener_refuses_a_read_key(auth_enabled):
    """An injection port presents no Bearer token — the identity comes from `inject_as`
    in config — so nothing else in the request would ever consult a scope. Before this
    build the listener honoured only `max_priority`, which meant a read-only key
    injected on a listener port could still proxy inference.

    Enforced with auth OFF as well as on, matching how `max_priority` already behaves
    there: the identity is declared by the operator rather than asserted by the caller,
    so the policy attached to it is meaningful either way.
    """
    key_cfg = ApiKeyConfig(key="inj-read-key-3333333333", client_id="watcher", scope="read")
    client, inj_mod = _injection_client(key_cfg, auth_enabled)
    try:
        with (
            patch(
                "ollama_queue_proxy.main.read_body",
                new=AsyncMock(return_value=(b'{"model":"x"}', None)),
            ),
            patch("ollama_queue_proxy.main.dispatch_request", new=AsyncMock()) as mock_dispatch,
        ):
            resp = client.post("/api/generate", json={"model": "x"})
        assert resp.status_code == 403, resp.text
        mock_dispatch.assert_not_awaited()
    finally:
        inj_mod.set_shared_state(None)


@pytest.mark.parametrize("auth_enabled", [True, False])
def test_injection_listener_still_serves_an_inference_key(auth_enabled):
    """CONTROL. Without it, an injection app that refused everything — or one whose
    shared state was never wired up — would satisfy the refusal test above."""
    key_cfg = ApiKeyConfig(key="inj-infer-key-444444444", client_id="memsearch", scope="inference")
    client, inj_mod = _injection_client(key_cfg, auth_enabled)
    try:
        with (
            patch(
                "ollama_queue_proxy.main.read_body",
                new=AsyncMock(return_value=(b'{"model":"x"}', None)),
            ),
            patch("ollama_queue_proxy.main.dispatch_request", new=AsyncMock()),
        ):
            resp = client.post("/api/generate", json={"model": "x"})
        assert resp.status_code == 200, resp.text
    finally:
        inj_mod.set_shared_state(None)


# ---------------------------------------------------------------------------
# The deprecated `management: bool`
# ---------------------------------------------------------------------------


def test_management_true_alone_still_grants_management():
    """The whole reason the key is deprecated rather than removed. This is the shape of
    every key on the reference deployment, which has eleven of them and no `scope:`."""
    legacy = ApiKeyConfig(key="legacy-mgmt-key-55555555", client_id="old-admin", management=True)
    assert legacy.scope == "management"
    client = build_route_client(keys=[legacy])
    assert client.post("/queue/pause", headers=_auth(legacy.key)).status_code == 200


def test_management_true_is_warned_about_at_startup(caplog):
    """Named per client_id: "some key uses a deprecated field" is not something an
    operator with eleven keys can act on."""
    cfg = make_config(
        auth_enabled=True,
        keys=[
            KEYS["inference"],
            ApiKeyConfig(key="legacy-mgmt-key-55555555", client_id="old-admin", management=True),
        ],
    )
    with caplog.at_level(logging.WARNING, logger="ollama_queue_proxy.auth"):
        AuthManager(cfg.auth)
    messages = [r.getMessage() for r in caplog.records]
    assert any("management" in m and "old-admin" in m for m in messages), messages


def test_management_false_is_not_warned_about(caplog):
    """CONTROL. `management: false` is the field's default, so warning on it would fire
    for every operator who never used the feature — which is how a deprecation notice
    trains people to filter it out (same reasoning as the health_check_interval notice
    in routing.py)."""
    cfg = make_config(auth_enabled=True, keys=[KEYS["inference"]])
    with caplog.at_level(logging.WARNING, logger="ollama_queue_proxy.auth"):
        AuthManager(cfg.auth)
    assert not any("deprecated" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("scope", ["read", "inference"])
def test_management_true_with_a_conflicting_scope_is_a_startup_error(scope):
    """A config that contradicts itself about a privilege must not boot and pick a
    winner. Whichever way a precedence rule went, half the operators who wrote this
    would silently get the opposite of what they meant."""
    with pytest.raises(ValueError, match="contradict"):
        ApiKeyConfig(
            key="conflict-key-6666666666", client_id="confused", management=True, scope=scope
        )


def test_management_true_with_scope_management_is_accepted():
    """CONTROL: the check must reject disagreement, not the mere presence of both keys."""
    k = ApiKeyConfig(
        key="agree-key-77777777777", client_id="admin2", management=True, scope="management"
    )
    assert k.scope == "management"


def test_management_false_does_not_conflict_with_a_higher_scope():
    """`management: false` is the default value, so writing it asserts nothing and is
    indistinguishable from omitting the field. Treating it as a contradiction would turn
    the obvious migration — leave the old `management: false` lines alone, add `scope:`
    to the one key that needs it — into a boot failure."""
    k = ApiKeyConfig(
        key="nonconflict-key-88888888", client_id="admin3", management=False, scope="management"
    )
    assert k.scope == "management"


# ---------------------------------------------------------------------------
# Backward compatibility: a config that has never heard of `scope`
# ---------------------------------------------------------------------------


def test_a_key_with_no_scope_defaults_to_inference():
    """The default has to be the OLD behaviour, not the safer one. Every pre-0.5.0 key
    could proxy inference, so defaulting to `read` would break every deployed config on
    upgrade — silently, and only for requests that actually matter."""
    assert ApiKeyConfig(key="plain-key-999999999999", client_id="c").scope == "inference"


@pytest.mark.asyncio
async def test_a_v040_style_config_behaves_exactly_as_before():
    """The whole v0.4.0 key matrix, replayed with no `scope:` anywhere: the management
    key manages and proxies, the plain key proxies but does not manage."""
    admin = ApiKeyConfig(key="compat-admin-key-0000000", client_id="admin", management=True)
    plain = ApiKeyConfig(key="compat-plain-key-1111111", client_id="user", management=False)
    client = build_route_client(keys=[admin, plain])

    assert client.post("/queue/pause", headers=_auth(admin.key)).status_code == 200
    assert client.post("/queue/pause", headers=_auth(plain.key)).status_code == 403
    assert client.get("/queue/status", headers=_auth(plain.key)).status_code == 200
    assert await _attempt_inference(admin) == 200
    assert await _attempt_inference(plain) == 200, (
        "a non-management key could always proxy inference before 0.5.0 and must still"
    )


# ---------------------------------------------------------------------------
# Refusal shape
# ---------------------------------------------------------------------------


def test_refusal_names_the_required_scope_and_not_the_held_one():
    """Reporting what a key HAS turns every refusal into an oracle for the credential
    being presented."""
    client = build_route_client()
    resp = client.post("/queue/pause", headers=_auth(KEYS["read"].key))
    assert resp.status_code == 403
    assert "management permission required" in resp.text
    assert "read" not in resp.json()["error"]
    assert "watcher" not in resp.text, "the refusal must not echo the caller's client_id"


def test_a_bad_key_is_401_and_a_valid_underprivileged_key_is_403():
    """The two must stay distinguishable: 401 means "I do not know who you are", 403
    means "I do, and it is not enough". Collapsing them tells a caller with a working
    credential to go and fix the credential."""
    client = build_route_client()
    assert client.post("/queue/pause", headers=_auth("not-a-key-at-all")).status_code == 401
    assert client.post("/queue/pause", headers=_auth(KEYS["read"].key)).status_code == 403


# ---------------------------------------------------------------------------
# The auth-off caveat now covers scope too
# ---------------------------------------------------------------------------


def test_scope_is_unenforced_on_the_main_port_when_auth_is_disabled():
    """Documented behaviour, asserted rather than described: with auth off no key is
    presented, so there is no scope to enforce — exactly as `management` already
    behaved. If this ever becomes a deliberate lockdown, this is the test that should
    fail and force the README caveat to be rewritten with it."""
    client = build_route_client(auth_enabled=False)
    assert client.post("/queue/pause").status_code == 200
    assert client.get("/queue/status").status_code == 200


@pytest.mark.asyncio
async def test_no_key_means_no_scope_check_at_the_proxy_chokepoint():
    """The auth-off half of the chokepoint rule "if there is a key, its scope is
    enforced"."""
    assert await _attempt_inference(None) == 200
