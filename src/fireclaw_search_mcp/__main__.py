"""Entry point: `python -m fireclaw_search_mcp` and the `fireclaw-search-mcp` script."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .server import mcp
from .settings import Settings, configure_logging

log = logging.getLogger(__name__)

_DOTENV_CANDIDATES = (".env",)


def _load_dotenv() -> None:
    """Best-effort load of `.env` from the current working directory.

    Real env vars always win; we only fill in the gaps. Runs once per process;
    repeated calls are a no-op (python-dotenv tracks `DOTENV_LOADED`).
    """
    try:
        from dotenv import load_dotenv
    except Exception:
        # python-dotenv is installed transitively via FastMCP 4; if it ever
        # disappears we just lose the .env auto-load, not the server.
        return
    for name in _DOTENV_CANDIDATES:
        path = Path(name)
        if path.is_file():
            load_dotenv(path, override=False)
            return


def main() -> None:
    _load_dotenv()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    if settings.transport == "stdio":
        log.info("starting stdio transport (key_set=%s)", settings.api_key_set)
        mcp.run(transport="stdio")
        return
    log.info(
        "starting Streamable HTTP on http://%s:%d%s (key_set=%s, cache=%s, ttl=%ss, min_credits=%d, max_md=%d)",
        settings.host,
        settings.port,
        settings.path,
        settings.api_key_set,
        settings.cache_dir,
        settings.cache_ttl_secs,
        settings.min_credits,
        settings.max_markdown_chars,
    )
    mcp.run(transport="http", host=settings.host, port=settings.port, path=settings.path)


if __name__ == "__main__":
    main()
