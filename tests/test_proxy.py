"""Tests for proxy logic: model extraction, body buffering, model management protection."""

from __future__ import annotations

import json

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
