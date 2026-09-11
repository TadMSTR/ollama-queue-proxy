"""Tests for the OpenAI-compat embedding translation layer (openai_compat.py)."""

from ollama_queue_proxy.openai_compat import (
    is_openai_compat_path,
    rewrite_path,
    wrap_response,
)

# ── path detection ────────────────────────────────────────────────────────────


def test_v1_embeddings_is_compat_path():
    assert is_openai_compat_path("/v1/embeddings") is True


def test_v1_embeddings_without_leading_slash():
    assert is_openai_compat_path("v1/embeddings") is True


def test_api_embed_is_not_compat_path():
    assert is_openai_compat_path("/api/embed") is False


def test_api_generate_is_not_compat_path():
    assert is_openai_compat_path("/api/generate") is False


# ── path rewrite ──────────────────────────────────────────────────────────────


def test_rewrite_v1_embeddings_to_api_embed():
    assert rewrite_path("/v1/embeddings") == "/api/embed"


def test_rewrite_non_compat_path_unchanged():
    assert rewrite_path("/api/generate") == "/api/generate"


def test_rewrite_without_leading_slash():
    assert rewrite_path("v1/embeddings") == "/api/embed"


# ── response wrapping ─────────────────────────────────────────────────────────


def test_wrap_response_single_embedding():
    ollama = {
        "embeddings": [[0.1, 0.2, 0.3]],
        "model": "bge-m3",
    }
    result = wrap_response(ollama)

    assert result["object"] == "list"
    assert result["model"] == "bge-m3"
    assert len(result["data"]) == 1
    assert result["data"][0]["object"] == "embedding"
    assert result["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    assert result["data"][0]["index"] == 0
    assert result["usage"]["prompt_tokens"] == 0
    assert result["usage"]["total_tokens"] == 0


def test_wrap_response_batch_embeddings():
    vectors = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
    ollama = {"embeddings": vectors, "model": "bge-m3"}
    result = wrap_response(ollama)

    assert len(result["data"]) == 3
    for i, item in enumerate(result["data"]):
        assert item["index"] == i
        assert item["embedding"] == vectors[i]


def test_wrap_response_model_override():
    ollama = {"embeddings": [[0.1]], "model": "bge-m3"}
    result = wrap_response(ollama, model="nomic-embed-text")
    assert result["model"] == "nomic-embed-text"


def test_wrap_response_empty_embeddings():
    ollama = {"embeddings": [], "model": "bge-m3"}
    result = wrap_response(ollama)
    assert result["data"] == []
    assert result["object"] == "list"


def test_wrap_response_missing_embeddings_key():
    ollama = {"model": "bge-m3"}
    result = wrap_response(ollama)
    assert result["data"] == []


def test_wrap_response_missing_model_falls_back_to_empty_string():
    ollama = {"embeddings": [[0.1]]}
    result = wrap_response(ollama)
    assert result["model"] == ""


# ── test_env_override_list_index_skipped_gracefully ───────────────────────────


def test_env_override_list_index_skipped_gracefully(monkeypatch):
    """OQP_OLLAMA__HOSTS__0__URL must not crash and must not corrupt data."""
    monkeypatch.setenv("OQP_OLLAMA__HOSTS__0__URL", "http://tampered:11434")

    from ollama_queue_proxy.config import _apply_env_overrides

    data = {"ollama": {"hosts": [{"url": "http://original:11434", "name": "primary"}]}}
    result = _apply_env_overrides(data)

    # The list must be untouched — the env var is silently skipped
    assert result["ollama"]["hosts"][0]["url"] == "http://original:11434"
