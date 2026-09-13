"""GET /dashboard — the embedded read-only view.

Three properties carry the weight here and none of them is visible from a 200:

  1. Disabled means 404, not 401 — a disabled feature must not confirm it exists.
  2. The page never embeds a credential, and never renders API data as markup.
  3. It offers no control surface. It is reachable with `read`, so a pause button would
     be dead UI for most callers and an escalation for the rest.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ollama_queue_proxy.auth import AuthManager
from ollama_queue_proxy.config import ApiKeyConfig, DashboardConfig
from ollama_queue_proxy.middleware import RequestContextMiddleware
from ollama_queue_proxy.routes import dashboard as dashboard_routes
from tests.conftest import make_config

READ_KEY = "dash-read-key-00000000000"
INFER_KEY = "dash-infer-key-1111111111"
READ_KEY_CFG = ApiKeyConfig(key=READ_KEY, client_id="watcher", scope="read")
INFER_KEY_CFG = ApiKeyConfig(key=INFER_KEY, client_id="consumer", scope="inference")


def build_client(
    enabled: bool = True, auth_enabled: bool = True, refresh_seconds: int = 5
) -> TestClient:
    cfg = make_config(auth_enabled=auth_enabled, keys=[READ_KEY_CFG, INFER_KEY_CFG])
    cfg.auth.enabled = auth_enabled
    cfg.dashboard = DashboardConfig(enabled=enabled, refresh_seconds=refresh_seconds)

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(dashboard_routes.router)

    state = MagicMock()
    state.config = cfg
    state.auth_manager = AuthManager(cfg.auth)
    state.start_time = datetime.now(UTC)
    app.state.oqp = state
    return TestClient(app)


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _page(**kw) -> str:
    resp = build_client(**kw).get("/dashboard", headers=_auth(READ_KEY))
    assert resp.status_code == 200, resp.text
    return resp.text


# ---------------------------------------------------------------------------
# Enabled / disabled
# ---------------------------------------------------------------------------


def test_enabled_serves_html_to_a_read_key():
    resp = build_client().get("/dashboard", headers=_auth(READ_KEY))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.text.lstrip().startswith("<!DOCTYPE html>")


def test_disabled_is_404():
    assert build_client(enabled=False).get("/dashboard", headers=_auth(READ_KEY)).status_code == 404


def test_disabled_is_404_even_without_a_credential():
    """The 404 is checked BEFORE authentication on purpose. Answering 401 first would
    tell an unauthenticated prober that there is a dashboard here to be credentialed
    into — which is the one thing the disabled state should not disclose."""
    resp = build_client(enabled=False).get("/dashboard")
    assert resp.status_code == 404
    assert "dashboard" not in resp.text.lower()


def test_disabled_is_404_and_not_403_for_a_valid_but_wrong_key():
    resp = build_client(enabled=False).get("/dashboard", headers=_auth(INFER_KEY))
    assert resp.status_code == 404


def test_defaults_to_disabled():
    """It fails closed, and a brand-new surface should be opted into rather than
    appearing on every deployment that upgrades."""
    assert DashboardConfig().enabled is False


def test_there_is_no_configurable_path():
    """The route is registered at import time so a disabled dashboard can answer its own
    404; config is not loaded then, so the path cannot come from config. A fixed path
    also settles "must not be /" by construction rather than by a validator."""
    assert "path" not in DashboardConfig.model_fields


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_no_key_is_refused_when_auth_is_enabled():
    assert build_client().get("/dashboard").status_code == 401


def test_an_invalid_key_is_refused():
    assert build_client().get("/dashboard", headers=_auth("bogus-000")).status_code == 401


def test_no_key_is_accepted_when_auth_is_disabled():
    assert build_client(auth_enabled=False).get("/dashboard").status_code == 200


def test_an_inference_key_also_reaches_it():
    """`read` is the floor, so a higher scope is not refused."""
    assert build_client().get("/dashboard", headers=_auth(INFER_KEY)).status_code == 200


# ---------------------------------------------------------------------------
# No credential reaches the page
# ---------------------------------------------------------------------------


def test_the_page_contains_no_key_material():
    page = _page()
    for secret in (READ_KEY, INFER_KEY, "Bearer ", "Authorization"):
        assert secret not in page, f"{secret!r} must not appear in the dashboard HTML"


def test_the_page_polls_with_same_origin_credentials_rather_than_a_key():
    """A browser cannot set an Authorization header on a navigation, so the page must
    inherit whatever authenticated the document rather than carry a key of its own."""
    page = _page()
    assert 'credentials: "same-origin"' in page


def test_the_poll_urls_are_relative():
    """Relative, so the page keeps working when a reverse proxy mounts it under a
    subpath. Absolute paths would 404 there, and only in that deployment."""
    page = _page()
    assert 'fetch("queue/summary"' in page
    assert 'fetch("queue/status"' in page
    assert 'fetch("/queue/' not in page


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint", ["/queue/pause", "/queue/resume", "/queue/drain", "/queue/flush"]
)
def test_no_management_endpoint_is_referenced(endpoint):
    """Those need `management`; this page is reachable with `read`."""
    assert endpoint not in _page()


def test_the_page_issues_no_state_changing_requests():
    page = _page()
    assert "method:" not in page and '"POST"' not in page and "'POST'" not in page


def test_the_management_check_is_not_vacuous():
    """CONTROL for the four assertions above: the page DOES reference the two read
    endpoints, so their absence is a property of the page rather than of the check."""
    page = _page()
    assert "queue/summary" in page and "queue/status" in page


# ---------------------------------------------------------------------------
# Self-containment and escaping
# ---------------------------------------------------------------------------


def test_no_external_resource_is_fetched():
    """A CDN reference makes an air-gapped deployment render blank, and adds a third
    party to a page that reports infrastructure state."""
    page = _page()
    assert "//cdn" not in page
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', page)
    assert "<script" in page and "src=" not in page.split("<script")[1].split(">")[0]


def test_api_data_is_written_with_textcontent_and_never_as_markup():
    """client_id, host name and model names are operator-supplied config strings that
    reach this page verbatim from the API. textContent is the only thing between a
    config file and script execution here.

    Asserted against the ASSIGNMENT, not against the word appearing anywhere: a bare
    substring check also matches the source comment explaining why the assignment is
    absent, so it would go red on prose and — worse — could be made green again by
    deleting the comment rather than by fixing anything.
    """
    page = _page()
    assert "textContent" in page
    assert not re.search(r"\.(inner|outer)HTML\s*=", page), "API data must not be set as markup"
    assert "insertAdjacentHTML" not in page
    assert "document.write" not in page


def test_the_markup_assertion_can_fail():
    """CONTROL for the regex above. A pattern that matches nothing passes against every
    page, including one that assigns innerHTML on every row."""
    assert re.search(r"\.(inner|outer)HTML\s*=", "el.innerHTML = data.name;")


def test_no_templating_dependency_was_introduced():
    """The repo had zero templating dependencies before this build and the dashboard
    must not be the reason it acquires one. A dashboard that drags a template engine —
    or a Node toolchain — into a Python proxy is a worse trade than no dashboard.

    Checked against the declared dependencies and the module's imports rather than
    against the word appearing in the file, so the module docstring can name jinja2 in
    order to explain its absence.
    """
    import pathlib as _pathlib

    import ollama_queue_proxy.routes.dashboard as mod

    source = _pathlib.Path(mod.__file__).read_text()
    assert not re.search(r"^\s*(from|import)\s+jinja", source, re.M)
    assert not re.search(r"^\s*from\s+\S*staticfiles\s+import", source, re.M | re.I)
    assert not re.search(r"\bStaticFiles\s*\(", source)

    pyproject = _pathlib.Path(mod.__file__).parents[3] / "pyproject.toml"
    declared = pyproject.read_text()
    deps = declared.split("dependencies", 1)[1].split("]", 1)[0].lower()
    assert "jinja" not in deps, "the dashboard must not add a templating dependency"


# ---------------------------------------------------------------------------
# refresh_seconds
# ---------------------------------------------------------------------------


def test_refresh_interval_is_interpolated_in_milliseconds():
    assert "var REFRESH_MS = 5000;" in _page(refresh_seconds=5)
    assert "var REFRESH_MS = 30000;" in _page(refresh_seconds=30)


def test_the_placeholder_is_always_substituted():
    """A surviving placeholder is a syntax error in the page's only script block, which
    renders a dashboard that loads and then never updates."""
    assert "__REFRESH_MS__" not in _page()


def test_polling_stops_while_the_tab_is_hidden():
    """A dashboard left open on a second monitor otherwise polls forever against a proxy
    whose whole purpose is rationing a scarce resource."""
    page = _page()
    assert "visibilitychange" in page
    assert "document.hidden" in page


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_refresh_interval_is_rejected(bad):
    with pytest.raises(ValueError, match="refresh_seconds"):
        DashboardConfig(refresh_seconds=bad)


# ---------------------------------------------------------------------------
# Content-Security-Policy (baseline OE-03)
# ---------------------------------------------------------------------------
#
# A second, INDEPENDENT barrier behind textContent. If the two agreed — if the CSP were
# `unsafe-inline` — it would restate the first barrier rather than back it up, and a
# single escaping mistake would be the whole defence.


def _csp() -> str:
    resp = build_client().get("/dashboard", headers=_auth(READ_KEY))
    return resp.headers["content-security-policy"]


def test_a_csp_is_sent():
    assert _csp()


def test_the_csp_does_not_allow_unsafe_inline():
    """The easy way to permit this page's own inline blocks also permits anything an
    attacker injects, which would make the CSP decorative."""
    csp = _csp()
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp


def test_the_csp_defaults_to_none():
    """Anything not explicitly granted is refused rather than inherited."""
    assert "default-src 'none'" in _csp()


def test_the_inline_blocks_carry_the_nonce_from_the_header():
    """The nonce is worthless if the header and the document disagree — the page would
    render blank, and only in a browser, which no test here would otherwise notice."""
    resp = build_client().get("/dashboard", headers=_auth(READ_KEY))
    csp, page = resp.headers["content-security-policy"], resp.text
    nonces = set(re.findall(r"'nonce-([A-Za-z0-9_-]+)'", csp))
    assert len(nonces) == 1, f"header should carry exactly one nonce, got {nonces}"
    nonce = nonces.pop()
    assert page.count(f'nonce="{nonce}"') == 2, "both <style> and <script> must carry it"
    assert "__NONCE__" not in page, "an unsubstituted placeholder renders the page blank"


def test_the_nonce_is_fresh_on_every_response():
    """A fixed nonce is a permanent allowlist entry for anyone who reads the source."""
    seen = {re.search(r"'nonce-([A-Za-z0-9_-]+)'", _csp()).group(1) for _ in range(5)}
    assert len(seen) == 5, f"nonce must not repeat across responses: {seen}"


def test_the_page_can_still_reach_its_own_endpoints():
    """connect-src must permit the same-origin polls, or the CSP silently breaks the
    dashboard it is protecting."""
    assert "connect-src 'self'" in _csp()


@pytest.mark.parametrize(
    "header,value",
    [
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        ("x-frame-options", "DENY"),
    ],
)
def test_hardening_headers_are_present(header, value):
    resp = build_client().get("/dashboard", headers=_auth(READ_KEY))
    assert resp.headers[header] == value


def test_framing_is_refused_two_ways():
    """`frame-ancestors` is the modern control; X-Frame-Options covers browsers that
    ignore it. This page reports infrastructure state and has no reason to be framed."""
    assert "frame-ancestors 'none'" in _csp()
