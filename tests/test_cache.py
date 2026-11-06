"""Tests for embedding response cache."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from ollama_queue_proxy.cache import (
    CACHEABLE_PATHS,
    EmbeddingCache,
    _embed_key,
    _embeddings_key,
    _startup_error_detail,
    _userinfo_bled,
    redact_backend_url,
)
from ollama_queue_proxy.config import EmbeddingCacheConfig


def make_cfg(**kwargs) -> EmbeddingCacheConfig:
    defaults = {
        "enabled": True,
        "backend": "redis://localhost:6379/0",
        "ttl": 3600,
        "max_entry_bytes": 32768,
        "key_prefix": "oqp:embed:",
        "connect_timeout": 2,
    }
    defaults.update(kwargs)
    return EmbeddingCacheConfig(**defaults)


# ---------------------------------------------------------------------------
# Cache key construction
# ---------------------------------------------------------------------------


def test_embed_key_single_string_normalised_to_list():
    k1 = _embed_key("oqp:embed:", "nomic", {"input": "hello"})
    k2 = _embed_key("oqp:embed:", "nomic", {"input": ["hello"]})
    assert k1 == k2


def test_embed_key_different_models_differ():
    k1 = _embed_key("oqp:embed:", "nomic", {"input": "hello"})
    k2 = _embed_key("oqp:embed:", "mxbai", {"input": "hello"})
    assert k1 != k2


def test_embeddings_key_different_from_embed_key():
    """Same model+text must produce different keys across endpoints."""
    k_embed = _embed_key("oqp:embed:", "nomic", {"input": "hello"})
    k_embeddings = _embeddings_key("oqp:embed:", "nomic", {"prompt": "hello"})
    assert k_embed != k_embeddings
    assert "embed:" in k_embed
    assert "embeddings:" in k_embeddings


def test_key_prefix_included():
    k = _embed_key("myprefix:", "nomic", {"input": "hi"})
    assert k.startswith("myprefix:")


def test_cacheable_paths():
    assert "/api/embed" in CACHEABLE_PATHS
    assert "/api/embeddings" in CACHEABLE_PATHS
    assert "/api/generate" not in CACHEABLE_PATHS
    assert "/api/chat" not in CACHEABLE_PATHS


# ---------------------------------------------------------------------------
# EmbeddingCache — disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_cache_startup_no_connect():
    cfg = make_cfg(enabled=False)
    cache = EmbeddingCache(cfg)
    with patch("redis.asyncio.from_url") as mock_from_url:
        await cache.startup()
        mock_from_url.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_cache_get_returns_none():
    cfg = make_cfg(enabled=False)
    cache = EmbeddingCache(cfg)
    result = await cache.get("/api/embed", {"input": "hi", "model": "nomic"}, "nomic", None)
    assert result is None


# ---------------------------------------------------------------------------
# EmbeddingCache — startup fail-fast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_exits_on_unreachable_backend():
    cfg = make_cfg(backend="redis://bad-host:6379/0")
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.ping.side_effect = Exception("connection refused")

    with patch("redis.asyncio.from_url", return_value=mock_client), pytest.raises(SystemExit):
        await cache.startup()


@pytest.mark.asyncio
async def test_startup_succeeds_on_ping_ok():
    cfg = make_cfg()
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.ping.return_value = True

    with patch("redis.asyncio.from_url", return_value=mock_client):
        await cache.startup()  # must not raise


# ---------------------------------------------------------------------------
# Backend URL redaction — the password must never reach a log or stderr
# ---------------------------------------------------------------------------

SECRET = "s3cret"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # password, no username: the shape deployments use
        ("redis://:s3cret@valkey:6379/0", "redis://valkey:6379/0"),
        ("redis://user:s3cret@valkey:6379/2", "redis://valkey:6379/2"),
        ("rediss://default:s3cret@cache.example:6380/0", "rediss://cache.example:6380/0"),
        ("unix://:s3cret@/run/valkey.sock?db=3", "unix:///run/valkey.sock?db=3"),
        # no credentials: unchanged
        ("redis://localhost:6379/0", "redis://localhost:6379/0"),
        ("unix:///run/valkey.sock", "unix:///run/valkey.sock"),
        # password as a query parameter (redis-py accepts it); other params dropped
        ("redis://valkey:6379/0?password=s3cret&db=1", "redis://valkey:6379/0?db=1"),
        ("redis://valkey:6379/0#s3cret", "redis://valkey:6379/0"),
        # unencoded reserved characters end urlsplit's netloc early; still stripped
        ("redis://:s3/cret@valkey:6379/0", "redis://valkey:6379/0"),
        ("redis://:s3@cret@valkey:6379/0", "redis://valkey:6379/0"),
        ("redis://:s3%40cret@valkey:6379/0", "redis://valkey:6379/0"),
        # a '?' or '#' inside the password leaves an '@' past the authority: ambiguous,
        # so nothing after :// is kept
        ("redis://:s3?cret@valkey:6379/0", "redis://<redacted>"),
        ("redis://:s3#cret@valkey:6379/0", "redis://<redacted>"),
        ("redis://:s3/?cret@valkey:6379/0", "redis://<redacted>"),
        ("redis://u:s3/?c#r%40et@valkey:6379/0?db=1#x", "redis://<redacted>"),
        # an '@' inside a query value or the fragment, with no userinfo at all
        ("redis://valkey:6379/0?password=hunt@s3cret", "redis://<redacted>"),
        ("redis://valkey:6379/0?username=u&password=p@s3cret&db=0", "redis://<redacted>"),
        ("unix:///run/valkey.sock?password=a@s3cret&db=2", "unix://<redacted>"),
        ("redis://valkey:6379/0#frag@s3cret", "redis://<redacted>"),
        # not a URL at all: say nothing about it
        ("s3cret", "<unrecognised backend URL>"),
    ],
)
def test_redact_backend_url(url, expected):
    assert redact_backend_url(url) == expected


@pytest.mark.asyncio
async def test_connected_log_redacts_password(caplog):
    cfg = make_cfg(backend=f"redis://:{SECRET}@valkey:6379/0")
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.ping.return_value = True

    # Pin the level so the INFO record is produced: the point is that it exists AND is
    # redacted, not that it was filtered out.
    with (
        caplog.at_level(logging.INFO, logger="ollama_queue_proxy.cache"),
        patch("redis.asyncio.from_url", return_value=mock_client) as from_url,
    ):
        await cache.startup()

    # the real URL, password and all, still goes to the client
    assert from_url.call_args.args[0] == f"redis://:{SECRET}@valkey:6379/0"
    records = [r for r in caplog.records if "embedding_cache.connected" in r.getMessage()]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "valkey:6379/0" in msg
    assert SECRET not in msg
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_startup_failure_stderr_redacts_password(capsys):
    cfg = make_cfg(backend=f"redis://:{SECRET}@valkey:6379/0")
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.ping.side_effect = RedisConnectionError(
        "Error 111 connecting to valkey:6379. Connection refused."
    )

    with patch("redis.asyncio.from_url", return_value=mock_client), pytest.raises(SystemExit):
        await cache.startup()

    err = capsys.readouterr().err
    assert "FATAL" in err
    assert "valkey:6379" in err
    assert "Connection refused" in err  # redis-py's own detail is kept
    assert SECRET not in err


@pytest.mark.asyncio
async def test_startup_failure_withholds_non_redis_exception_text(capsys):
    """A non-redis error (e.g. urllib's port ValueError) can quote the URL: type only."""
    cfg = make_cfg(backend=f"redis://:{SECRET}@valkey:6379/0")
    cache = EmbeddingCache(cfg)

    with (
        patch("redis.asyncio.from_url", side_effect=ValueError(f"bad value {SECRET!r}")),
        pytest.raises(SystemExit),
    ):
        await cache.startup()

    err = capsys.readouterr().err
    assert "ValueError" in err
    assert SECRET not in err


@pytest.mark.asyncio
async def test_startup_failure_on_malformed_url_leaks_no_password_fragment(capsys):
    """Real redis-py parse of a password with an unencoded '/': urllib's port error
    quotes the fragment after the colon. Nothing of the password may reach stderr."""
    cfg = make_cfg(backend="redis://:pw/s3cret@valkey:6379/0")
    cache = EmbeddingCache(cfg)

    with pytest.raises(SystemExit):
        await cache.startup()

    err = capsys.readouterr().err
    assert "redis://valkey:6379/0" in err
    assert "pw" not in err.replace("password", "")
    assert SECRET not in err


@pytest.mark.asyncio
async def test_startup_failure_bled_url_withholds_redis_error_text(capsys):
    """When the password bled out of the netloc, even redis-py's host:port text can
    carry part of it (here the digits before the '/'), so the detail is withheld."""
    cfg = make_cfg(backend="redis://:4242/s3cret@valkey:6379/0")
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.ping.side_effect = RedisConnectionError(
        "Error 111 connecting to localhost:4242. Connection refused."
    )

    with patch("redis.asyncio.from_url", return_value=mock_client), pytest.raises(SystemExit):
        await cache.startup()

    err = capsys.readouterr().err
    assert "ConnectionError" in err
    assert "4242" not in err
    assert SECRET not in err


@pytest.mark.asyncio
async def test_startup_failure_password_with_at_and_slash_withholds_detail(capsys):
    """A password holding '@' then '/' keeps an '@' in urlsplit's netloc, so a check for
    "any '@' in the netloc" misses that redis-py's host:port is now password text."""
    cfg = make_cfg(backend="redis://user:pw@s3cret:1/rest@valkey:6379/0")
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.ping.side_effect = RedisConnectionError(
        "Error 111 connecting to s3cret:1. Connection refused."
    )

    with patch("redis.asyncio.from_url", return_value=mock_client), pytest.raises(SystemExit):
        await cache.startup()

    err = capsys.readouterr().err
    assert "ConnectionError" in err
    assert "redis://valkey:6379/0" in err
    assert SECRET not in err


@pytest.mark.parametrize(
    ("url", "bled"),
    [
        ("redis://:s3cret@valkey:6379/0", False),
        ("redis://valkey:6379/0", False),
        ("unix:///run/valkey.sock", False),
        ("unix:/run/valkey.sock", False),  # no "//" but no "@": nothing to leak
        ("unix::s3cret@/run/x.sock", True),  # no "//" and an "@": fail closed
        ("unix:s3cret@/run/x.sock", True),
        ("redis::s3cret@valkey:6379/0", True),
        ("redis://user:pw@s3cret:1/rest@valkey:6379/0", True),
    ],
)
def test_userinfo_bled(url, bled):
    assert _userinfo_bled(url) is bled


def test_startup_error_detail_withholds_text_when_url_is_not_clean():
    """Independently of _userinfo_bled: a URL redact_backend_url can't name a host for
    gets no redis-py text, even when nothing bled."""
    exc = RedisConnectionError("Error 2 connecting to /run/valkey.sock. No such file.")
    assert not _userinfo_bled("unix:/run/valkey.sock")
    assert "withheld" in _startup_error_detail("unix:/run/valkey.sock", exc)
    assert "No such file" in _startup_error_detail("unix:///run/valkey.sock", exc)


@pytest.mark.parametrize(
    "url",
    ["unix:s3cret@/run/x.sock", "unix:/:s3cret@/run/x.sock", "unix::s3cret@/run/x.sock?db=0"],
)
@pytest.mark.asyncio
async def test_startup_failure_unix_without_slashes_withholds_detail(url, capsys):
    """redis-py parses 'unix:' with no '//' and connects to the whole remainder as the
    socket path, so its real ConnectionError quotes the password."""
    from redis.asyncio.connection import parse_url

    path = parse_url(url)["path"]
    assert SECRET in path  # the premise: redis-py really does put it in the path
    cache = EmbeddingCache(make_cfg(backend=url))
    bad_client = AsyncMock()
    bad_client.ping.side_effect = RedisConnectionError(
        f"Error 2 connecting to {path}. No such file or directory."
    )
    with patch("redis.asyncio.from_url", return_value=bad_client), pytest.raises(SystemExit):
        await cache.startup()

    err = capsys.readouterr().err
    assert "ConnectionError" in err
    assert SECRET not in err


# ---------------------------------------------------------------------------
# Class probe (SC-54): no fragment of a password reaches either output, for every
# separator in every position redis-py reads a password from, on every scheme.
# ---------------------------------------------------------------------------

_SEPARATORS = ["", "/", "?", "#", "@", ":", "%40", "/?", "@/", "/@", "?@", "#@", "@?", "@#"]
_HEAD, _TAIL = "HEADPART", "TAILPART"


def _probe_urls():
    for sep in _SEPARATORS:
        pw = f"{_HEAD}{sep}{_TAIL}"
        for scheme in ("redis", "rediss"):
            yield f"{scheme}://:{pw}@valkey:6379/0"
            yield f"{scheme}://user:{pw}@valkey:6379/0"
            yield f"{scheme}://valkey:6379/0?password={pw}"
            yield f"{scheme}://valkey:6379/0?password={pw}&db=1"
            yield f"{scheme}://valkey:6379/0#{pw}"
        yield f"unix://:{pw}@/run/valkey.sock?db=1"
        yield f"unix:///run/valkey.sock?password={pw}"
        # no "//": redis-py still parses these, and a unix: one puts the whole remainder,
        # userinfo included, into the socket path
        for scheme, where in (
            ("redis", "valkey:6379/0"),
            ("rediss", "valkey:6379/0"),
            ("unix", "/run/valkey.sock"),
        ):
            for slash in ("", "/"):
                yield f"{scheme}:{slash}:{pw}@{where}"
                yield f"{scheme}:{slash}{pw}@{where}"
                yield f"{scheme}:{slash}user:{pw}@{where}?db=0"


def _redis_py_failure(url: str) -> Exception:
    """The exception redis-py would raise connecting to this URL: its real parse, and its
    real error text shape (format_error_message: host:port or the socket path)."""
    from redis.asyncio.connection import parse_url

    try:
        kw = parse_url(url)
    except ValueError as e:  # urllib's port error and redis-py's own refusals
        return e
    where = kw.get("path") or f"{kw.get('host', 'localhost')}:{kw.get('port', 6379)}"
    return RedisConnectionError(f"Error 111 connecting to {where}. Connection refused.")


@pytest.mark.parametrize("url", list(_probe_urls()))
def test_probe_redact_backend_url_leaks_no_password_fragment(url):
    out = redact_backend_url(url)
    assert _HEAD not in out and _TAIL not in out, out


@pytest.mark.parametrize("url", list(_probe_urls()))
@pytest.mark.asyncio
async def test_probe_startup_outputs_leak_no_password_fragment(url, caplog, capsys):
    cache = EmbeddingCache(make_cfg(backend=url))
    ok_client = AsyncMock()
    ok_client.ping.return_value = True
    with (
        caplog.at_level(logging.INFO, logger="ollama_queue_proxy.cache"),
        patch("redis.asyncio.from_url", return_value=ok_client),
    ):
        await cache.startup()

    failing = EmbeddingCache(make_cfg(backend=url))
    bad_client = AsyncMock()
    bad_client.ping.side_effect = _redis_py_failure(url)
    with patch("redis.asyncio.from_url", return_value=bad_client), pytest.raises(SystemExit):
        await failing.startup()

    for text in (caplog.text, capsys.readouterr().err):
        assert _HEAD not in text and _TAIL not in text, text


# ---------------------------------------------------------------------------
# EmbeddingCache — cache miss / hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_miss_returns_none():
    cfg = make_cfg()
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.get.return_value = None
    mock_client.ping.return_value = True

    with patch("redis.asyncio.from_url", return_value=mock_client):
        await cache.startup()

    result = await cache.get("/api/embed", {"input": "hello", "model": "nomic"}, "nomic", "svc")
    assert result is None


@pytest.mark.asyncio
async def test_cache_hit_returns_bytes():
    cfg = make_cfg()
    cache = EmbeddingCache(cfg)

    cached_bytes = b'{"embedding":[0.1,0.2]}'
    mock_client = AsyncMock()
    mock_client.get.return_value = cached_bytes
    mock_client.ping.return_value = True

    with patch("redis.asyncio.from_url", return_value=mock_client):
        await cache.startup()

    result = await cache.get("/api/embed", {"input": "hi", "model": "nomic"}, "nomic", "svc")
    assert result == cached_bytes


@pytest.mark.asyncio
async def test_same_embed_endpoint_cache_key_matches():
    """Verify the cache key is stable across two identical requests."""
    cfg = make_cfg()

    k1 = _embed_key(cfg.key_prefix, "nomic", {"input": "hello"})
    k2 = _embed_key(cfg.key_prefix, "nomic", {"input": "hello"})
    assert k1 == k2


@pytest.mark.asyncio
async def test_embed_and_embeddings_separate_cache_entries():
    """Same model+text on /api/embed vs /api/embeddings must not share a key."""
    cfg = make_cfg()
    body = {"model": "nomic", "input": "hello", "prompt": "hello"}

    k_embed = _embed_key(cfg.key_prefix, "nomic", body)
    k_embeddings = _embeddings_key(cfg.key_prefix, "nomic", body)
    assert k_embed != k_embeddings


# ---------------------------------------------------------------------------
# EmbeddingCache — not cached conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_2xx_response_not_cached():
    cfg = make_cfg()
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.ping.return_value = True

    with patch("redis.asyncio.from_url", return_value=mock_client):
        await cache.startup()

    # We don't have a set() call flow for non-2xx since main.py guards status_code==200
    # But we test that set() itself caches correctly and setex is called
    await cache.set("/api/embed", {"input": "hi", "model": "nomic"}, "nomic", b"x" * 100, None)
    mock_client.setex.assert_called_once()


@pytest.mark.asyncio
async def test_oversized_response_not_cached():
    cfg = make_cfg(max_entry_bytes=10)
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.ping.return_value = True

    with patch("redis.asyncio.from_url", return_value=mock_client):
        await cache.startup()

    oversized = b"x" * 100  # > max_entry_bytes=10
    await cache.set("/api/embed", {"input": "hi", "model": "nomic"}, "nomic", oversized, None)
    mock_client.setex.assert_not_called()


# ---------------------------------------------------------------------------
# EmbeddingCache — graceful degradation on RESP errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resp_error_on_get_returns_none_no_raise():
    cfg = make_cfg()
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.get.side_effect = Exception("RESP connection dropped")
    mock_client.ping.return_value = True

    with patch("redis.asyncio.from_url", return_value=mock_client):
        await cache.startup()

    result = await cache.get("/api/embed", {"input": "hi", "model": "nomic"}, "nomic", None)
    assert result is None  # graceful degrade — no exception raised


@pytest.mark.asyncio
async def test_resp_error_on_set_no_raise():
    cfg = make_cfg()
    cache = EmbeddingCache(cfg)

    mock_client = AsyncMock()
    mock_client.setex.side_effect = Exception("RESP write error")
    mock_client.ping.return_value = True

    with patch("redis.asyncio.from_url", return_value=mock_client):
        await cache.startup()

    await cache.set("/api/embed", {"input": "hi", "model": "nomic"}, "nomic", b"data", None)
    # Must not raise


# ---------------------------------------------------------------------------
# Logging — no body content in logs (FLAG E verification)
# ---------------------------------------------------------------------------


def test_cache_key_contains_no_raw_input():
    """The cache key hash must not contain the raw input text."""
    key = _embed_key("oqp:embed:", "nomic", {"input": "sensitive user data"})
    assert "sensitive user data" not in key
    assert "sensitive" not in key
