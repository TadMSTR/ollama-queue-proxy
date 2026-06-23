"""Tests for proxy logic: model extraction, body buffering, model management protection."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ollama_queue_proxy.proxy import extract_model, _MODEL_MANAGEMENT_PATHS


def test_extract_model_present():
    body = json.dumps({"model": "nomic-embed-text", "prompt": "hello"}).encode()
    assert extract_model(body) == "nomic-embed-text"


def test_extract_model_absent():
    body = json.dumps({"prompt": "hello"}).encode()
    assert extract_model(body) is None


def test_extract_model_empty_body():
    assert extract_model(b"") is None


def test_extract_model_invalid_json():
    assert extract_model(b"{not valid json}") is None


def test_model_management_paths_defined():
    assert "/api/pull" in _MODEL_MANAGEMENT_PATHS
    assert "/api/push" in _MODEL_MANAGEMENT_PATHS
    assert "/api/delete" in _MODEL_MANAGEMENT_PATHS
    assert "/api/create" in _MODEL_MANAGEMENT_PATHS
    assert "/api/copy" in _MODEL_MANAGEMENT_PATHS


def test_generate_not_in_management_paths():
    assert "/api/generate" not in _MODEL_MANAGEMENT_PATHS


def test_chat_not_in_management_paths():
    assert "/api/chat" not in _MODEL_MANAGEMENT_PATHS


# ---------------------------------------------------------------------------
# OQP-1: Content-Length off-by-one on non-streaming chat completions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_streaming_response_content_length_correct():
    """
    dispatch_request must NOT forward the upstream content-length to JSONResponse.
    Ollama appends a trailing newline to non-streaming JSON bodies, so the upstream
    content-length is 1 byte longer than the re-serialised response body.
    The returned response must carry the correct (re-serialised) content-length.
    """
    from fastapi import Request
    from ollama_queue_proxy.proxy import dispatch_request
    from ollama_queue_proxy.hosts import HostManager, OllamaHost
    from tests.conftest import make_config

    payload = {"message": {"role": "assistant", "content": "hi"}, "done": True}
    # Ollama appends \n — body is 1 byte longer than the JSON-only serialisation
    upstream_body = json.dumps(payload).encode() + b"\n"
    upstream_content_length = str(len(upstream_body))  # e.g. "52"

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.headers = httpx.Headers({
        "content-type": "application/json",
        "content-length": upstream_content_length,
    })
    mock_resp.json.return_value = payload

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_resp)

    cfg = make_config()
    host = OllamaHost(url="http://ollama-test:11434", name="test")
    host.healthy = True
    hm = HostManager.__new__(HostManager)
    hm.hosts = [host]

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chat",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    request = Request(scope)
    request.state.request_id = "test-req"

    response = await dispatch_request(
        request=request,
        body=json.dumps({"model": "llama3", "messages": []}).encode(),
        client_id=None,
        config=cfg,
        host_manager=hm,
        client=mock_client,
    )

    # JSONResponse uses compact separators — match that serialisation to get the correct length
    expected_body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                               indent=None, separators=(",", ":")).encode("utf-8")
    assert response.headers["content-length"] == str(len(expected_body)), (
        f"content-length should be {len(expected_body)} (re-serialised body), "
        f"not {upstream_content_length} (upstream body with trailing newline)"
    )
    assert response.headers["content-length"] != upstream_content_length, (
        "upstream content-length (with trailing newline) must not bleed into response"
    )


# ---------------------------------------------------------------------------
# OQP-4: /api/embed returns application/json with chunked TE — must be JSONResponse
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chunked_json_response_not_treated_as_streaming():
    """
    dispatch_request must return JSONResponse (not StreamingResponse) when the
    upstream sends application/json with transfer-encoding: chunked.
    Ollama's /api/embed does this. Treating it as streaming caused proxy_handler's
    isinstance(response, JSONResponse) check to fail, preventing the OpenAI compat
    wrap_response call — the root cause of OQP-4.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from ollama_queue_proxy.proxy import dispatch_request
    from ollama_queue_proxy.hosts import HostManager, OllamaHost
    from tests.conftest import make_config

    payload = {"embeddings": [[0.1, 0.2, 0.3]], "model": "bge-m3"}

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.headers = httpx.Headers({
        "content-type": "application/json; charset=utf-8",
        "transfer-encoding": "chunked",
    })
    mock_resp.json.return_value = payload

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request = AsyncMock(return_value=mock_resp)

    cfg = make_config()
    host = OllamaHost(url="http://ollama-test:11434", name="test")
    host.healthy = True
    hm = HostManager.__new__(HostManager)
    hm.hosts = [host]

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/embed",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    request = Request(scope)
    request.state.request_id = "test-oqp4"

    response = await dispatch_request(
        request=request,
        body=json.dumps({"model": "bge-m3", "input": "test"}).encode(),
        client_id=None,
        config=cfg,
        host_manager=hm,
        client=mock_client,
    )

    assert isinstance(response, JSONResponse), (
        "application/json response with transfer-encoding: chunked must return "
        "JSONResponse so that proxy_handler can apply the OpenAI compat wrap"
    )
    assert not isinstance(response, StreamingResponse)
    assert json.loads(response.body) == payload
