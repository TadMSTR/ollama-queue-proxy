"""Tests for config loading and validation."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from ollama_queue_proxy.config import ApiKeyConfig, load_config


def write_config(tmp_path, data: dict) -> str:
    path = str(tmp_path / "config.yml")
    with open(path, "w") as f:
        yaml.safe_dump(data, f)
    return path


def base_config() -> dict:
    return {"ollama": {"hosts": [{"url": "http://ollama:11434", "name": "primary"}]}}


def test_load_minimal_config(tmp_path):
    path = write_config(tmp_path, base_config())
    cfg = load_config(path)
    assert cfg.proxy.port == 11435
    assert cfg.auth.enabled is False
    assert len(cfg.ollama.hosts) == 1


def test_load_with_auth_keys(tmp_path):
    data = base_config()
    data["auth"] = {
        "enabled": True,
        "keys": [{"key": "secret123", "client_id": "svc", "max_priority": "normal"}],
    }
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.auth.enabled is True
    assert cfg.auth.keys[0].client_id == "svc"


def test_auth_enabled_no_keys_exits(tmp_path):
    data = base_config()
    data["auth"] = {"enabled": True, "keys": []}
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


def test_missing_config_file_exits():
    with pytest.raises(SystemExit):
        load_config("/nonexistent/path/config.yml")


def test_env_override_port(tmp_path, monkeypatch):
    path = write_config(tmp_path, base_config())
    monkeypatch.setenv("OQP_PROXY__PORT", "9999")
    cfg = load_config(path)
    assert cfg.proxy.port == 9999


def test_env_override_bool(tmp_path, monkeypatch):
    path = write_config(tmp_path, base_config())
    monkeypatch.setenv("OQP_PROXY__ALLOW_MODEL_MANAGEMENT", "true")
    cfg = load_config(path)
    assert cfg.proxy.allow_model_management is True


# ---------------------------------------------------------------------------
# v0.2.0 back-compat: v0.1.x configs get sane defaults
# ---------------------------------------------------------------------------


def test_v1_config_gets_v2_defaults(tmp_path):
    """A v0.1.x config (no new fields) must load cleanly with v0.2.0 defaults."""
    path = write_config(tmp_path, base_config())
    cfg = load_config(path)
    assert cfg.ollama.hosts[0].weight == 1
    assert cfg.ollama.hosts[0].model_sync_interval == 30
    assert cfg.routing.strategy == "round_robin"
    assert cfg.client_injection.listeners == []
    assert cfg.embedding_cache.enabled is False
    assert cfg.keep_alive.default == "5m"


# ---------------------------------------------------------------------------
# HostConfig extensions
# ---------------------------------------------------------------------------


def test_host_weight_and_sync_interval(tmp_path):
    data = base_config()
    data["ollama"]["hosts"][0]["weight"] = 3
    data["ollama"]["hosts"][0]["model_sync_interval"] = 60
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.ollama.hosts[0].weight == 3
    assert cfg.ollama.hosts[0].model_sync_interval == 60


def test_host_weight_zero_rejected(tmp_path):
    data = base_config()
    data["ollama"]["hosts"][0]["weight"] = 0
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


def test_host_weight_negative_rejected(tmp_path):
    data = base_config()
    data["ollama"]["hosts"][0]["weight"] = -1
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


# ---------------------------------------------------------------------------
# Client injection config
# ---------------------------------------------------------------------------


def _config_with_auth_and_injection(port: int = 11436, inject_as: str = "svc") -> dict:
    data = base_config()
    data["auth"] = {
        "enabled": True,
        "keys": [{"key": "secret", "client_id": inject_as, "max_priority": "low"}],
    }
    data["client_injection"] = {"listeners": [{"listen_port": port, "inject_as": inject_as}]}
    return data


def test_injection_listener_happy_path(tmp_path):
    data = _config_with_auth_and_injection()
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert len(cfg.client_injection.listeners) == 1
    assert cfg.client_injection.listeners[0].listen_port == 11436
    assert cfg.client_injection.listeners[0].bind == "127.0.0.1"


def test_injection_unknown_inject_as_exits(tmp_path):
    data = _config_with_auth_and_injection(inject_as="known")
    data["client_injection"]["listeners"][0]["inject_as"] = "unknown-id"
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


def test_injection_port_collision_with_proxy_port_exits(tmp_path):
    data = _config_with_auth_and_injection(port=11435)  # same as proxy.port default
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


def test_injection_duplicate_ports_exits(tmp_path):
    data = base_config()
    data["auth"] = {
        "enabled": True,
        "keys": [
            {"key": "k1", "client_id": "a"},
            {"key": "k2", "client_id": "b"},
        ],
    }
    data["client_injection"] = {
        "listeners": [
            {"listen_port": 11436, "inject_as": "a"},
            {"listen_port": 11436, "inject_as": "b"},
        ]
    }
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


def test_injection_port_below_1024_rejected(tmp_path):
    data = _config_with_auth_and_injection(port=80)
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


def test_injection_allow_public_no_auth_emits_warning(tmp_path, capsys):
    data = base_config()
    data["client_injection"] = {"allow_public_injection": True}
    path = write_config(tmp_path, data)
    load_config(path)  # should NOT exit — warning only
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "allow_public_injection" in captured.err


def test_injection_non_loopback_bind_without_allow_public_exits(tmp_path):
    data = _config_with_auth_and_injection()
    data["client_injection"]["listeners"][0]["bind"] = "0.0.0.0"
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


def test_injection_non_loopback_bind_with_allow_public_warns(tmp_path, capsys):
    data = _config_with_auth_and_injection()
    data["client_injection"]["listeners"][0]["bind"] = "0.0.0.0"
    data["client_injection"]["allow_public_injection"] = True
    path = write_config(tmp_path, data)
    load_config(path)  # allow_public_injection=true unlocks the bind; warning still fires
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "non-loopback" in captured.err


def test_injection_non_loopback_bind_with_auth_and_allow_public_still_warns(tmp_path, capsys):
    # auth.enabled=true does NOT silence the non-loopback warning —
    # injection bypasses main-port auth.
    data = _config_with_auth_and_injection()
    data["client_injection"]["listeners"][0]["bind"] = "192.168.1.50"
    data["client_injection"]["allow_public_injection"] = True
    path = write_config(tmp_path, data)
    load_config(path)
    captured = capsys.readouterr()
    assert "non-loopback" in captured.err


def test_injection_localhost_bind_accepted(tmp_path):
    data = _config_with_auth_and_injection()
    data["client_injection"]["listeners"][0]["bind"] = "localhost"
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.client_injection.listeners[0].bind == "localhost"


def test_injection_ipv6_loopback_bind_accepted(tmp_path):
    data = _config_with_auth_and_injection()
    data["client_injection"]["listeners"][0]["bind"] = "::1"
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.client_injection.listeners[0].bind == "::1"


# ---------------------------------------------------------------------------
# Routing config
# ---------------------------------------------------------------------------


def test_routing_model_aware(tmp_path):
    data = base_config()
    data["routing"] = {"strategy": "model_aware", "model_poll_timeout": 5}
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.routing.strategy == "model_aware"
    assert cfg.routing.model_poll_timeout == 5


def test_routing_invalid_strategy_exits(tmp_path):
    data = base_config()
    data["routing"] = {"strategy": "least_loaded"}
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


# ---------------------------------------------------------------------------
# Embedding cache config
# ---------------------------------------------------------------------------


def test_embedding_cache_config(tmp_path):
    data = base_config()
    data["embedding_cache"] = {
        "enabled": True,
        "backend": "redis://valkey:6379/0",
        "ttl": 3600,
        "max_entry_bytes": 16384,
    }
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.embedding_cache.enabled is True
    assert cfg.embedding_cache.backend == "redis://valkey:6379/0"
    assert cfg.embedding_cache.ttl == 3600


# ---------------------------------------------------------------------------
# keep_alive config
# ---------------------------------------------------------------------------


def test_keep_alive_config(tmp_path):
    data = base_config()
    data["keep_alive"] = {"default": "10m", "override": True}
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.keep_alive.default == "10m"
    assert cfg.keep_alive.override is True


# ---------------------------------------------------------------------------
# Per-client max_concurrent
# ---------------------------------------------------------------------------


def test_max_concurrent_on_key(tmp_path):
    data = base_config()
    data["proxy"] = {"max_concurrent": 4}
    data["auth"] = {
        "enabled": True,
        "keys": [{"key": "k", "client_id": "batch", "max_concurrent": 2}],
    }
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.auth.keys[0].max_concurrent == 2


def test_max_concurrent_zero_unlimited(tmp_path):
    data = base_config()
    data["auth"] = {
        "enabled": True,
        "keys": [{"key": "k", "client_id": "svc", "max_concurrent": 0}],
    }
    path = write_config(tmp_path, data)
    cfg = load_config(path)
    assert cfg.auth.keys[0].max_concurrent == 0


def test_max_concurrent_exceeds_global_exits(tmp_path):
    data = base_config()
    data["proxy"] = {"max_concurrent": 2}
    data["auth"] = {
        "enabled": True,
        "keys": [{"key": "k", "client_id": "batch", "max_concurrent": 5}],
    }
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


def test_max_concurrent_negative_rejected(tmp_path):
    data = base_config()
    data["auth"] = {
        "enabled": True,
        "keys": [{"key": "k", "client_id": "svc", "max_concurrent": -1}],
    }
    path = write_config(tmp_path, data)
    with pytest.raises(SystemExit):
        load_config(path)


# ---------------------------------------------------------------------------
# OQP_ env overrides for new sections
# ---------------------------------------------------------------------------


def test_env_override_routing_strategy(tmp_path, monkeypatch):
    path = write_config(tmp_path, base_config())
    monkeypatch.setenv("OQP_ROUTING__STRATEGY", "model_aware")
    cfg = load_config(path)
    assert cfg.routing.strategy == "model_aware"


def test_env_override_embedding_cache_enabled(tmp_path, monkeypatch):
    path = write_config(tmp_path, base_config())
    monkeypatch.setenv("OQP_EMBEDDING_CACHE__ENABLED", "true")
    cfg = load_config(path)
    assert cfg.embedding_cache.enabled is True


# ---------------------------------------------------------------------------
# Non-literal API key sources (0.4.0)
#
# Before this, a literal in config.yml was the only option — and not by design.
# _apply_env_overrides skips any path containing a numeric component, so
# OQP_AUTH__KEYS__0__KEY is silently ignored; the list index is what trips it.
# Everything else can come from the environment, which is why the gap was not
# obvious: the mechanism works everywhere except the field that most needs it.
# ---------------------------------------------------------------------------

SECRET = "s3cr3t-resolved-key-not-a-literal-in-yaml"


def _key_entry(**kwargs) -> dict:
    base = {"client_id": "consumer", "max_priority": "normal"}
    base.update(kwargs)
    return base


def test_literal_key_still_works():
    """The live deployment has 11 literal keys and must keep loading unchanged."""
    cfg = ApiKeyConfig(**_key_entry(key=SECRET))
    assert cfg.key == SECRET


def test_key_env_resolves_from_the_environment(monkeypatch):
    monkeypatch.setenv("OQP_KEY_CONSUMER", SECRET)
    cfg = ApiKeyConfig(**_key_entry(key_env="OQP_KEY_CONSUMER"))
    assert cfg.key == SECRET


def test_key_file_resolves_from_a_file(tmp_path):
    f = tmp_path / "oqp-consumer"
    f.write_text(SECRET)
    cfg = ApiKeyConfig(**_key_entry(key_file=str(f)))
    assert cfg.key == SECRET


def test_key_file_strips_the_trailing_newline(tmp_path):
    """`echo secret > file` and every secret manager that writes a file leave a
    trailing newline. A key differing from the expected one by \\n fails
    authentication with nothing in the logs explaining why."""
    f = tmp_path / "oqp-consumer"
    f.write_text(SECRET + "\n")
    cfg = ApiKeyConfig(**_key_entry(key_file=str(f)))
    assert cfg.key == SECRET


def test_exactly_one_source_required_none_given():
    with pytest.raises(ValidationError, match="set exactly one of key, key_env or key_file"):
        ApiKeyConfig(**_key_entry())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"key": SECRET, "key_env": "OQP_KEY_CONSUMER"},
        {"key": SECRET, "key_file": "/run/secrets/x"},
        {"key_env": "OQP_KEY_CONSUMER", "key_file": "/run/secrets/x"},
    ],
)
def test_exactly_one_source_required_two_given(kwargs):
    with pytest.raises(ValidationError, match="set exactly one of key, key_env or key_file"):
        ApiKeyConfig(**_key_entry(**kwargs))


def test_missing_env_var_fails_fast_naming_the_client(monkeypatch):
    monkeypatch.delenv("OQP_KEY_ABSENT", raising=False)
    with pytest.raises(ValidationError) as exc:
        ApiKeyConfig(**_key_entry(key_env="OQP_KEY_ABSENT"))
    assert "consumer" in str(exc.value), "the message must name the client_id"
    assert "OQP_KEY_ABSENT" in str(exc.value)


def test_unreadable_key_file_fails_fast_naming_the_client(tmp_path):
    with pytest.raises(ValidationError) as exc:
        ApiKeyConfig(**_key_entry(key_file=str(tmp_path / "does-not-exist")))
    assert "consumer" in str(exc.value)


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_empty_resolved_key_is_rejected(monkeypatch, value):
    """An empty credential authenticates no one and is never intentional. Without
    this an unset-but-present env var yields a key of "" that silently matches
    nothing, which reads as 'auth is broken' rather than 'config is wrong'."""
    monkeypatch.setenv("OQP_KEY_EMPTY", value)
    with pytest.raises(ValidationError, match="empty key"):
        ApiKeyConfig(**_key_entry(key_env="OQP_KEY_EMPTY"))


def test_error_messages_never_contain_the_resolved_key(monkeypatch, tmp_path):
    """A message that echoes the key to explain the key is wrong puts the credential
    in the log the operator is about to paste into a chat window."""
    monkeypatch.setenv("OQP_KEY_CONSUMER", SECRET)
    with pytest.raises(ValidationError) as exc:
        ApiKeyConfig(**_key_entry(key=SECRET, key_env="OQP_KEY_CONSUMER"))
    assert SECRET not in str(exc.value)


def test_resolved_key_is_absent_from_repr(monkeypatch):
    """repr() reaches logs through tracebacks, debug prints and pydantic's own
    validation errors quoting the model — none of which are deliberate logging."""
    monkeypatch.setenv("OQP_KEY_CONSUMER", SECRET)
    cfg = ApiKeyConfig(**_key_entry(key_env="OQP_KEY_CONSUMER"))
    assert SECRET not in repr(cfg)
    assert SECRET not in str(cfg)


def test_resolved_key_never_reaches_logs_at_debug(monkeypatch, tmp_path):
    """A resolved credential must not appear in log output at the most verbose level.

    Deliberately NOT written with `caplog.at_level`. That helper installs its own
    handler and forces a level, so it reports what logging *would* emit under a
    configuration the application never uses — a test that passes while production
    is silent, or passes while production is loud. This drives the app's own
    `_configure_logging` with `level: debug` and captures what that configuration
    actually produces.

    The positive control at the end is the part that makes the negative assertion
    mean anything: it proves this harness can see a DEBUG record at all. Without it
    a capture that silently collected nothing — a propagate=False somewhere, a
    handler on the wrong logger — would report "the key is not in the logs" for the
    same reason it would report that about any string whatsoever.
    """
    import io
    import logging

    from ollama_queue_proxy.auth import AuthManager
    from ollama_queue_proxy.config import Config
    from ollama_queue_proxy.main import _configure_logging

    monkeypatch.setenv("OQP_KEY_CONSUMER", SECRET)
    cfg_path = write_config(
        tmp_path,
        {
            **base_config(),
            "logging": {"level": "debug", "format": "text"},
            "auth": {
                "enabled": True,
                "keys": [{"key_env": "OQP_KEY_CONSUMER", "client_id": "consumer"}],
            },
        },
    )

    root = logging.getLogger()
    original_level = root.level
    original_handlers = root.handlers[:]
    stream = io.StringIO()
    try:
        config: Config = load_config(cfg_path)
        assert config.auth.keys[0].key == SECRET, "precondition: the key did resolve"

        _configure_logging(config)
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)

        # Exercise the paths that hold the credential: construction, a successful
        # match, and a failed one. A rejection handler echoing the presented key is
        # the most likely way this leaks.
        mgr = AuthManager(config.auth)
        assert mgr.lookup_key(SECRET) is not None
        assert mgr.lookup_key("wrong-key-entirely") is None

        logging.getLogger("ollama_queue_proxy").debug("config loaded: %r", config.auth)

        captured = stream.getvalue()
        assert SECRET not in captured, "the resolved API key reached the logs at DEBUG"

        # CONTROL — see the docstring.
        logging.getLogger("ollama_queue_proxy").debug("canary-%s", "9f3ac1")
        assert "canary-9f3ac1" in stream.getvalue(), (
            "the capture saw no DEBUG output at all, so the assertion above proved nothing"
        )
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)


def test_world_readable_key_file_warns(tmp_path):
    """NE-05. A credential in a 0644 file is exposed to every local user and nothing
    else in the system would ever mention it."""
    import os

    f = tmp_path / "oqp-consumer"
    f.write_text(SECRET)
    os.chmod(f, 0o644)
    with pytest.warns(UserWarning, match="readable beyond its owner"):
        cfg = ApiKeyConfig(**_key_entry(key_file=str(f)))
    assert cfg.key == SECRET, "the warning must not stop the key resolving"


def test_owner_only_key_file_does_not_warn(tmp_path):
    """CONTROL: warning on a correctly-permissioned file would fire for every
    well-configured deployment, which trains people to ignore the warning."""
    import os
    import warnings as _w

    f = tmp_path / "oqp-consumer"
    f.write_text(SECRET)
    os.chmod(f, 0o600)
    with _w.catch_warnings():
        _w.simplefilter("error")  # any warning becomes an exception
        cfg = ApiKeyConfig(**_key_entry(key_file=str(f)))
    assert cfg.key == SECRET
