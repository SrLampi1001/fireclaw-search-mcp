"""Environment-driven configuration for the server.

Single source of truth for defaults and env-var parsing. All settings are
read at process start; nothing here mutates global state.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from e


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a float; got {raw!r}") from e


def _resolve_log_level() -> int:
    raw = _env_str("MCP_LOG_LEVEL", _env_str("FASTMCP_LOG_LEVEL", "WARNING")).upper()
    mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    if raw not in mapping:
        raise ValueError(f"MCP_LOG_LEVEL must be one of {sorted(mapping)}; got {raw!r}")
    return mapping[raw]


def _resolve_api_key() -> str:
    """FIRECRAWL_API_KEY, with FIRECLAW_API_KEY as a legacy alias.

    The project name uses the misspelling; the alias exists only for parity
    with older deployment scripts. Prefer the correctly-spelled variable.
    """
    key = os.environ.get("FIRECRAWL_API_KEY") or os.environ.get("FIRECLAW_API_KEY")
    return key.strip() if key else ""


@dataclass(frozen=True)
class Settings:
    api_key: str
    transport: str
    host: str
    port: int
    path: str
    cache_dir: Path
    cache_ttl_secs: int
    min_credits: int
    max_markdown_chars: int
    min_interval_ms: int
    http_timeout_s: float
    log_level: int
    firecrawl_base_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        transport = _env_str("FIRECRAWL_TRANSPORT", "http").lower()
        if transport not in {"http", "stdio"}:
            raise ValueError(f"FIRECRAWL_TRANSPORT must be 'http' or 'stdio'; got {transport!r}")
        min_credits = _env_int("FIRECRAWL_MIN_CREDITS", 4)
        if min_credits < 0:
            raise ValueError("FIRECRAWL_MIN_CREDITS must be >= 0")
        max_markdown_chars = _env_int("FIRECRAWL_MAX_MARKDOWN_CHARS", 8000)
        if max_markdown_chars < 256:
            raise ValueError("FIRECRAWL_MAX_MARKDOWN_CHARS must be >= 256")
        min_interval_ms = _env_int("FIRECRAWL_MIN_INTERVAL_MS", 0)
        if min_interval_ms < 0:
            raise ValueError("FIRECRAWL_MIN_INTERVAL_MS must be >= 0")
        http_timeout_s = _env_float("FIRECRAWL_HTTP_TIMEOUT_S", 60.0)
        if http_timeout_s <= 0:
            raise ValueError("FIRECRAWL_HTTP_TIMEOUT_S must be > 0")
        cache_ttl_secs = _env_int("FIRECRAWL_CACHE_TTL_SECS", 86400)
        if cache_ttl_secs < 0:
            raise ValueError("FIRECRAWL_CACHE_TTL_SECS must be >= 0")
        port = _env_int("FIRECRAWL_PORT", 8000)
        if not (1 <= port <= 65535):
            raise ValueError(f"FIRECRAWL_PORT must be in [1, 65535]; got {port}")
        return cls(
            api_key=_resolve_api_key(),
            transport=transport,
            host=_env_str("FIRECRAWL_HOST", "127.0.0.1"),
            port=port,
            path=_env_str("FIRECRAWL_PATH", "/mcp"),
            cache_dir=Path(_env_str("FIRECRAWL_CACHE_DIR", "./cache/firecrawl")).expanduser().resolve(),
            cache_ttl_secs=cache_ttl_secs,
            min_credits=min_credits,
            max_markdown_chars=max_markdown_chars,
            min_interval_ms=min_interval_ms,
            http_timeout_s=http_timeout_s,
            log_level=_resolve_log_level(),
            firecrawl_base_url=_env_str("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev/v2").rstrip("/"),
        )

    @property
    def api_key_set(self) -> bool:
        return bool(self.api_key)


def configure_logging(level: int) -> None:
    """Logs go to stderr; stdout is the JSON-RPC stream."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def clamp_count(value: int | None) -> int:
    """Clamp `max_results` to [1, 10]. None or out-of-range values are clamped."""
    if value is None:
        return 5
    return max(1, min(10, int(value)))
