"""Environment config: config/<env>.yaml with ${VAR} interpolation and no-cloud validation."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

PORT_NAMES: tuple[str, ...] = (
    "document_source",
    "upload_source",
    "malware_scanner",
    "raw_store",
    "registry",
    "chunk_index",
    "embedder",
    "reranker",
    "llm",
    "sql_engine",
    "identity_provider",
    "graph_store",
    "tracer",
)

# Hosted AI endpoints. A model port pointing at any of these is a config error (CLAUDE.md rule 2).
HOSTED_AI_HOST_SUFFIXES: tuple[str, ...] = (
    "openai.com",
    "openai.azure.com",
    "anthropic.com",
    "googleapis.com",
    "generativelanguage.googleapis.com",
    "cohere.ai",
    "cohere.com",
    "mistral.ai",
    "groq.com",
    "together.xyz",
    "deepseek.com",
    "huggingface.co",
    "hf.space",
    "bedrock-runtime.amazonaws.com",
    "moonshot.cn",
    "openrouter.ai",
)
MODEL_PORTS: tuple[str, ...] = ("embedder", "reranker", "llm")

_VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigError(ValueError):
    """Raised for an invalid or unsafe configuration."""


class PortConfig(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    adapter: str

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    env: str
    paths: dict[str, str]
    network: dict[str, list[str]]
    services: dict[str, dict[str, Any]] = Field(default_factory=dict)
    gateway: dict[str, Any] = Field(default_factory=dict)
    processing: dict[str, Any] = Field(default_factory=dict)
    retrieval: dict[str, Any] = Field(default_factory=dict)
    graph: dict[str, Any] = Field(default_factory=dict)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    ports: dict[str, PortConfig]

    @property
    def allowed_hosts(self) -> list[str]:
        return list(self.network.get("allowed_hosts", []))

    def path(self, key: str) -> Path:
        p = Path(self.paths[key])
        return p if p.is_absolute() else REPO_ROOT / p

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        missing = [p for p in PORT_NAMES if p not in self.ports]
        if missing:
            raise ConfigError(f"ports missing from config: {missing}")
        unknown = [p for p in self.ports if p not in PORT_NAMES]
        if unknown:
            raise ConfigError(f"unknown ports in config: {unknown}")
        for name, port in self.ports.items():
            for url in _urls_in(port.options):
                host = (urlparse(url).hostname or "").lower()
                if not host:
                    continue
                if name in MODEL_PORTS and _is_hosted_ai(host):
                    raise ConfigError(f"port {name} points at hosted AI endpoint {host}")
                if host not in self.allowed_hosts:
                    raise ConfigError(f"port {name} uses host {host} not in network.allowed_hosts")
        auth = self.gateway.get("auth")
        if auth in ("dev_header", "local_jwt") and self.env != "local":
            raise ConfigError(f"gateway.auth={auth} is a local-only development mode")
        if self.gateway.get("trusted_clients") and self.env != "local":
            raise ConfigError("gateway.trusted_clients (dev header) is local-only")
        for svc, opts in self.services.items():
            for url in _urls_in(opts):
                host = (urlparse(url).hostname or "").lower()
                if host and host not in self.allowed_hosts:
                    raise ConfigError(f"service {svc} uses host {host} not in network.allowed_hosts")
        return self


def _is_hosted_ai(host: str) -> bool:
    return any(host == s or host.endswith("." + s) for s in HOSTED_AI_HOST_SUFFIXES)


def _urls_in(opts: dict[str, Any]) -> list[str]:
    return [v for v in opts.values() if isinstance(v, str) and "://" in v and not v.startswith("file://")]


def _interpolate(value: Any, env: dict[str, str], strict: bool) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            key = m.group(1)
            if key in env:
                return env[key]
            if strict:
                raise ConfigError(f"environment variable {key} is not set")
            return m.group(0)

        return _VAR.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, env, strict) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, env, strict) for v in value]
    return value


def read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def load_settings(env: str | None = None, *, strict: bool = False, config_dir: Path = CONFIG_DIR) -> Settings:
    """Load config/<env>.yaml. strict=True fails on unset ${VAR}s (services need them)."""
    env = env or os.environ.get("KB_ENV", "local")
    path = config_dir / f"{env}.yaml"
    if not path.exists():
        raise ConfigError(f"no config for env {env!r} at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    values = {**read_dotenv(REPO_ROOT / ".env.local"), **os.environ}
    data = _interpolate(raw, values, strict)
    try:
        return Settings.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"{path.name}: {e}") from e
