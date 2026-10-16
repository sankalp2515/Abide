"""Configuration loading. Providers and models come from YAML; keys and paths from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from .secrets import ApiKey, load_keys


DEFAULTS = Path(__file__).resolve().parent / "defaults"   # bundled config, policy templates, starter policy


def repo_root() -> Path:
    """Where IntentSeal keeps its configuration and data: INTENTSEAL_HOME, else the source checkout this package runs
    from (development), else a per-user folder (~/.intentseal) for an installed package."""
    env = os.environ.get("INTENTSEAL_HOME")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "providers.yaml").exists():
            return parent
    return Path.home() / ".intentseal"


def config_path(*parts: str) -> Path:
    """A configuration file or folder: the home's own copy if there is one, else the copy bundled with the package."""
    own = repo_root().joinpath("config", *parts)
    return own if own.exists() else DEFAULTS.joinpath(*parts)


def load_env() -> None:
    # The project's own .env (an installed package) first, then the home or repo .env. A value is taken only where the
    # environment has none: the empty placeholders `intentseal init` writes never hide a key set elsewhere.
    for path in (Path.cwd() / ".env", repo_root() / ".env"):
        for key, value in (dotenv_values(path) if path.is_file() else {}).items():
            if value and not os.environ.get(key):
                os.environ[key] = value
    from . import adopt_legacy_env

    adopt_legacy_env()


@dataclass
class ProviderConfig:
    name: str
    type: str
    enabled: bool
    base_url: str
    keys: list[ApiKey]
    priority: int = 50
    timeout_s: float = 30
    connect_timeout_s: float = 5
    retries: int = 1
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60
    circuit_failures: int = 3
    circuit_open_s: float = 60
    key_rotation: str = "on_auth_failure"
    rpm: float | None = None
    tpm: float | None = None              # tokens per minute, per model (provider limits are per model)
    daily_token_budget: int | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.keys)

    @property
    def disabled_reason(self) -> str | None:
        if not self.enabled:
            return "disabled in config"
        if not self.keys:
            return "no keys in environment"
        return None


@dataclass
class ModelRef:
    provider: str
    model: str
    price_in: float = 0.0   # USD per million input tokens (list price, for reporting)
    price_out: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoleConfig:
    name: str
    models: list[ModelRef]
    cache: bool = True
    cache_ttl_s: int = 86400
    defaults: dict[str, Any] = field(default_factory=dict)
    wait_on_rate_limit_s: float = 0.0   # >0: wait out a provider cooldown (up to this long) instead of failing
    max_queue_s: float | None = None    # longest client-side throttle wait before failing over (latency-sensitive roles)


@dataclass
class Settings:
    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]
    db_path: Path | str                 # SQLite file, or a postgresql:// URL (INTENTSEAL_DATABASE_URL)


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_providers(path: Path | None = None) -> dict[str, ProviderConfig]:
    path = path or config_path("providers.yaml")
    raw = _read_yaml(path)
    defaults = raw.get("defaults", {}) or {}
    out: dict[str, ProviderConfig] = {}
    for name, p in (raw.get("providers") or {}).items():
        merged = {**defaults, **p}
        keys_cfg = merged.get("keys") or {}
        limits = merged.get("limits") or {}
        budget = merged.get("budget") or {}
        known = {
            "type", "enabled", "base_url", "keys", "priority", "timeout_s", "connect_timeout_s",
            "retries", "backoff_base_s", "backoff_max_s", "circuit_failures", "circuit_open_s",
            "key_rotation", "limits", "budget", "extra_headers", "no_auth",
        }
        out[name] = ProviderConfig(
            name=name,
            type=merged.get("type", "openai_compatible"),
            enabled=bool(merged.get("enabled", True)),
            base_url=str(merged.get("base_url", "")).rstrip("/"),
            # `no_auth: true` (local servers such as Ollama or vLLM): one empty key, so the provider is usable.
            keys=([ApiKey(f"{name}#local", "")] if merged.get("no_auth")
                  else load_keys(name, keys_cfg.get("env_prefix"), keys_cfg.get("env_list"))),
            priority=int(merged.get("priority", 50)),
            timeout_s=float(merged.get("timeout_s", 30)),
            connect_timeout_s=float(merged.get("connect_timeout_s", 5)),
            retries=int(merged.get("retries", 1)),
            backoff_base_s=float(merged.get("backoff_base_s", 1.0)),
            backoff_max_s=float(merged.get("backoff_max_s", 60)),
            circuit_failures=int(merged.get("circuit_failures", 3)),
            circuit_open_s=float(merged.get("circuit_open_s", 60)),
            key_rotation=str(merged.get("key_rotation", "on_auth_failure")),
            rpm=limits.get("rpm"),
            tpm=limits.get("tpm"),
            daily_token_budget=budget.get("daily_tokens"),
            extra_headers=dict(merged.get("extra_headers") or {}),
            options={k: v for k, v in merged.items() if k not in known},
        )
    return out


def load_roles(path: Path | None = None) -> dict[str, RoleConfig]:
    path = path or config_path("models.yaml")
    raw = _read_yaml(path)
    out: dict[str, RoleConfig] = {}
    for name, r in (raw.get("roles") or {}).items():
        models = [
            ModelRef(
                provider=m["provider"],
                model=m["model"],
                price_in=float((m.get("price_per_mtok") or {}).get("input", 0.0)),
                price_out=float((m.get("price_per_mtok") or {}).get("output", 0.0)),
                params=dict(m.get("params") or {}),
            )
            for m in r.get("models", [])
        ]
        out[name] = RoleConfig(
            name=name,
            models=models,
            cache=bool(r.get("cache", True)),
            cache_ttl_s=int(r.get("cache_ttl_s", 86400)),
            defaults=dict(r.get("defaults") or {}),
            wait_on_rate_limit_s=float(r.get("wait_on_rate_limit_s", 0.0)),
            max_queue_s=r.get("max_queue_s"),
        )
    return out


def load_settings() -> Settings:
    load_env()
    url = os.environ.get("INTENTSEAL_DATABASE_URL", "").strip()
    db: Path | str = url or Path(os.environ.get("INTENTSEAL_DB_PATH") or repo_root() / "data" / "intentseal.db")
    return Settings(providers=load_providers(), roles=load_roles(), db_path=db)
