"""Integration tests: real Firecrawl calls, gated on opt-in.

Per SPECS §15. Network tests are opt-in via
`FIRECRAWL_NETWORK_TESTS=1`. Skipped by default so CI stays hermetic.

These tests use the real `FIRECRAWL_API_KEY` from `.env` (or the env) and
the real `api.firecrawl.dev` endpoint. Set
`FIRECRAWL_NETWORK_TESTS=1 .venv/bin/pytest tests/test_server_integration.py`
to run them.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastmcp import Client

# Load .env so the integration tests pick up the operator's API key the same
# way the entry-point does. Real env vars still win.
try:
    from dotenv import load_dotenv
    if Path(".env").is_file():
        load_dotenv(".env", override=False)
except Exception:
    pass

from fireclaw_search_mcp.server import mcp

NETWORK_ENABLED = os.environ.get("FIRECRAWL_NETWORK_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not NETWORK_ENABLED,
    reason="network tests disabled (set FIRECRAWL_NETWORK_TESTS=1 to run)",
)


async def test_credit_status_returns_live_snapshot():
    async with Client(mcp) as c:
        r = await c.call_tool("credit_status", {})
        sc = r.structured_content
        assert sc["source"] in {"live", "cached"}
        assert sc["credits"]["remaining_credits"] is not None


async def test_web_search_returns_at_least_one_result():
    async with Client(mcp) as c:
        r = await c.call_tool("web_search", {"query": "HTTP 418", "max_results": 3})
        sc = r.structured_content
        assert sc["cache"] in {"miss", "hit", "bypassed"}
        assert len(sc["results"]) >= 1
        assert sc["results"][0]["markdown_chars"] > 0
        assert sc["credits_used_this_call"] > 0


async def test_scrape_url_against_known_page():
    async with Client(mcp) as c:
        r = await c.call_tool(
            "scrape_url",
            {"url": "https://example.com"},
        )
        sc = r.structured_content
        assert sc["url"] == "https://example.com"
        assert sc["markdown_chars"] > 0


async def test_second_search_within_ttl_serves_from_cache():
    query = "HTTP 429 status code"
    async with Client(mcp) as c:
        r1 = await c.call_tool("web_search", {"query": query, "max_results": 2})
        r2 = await c.call_tool("web_search", {"query": query, "max_results": 2})
        assert r1.structured_content["cache"] == "miss"
        assert r2.structured_content["cache"] == "hit"
