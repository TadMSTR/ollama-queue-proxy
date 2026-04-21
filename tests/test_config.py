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
