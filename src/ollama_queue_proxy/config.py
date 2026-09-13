"""Configuration loading and validation for ollama-queue-proxy."""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class HostConfig(BaseModel):
    url: str
    name: str
    weight: int = 1
    model_sync_interval: int = 30

    @field_validator("weight")
    @classmethod
    def positive_weight(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"ollama.hosts[].weight must be a positive integer, got {v}")
        return v

    @field_validator("model_sync_interval")
    @classmethod
    def positive_sync_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"ollama.hosts[].model_sync_interval must be >= 1 second, got {v}")
        return v


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

    # A queued request holds its ENTIRE buffered body in memory until a worker picks
    # it up, so the real memory ceiling is depth x body size, not depth. With the
    # default depths that is 350 queued requests; at the default 50 MB
    # max_request_body_mb the existing per-request limit permits ~17 GB of resident
    # request bodies. Only the per-request size was bounded before this.
    #
    # Sized at 512 MB: comfortably above any realistic embedding or chat batch, far
    # below what would trouble the host. Counts QUEUED bytes only — bodies in flight
    # are already bounded by proxy.max_concurrent.
    max_queued_mb: int = 512


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


# Scope levels, ordered and strictly cumulative: management > inference > read.
# ONE axis, three named states. A second boolean alongside `management:` would give four
# states of which one ("may manage the queue but may not use it") is nonsense, and the
# nonsense state is the one nobody writes a test for.
SCOPE_ORDER: dict[str, int] = {"read": 0, "inference": 1, "management": 2}


class ApiKeyConfig(BaseModel):
    """One consumer's credential and the policy attached to it.

    The key may be given three ways — as a literal, from the environment, or from a
    file — and EXACTLY ONE must be used. Before 0.4.0 a literal in config.yml was the
    only option, and not by design: `_apply_env_overrides` skips any path containing a
    numeric component, so `OQP_AUTH__KEYS__0__KEY` is silently ignored. The index is
    what trips it. Every other setting can come from the environment (the deployment
    this was found on passes OQP_EMBEDDING_CACHE__BACKEND that way), which is why the
    gap was not obvious: the mechanism works everywhere except the one field that most
    needs it. That is the direct cause of plaintext keys sitting in config repositories.
    """

    # repr=False so a credential cannot reach a log through an incidental repr() of the
    # config object — a traceback, a debug print, a pydantic validation error quoting
    # the model. The resolved value still lives here for auth.py to read.
    key: str | None = Field(default=None, repr=False)
    key_env: str | None = None
    key_file: str | None = None
    client_id: str
    description: str | None = None
    max_priority: Literal["high", "normal", "low"] = "normal"
    # `inference` is the default because it is what EVERY key did before 0.5.0 — there
    # was no key that could not buy GPU time. An existing config must keep working with
    # no edit, so the default has to be the old behaviour, not the safer one.
    scope: Literal["read", "inference", "management"] = "inference"
    # DEPRECATED in 0.5.0, still honoured. Superseded by `scope`. Deliberately not
    # removed: it is a shipped key on deployed services (11 of them on the reference
    # deployment), and removing it would turn a working config into a boot failure on
    # upgrade. See `reconcile_deprecated_management` below.
    management: bool = False
    max_concurrent: int = 0  # 0 = unlimited (subject to proxy.max_concurrent)

    @field_validator("max_concurrent")
    @classmethod
    def non_negative_concurrent(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"auth.keys[].max_concurrent must be a non-negative integer, got {v}")
        return v

    def allows(self, required: str) -> bool:
        """True if this key's scope is at or above `required`.

        Every scope decision in the codebase goes through this one comparison. Spelled
        out per call site instead, adding a fourth level would mean finding each of
        them by hand — and the one that was missed would fail OPEN, which is the only
        direction that matters.

        An unknown `required` raises KeyError rather than returning False. Every caller
        passes a literal, so a typo is a coding error, and a 500 is a louder and safer
        answer than silently granting or silently refusing.
        """
        return SCOPE_ORDER[self.scope] >= SCOPE_ORDER[required]

    @model_validator(mode="after")
    def reconcile_deprecated_management(self) -> ApiKeyConfig:
        """Map the deprecated `management: bool` onto `scope`, or refuse to boot.

        `management: true` on its own still grants management — that is the entire point
        of deprecating rather than removing it. What is refused is a config that sets
        both and disagrees with itself. Resolving that by precedence would settle a
        privilege question silently, and whichever way the rule went, half the operators
        who wrote it would get the opposite of what they meant.

        `management: false` is NOT treated as conflicting with a higher scope. It is the
        field's default value, so writing it asserts nothing and is indistinguishable
        from a key that never mentioned the deprecated field — whereas an explicit
        `true` is a privilege claim that genuinely can conflict. This also keeps the
        obvious migration (leave the old `management: false` lines alone, add `scope:`
        to the one key that needs it) from becoming a boot failure.
        """
        if not self.management:
            return self
        if "scope" in self.model_fields_set and self.scope != "management":
            raise ValueError(
                f"auth.keys[] entry for client_id={self.client_id!r} sets both "
                f"management: true and scope: {self.scope!r}, which contradict each "
                "other. `management` is deprecated: set scope: management on its own."
            )
        self.scope = "management"
        return self

    @model_validator(mode="after")
    def resolve_key_source(self) -> ApiKeyConfig:
        """Resolve exactly one of key / key_env / key_file into `key`.

        Every failure message names the client_id and never the value. A message that
        echoes the key to explain that the key is wrong puts it in the log the operator
        is about to paste somewhere.
        """
        sources = [
            ("key", self.key),
            ("key_env", self.key_env),
            ("key_file", self.key_file),
        ]
        given = [name for name, value in sources if value is not None]

        if len(given) == 0:
            raise ValueError(
                f"auth.keys[] entry for client_id={self.client_id!r} has no key: "
                "set exactly one of key, key_env or key_file"
            )
        if len(given) > 1:
            raise ValueError(
                f"auth.keys[] entry for client_id={self.client_id!r} sets "
                f"{', '.join(given)} — set exactly one of key, key_env or key_file"
            )

        if self.key_env is not None:
            resolved = os.environ.get(self.key_env)
            if resolved is None:
                raise ValueError(
                    f"auth.keys[] entry for client_id={self.client_id!r} names "
                    f"key_env={self.key_env!r}, which is not set in the environment"
                )
            self.key = resolved
        elif self.key_file is not None:
            try:
                resolved = Path(self.key_file).read_text()
            except OSError as e:
                raise ValueError(
                    f"auth.keys[] entry for client_id={self.client_id!r} names "
                    f"key_file={self.key_file!r}, which cannot be read: {e.strerror}"
                ) from e
            # Warn — do not fail — if the file is readable beyond its owner. A
            # credential in a 0644 file is exposed to every local user, and nothing
            # else in the system would ever mention it (NE-05). Not fatal, because
            # the correct mode depends on deployment: a Docker secret is 0444 inside
            # the container and owned by root, and refusing that would make the
            # feature unusable exactly where it is most wanted.
            try:
                mode = Path(self.key_file).stat().st_mode
                if mode & 0o077:
                    warnings.warn(
                        f"auth.keys[] key_file for client_id={self.client_id!r} is "
                        f"mode {mode & 0o777:04o} — readable beyond its owner. "
                        "Prefer 0600.",
                        stacklevel=2,
                    )
            except OSError:
                pass  # already read successfully; a stat failure here is not fatal

            # Strip trailing whitespace. `echo secret > file` and every secret manager
            # that writes a file leave a trailing newline, and a key that differs from
            # the expected one by \n fails authentication with no indication why.
            self.key = resolved.strip()

        if not self.key or not self.key.strip():
            source = given[0]
            raise ValueError(
                f"auth.keys[] entry for client_id={self.client_id!r} resolved to an "
                f"empty key from {source}. An empty credential would authenticate no "
                "one and is never intentional"
            )
        return self


class RateLimitConfig(BaseModel):
    max_failures: int = 10
    window_seconds: int = 60


class AuthConfig(BaseModel):
    enabled: bool = False
    keys: list[ApiKeyConfig] = []
    rate_limit: RateLimitConfig = RateLimitConfig()

    @model_validator(mode="after")
    def keys_required_when_enabled(self) -> AuthConfig:
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


# ---------------------------------------------------------------------------
# v0.2.0 — new config sections
# ---------------------------------------------------------------------------


class InjectionListenerConfig(BaseModel):
    listen_port: int
    inject_as: str  # must match an auth.keys[].client_id
    bind: str = "127.0.0.1"

    @field_validator("listen_port")
    @classmethod
    def valid_listen_port(cls, v: int) -> int:
        if not (1024 <= v <= 65535):
            raise ValueError(
                f"client_injection.listeners[].listen_port must be in 1024-65535, got {v}"
            )
        return v


class ClientInjectionConfig(BaseModel):
    listeners: list[InjectionListenerConfig] = []
    allow_public_injection: bool = False


class RoutingConfig(BaseModel):
    strategy: Literal["model_aware", "round_robin"] = "round_robin"
    fallback: Literal["any_healthy"] = "any_healthy"
    model_poll_timeout: int = 3


class EmbeddingCacheConfig(BaseModel):
    enabled: bool = False
    backend: str = "redis://localhost:6379/0"
    ttl: int = 86400
    max_entry_bytes: int = 32768
    key_prefix: str = "oqp:embed:"
    connect_timeout: int = 2


class DashboardConfig(BaseModel):
    """The embedded read-only dashboard.

    There is deliberately no `path` key. The route is registered at import time so that
    a disabled dashboard can answer 404 itself; left unregistered it would fall through
    to the proxy catch-all and either be refused as inference or forwarded to Ollama,
    both of which disclose more than a 404 does. Config is not loaded at import time, so
    an unconditional registration cannot take a configured path.

    A fixed path also settles the one hard constraint by construction: the dashboard must
    not take "/", which is in the metadata fast-path list and is proxied to Ollama to
    answer "Ollama is running". With no key there is no validator to get wrong and no
    way for an operator to violate it.
    """

    # Off by default: it fails closed, and a brand-new surface should be opted into
    # rather than appearing on every deployment that upgrades.
    enabled: bool = False
    refresh_seconds: int = 5

    @field_validator("refresh_seconds")
    @classmethod
    def positive_refresh(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"dashboard.refresh_seconds must be >= 1 second, got {v}")
        return v


class KeepAliveConfig(BaseModel):
    default: str = "5m"
    override: bool = False


class Config(BaseModel):
    proxy: ProxyConfig = ProxyConfig()
    ollama: OllamaConfig
    queue: QueueConfig = QueueConfig()
    webhooks: WebhookConfig = WebhookConfig()
    auth: AuthConfig = AuthConfig()
    logging: LoggingConfig = LoggingConfig()
    # v0.2.0 sections
    client_injection: ClientInjectionConfig = ClientInjectionConfig()
    routing: RoutingConfig = RoutingConfig()
    embedding_cache: EmbeddingCacheConfig = EmbeddingCacheConfig()
    keep_alive: KeepAliveConfig = KeepAliveConfig()
    dashboard: DashboardConfig = DashboardConfig()

    @model_validator(mode="after")
    def validate_v2_constraints(self) -> Config:
        self._validate_injection_ports()
        self._validate_inject_as_refs()
        self._validate_client_max_concurrent()
        self._validate_public_injection_bind()
        self._warn_public_injection_no_auth()
        return self

    def _validate_injection_ports(self) -> None:
        seen: set[int] = {self.proxy.port}
        for listener in self.client_injection.listeners:
            if listener.listen_port in seen:
                print(
                    f"FATAL: client_injection.listeners[].listen_port {listener.listen_port} "
                    f"conflicts with another port (proxy.port or another injection listener).",
                    file=sys.stderr,
                )
                sys.exit(1)
            seen.add(listener.listen_port)

    def _validate_inject_as_refs(self) -> None:
        known_ids = {k.client_id for k in self.auth.keys}
        for listener in self.client_injection.listeners:
            if listener.inject_as not in known_ids:
                print(
                    f"FATAL: client_injection.listeners[].inject_as '{listener.inject_as}' "
                    f"does not match any auth.keys[].client_id. Known IDs: {sorted(known_ids)}",
                    file=sys.stderr,
                )
                sys.exit(1)

    def _validate_client_max_concurrent(self) -> None:
        global_cap = self.proxy.max_concurrent
        for key in self.auth.keys:
            if key.max_concurrent > global_cap:
                print(
                    f"FATAL: auth.keys[client_id={key.client_id}].max_concurrent "
                    f"({key.max_concurrent}) exceeds proxy.max_concurrent ({global_cap}). "
                    f"Set max_concurrent <= {global_cap} or increase proxy.max_concurrent.",
                    file=sys.stderr,
                )
                sys.exit(1)

    def _validate_public_injection_bind(self) -> None:
        loopback = {"127.0.0.1", "localhost", "::1"}
        for listener in self.client_injection.listeners:
            if listener.bind in loopback:
                continue
            if not self.client_injection.allow_public_injection:
                print(
                    f"FATAL: client_injection.listeners[listen_port={listener.listen_port}].bind "
                    f"is '{listener.bind}' (non-loopback) but allow_public_injection is false. "
                    "Set allow_public_injection: true to confirm exposing an unauthenticated "
                    "injection port on the network, or change bind to 127.0.0.1.",
                    file=sys.stderr,
                )
                sys.exit(1)

    def _warn_public_injection_no_auth(self) -> None:
        loopback = {"127.0.0.1", "localhost", "::1"}
        has_non_loopback = any(
            listener.bind not in loopback for listener in self.client_injection.listeners
        )
        if self.client_injection.allow_public_injection and not self.auth.enabled:
            print(
                "WARNING: allow_public_injection is true AND auth.enabled is false. "
                "Injection ports will bind on all interfaces with no credential check — "
                "any host on the network can consume queue slots under an injected identity. "
                "Set auth.enabled: true or restrict allow_public_injection: false.",
                file=sys.stderr,
            )
        elif has_non_loopback:
            print(
                "WARNING: one or more client_injection.listeners bind to a non-loopback "
                "address. Injection ports bypass Bearer auth by design — any host able to "
                "reach that port can consume queue slots under the injected client identity. "
                "Restrict access at the firewall / reverse proxy layer.",
                file=sys.stderr,
            )


def _apply_env_overrides(data: dict, prefix: str = "OQP") -> dict:
    """Apply OQP_ env var overrides onto the raw config dict using __ nesting."""
    for key, value in os.environ.items():
        if not key.startswith(prefix + "_"):
            continue
        parts = key[len(prefix) + 1 :].lower().split("__")
        # List-index overrides (e.g. OQP_OLLAMA__HOSTS__0__URL) are not
        # supported — skip the entire key if any component is a digit.
        if any(p.isdigit() for p in parts):
            continue
        target = data
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        leaf = parts[-1]
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
