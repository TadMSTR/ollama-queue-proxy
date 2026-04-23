"""Tests for config loading and validation."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap

import pytest
import yaml

from ollama_queue_proxy.config import load_config


def write_config(tmp_path, data: dict) -> str:
    path = str(tmp_path / "config.yml")
    with open(path, "w") as f:
        yaml.safe_dump(data, f)
    return path


def base_config() -> dict:
    return {
        "ollama": {
            "hosts": [{"url": "http://ollama:11434", "name": "primary"}]
        }
    }


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
    data["client_injection"] = {
        "listeners": [{"listen_port": port, "inject_as": inject_as}]
    }
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
    # auth.enabled=true does NOT silence the non-loopback warning — injection bypasses main-port auth.
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
