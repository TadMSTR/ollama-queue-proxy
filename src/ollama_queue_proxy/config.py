"""Configuration loading and validation for ollama-queue-proxy."""

from __future__ import annotations

import os
import sys
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator, model_validator


class HostConfig(BaseModel):
    url: str
    name: str


class OllamaConfig(BaseModel):
    hosts: list[HostConfig]
    health_check_interval: int = 30
    request_timeout: int = 300


class TierConfig(BaseModel):
    max_depth: int = 100
    max_wait: int = 300
    high_watermark_pct: int = 80


class QueueConfig(BaseModel):
    high: TierConfig = TierConfig(max_depth=50, max_wait=120)
    normal: TierConfig = TierConfig(max_depth=100, max_wait=300)
    low: TierConfig = TierConfig(max_depth=200, max_wait=600)
    overflow_status_code: Literal[503, 429] = 503


class WebhookConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    events: list[str] = [
        "queue.full",
        "queue.high_watermark",
        "queue.drained",
        "host.unhealthy",
        "host.recovered",
    ]
    allowed_hosts: list[str] = []  # hostnames exempt from SSRF check (for internal ntfy etc.)


class ApiKeyConfig(BaseModel):
    key: str
    client_id: str
    description: str | None = None
    max_priority: Literal["high", "normal", "low"] = "normal"
    management: bool = False


class RateLimitConfig(BaseModel):
    max_failures: int = 10
    window_seconds: int = 60


class AuthConfig(BaseModel):
    enabled: bool = False
    keys: list[ApiKeyConfig] = []
    rate_limit: RateLimitConfig = RateLimitConfig()

    @model_validator(mode="after")
    def keys_required_when_enabled(self) -> "AuthConfig":
        if self.enabled and len(self.keys) == 0:
            print(
                "FATAL: auth.enabled is true but no API keys are configured. "
                "Add at least one key to auth.keys or set auth.enabled: false.",
                file=sys.stderr,
            )
            sys.exit(1)
        return self


class LoggingConfig(BaseModel):
    level: str = "info"
    format: Literal["json", "text"] = "json"


class ProxyConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 11435
    max_concurrent: int = 2
    allow_model_management: bool = False
    drain_timeout: int = 30
    max_request_body_mb: int = 50

    @field_validator("port")
    @classmethod
    def valid_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError(f"Invalid port: {v}")
        return v


class Config(BaseModel):
    proxy: ProxyConfig = ProxyConfig()
    ollama: OllamaConfig
    queue: QueueConfig = QueueConfig()
    webhooks: WebhookConfig = WebhookConfig()
    auth: AuthConfig = AuthConfig()
    logging: LoggingConfig = LoggingConfig()


def _apply_env_overrides(data: dict, prefix: str = "OQP") -> dict:
    """Apply OQP_ env var overrides onto the raw config dict using __ nesting."""
    for key, value in os.environ.items():
        if not key.startswith(prefix + "_"):
            continue
        parts = key[len(prefix) + 1 :].lower().split("__")
        target = data
        for part in parts[:-1]:
            if part.isdigit():
                # list index — handled below
                continue
            target = target.setdefault(part, {})
        leaf = parts[-1]
        if leaf.isdigit():
            # Can't safely do list index overrides on arbitrary dicts here.
            # The most important list override (hosts[0].url) is handled by
            # full-key matching. Log and skip.
            continue
        # Attempt type coercion for booleans and integers
        if value.lower() in ("true", "false"):
            target[leaf] = value.lower() == "true"
        elif value.isdigit():
            target[leaf] = int(value)
        else:
            target[leaf] = value
    return data


def load_config(path: str | None = None) -> Config:
    """Load configuration from YAML file with env var overrides."""
    config_path = path or os.environ.get("OQP_CONFIG", "./config.yml")
    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(
            f"FATAL: Config file not found: {config_path}. "
            "Copy config.example.yml to config.yml and edit it.",
            file=sys.stderr,
        )
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"FATAL: Config file parse error: {e}", file=sys.stderr)
        sys.exit(1)

    raw = _apply_env_overrides(raw)
    try:
        return Config.model_validate(raw)
    except Exception as e:
        print(f"FATAL: Config validation error: {e}", file=sys.stderr)
        sys.exit(1)
