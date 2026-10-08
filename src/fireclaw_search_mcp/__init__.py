"""fireclaw-search-mcp — Firecrawl-backed MCP server.

Exposes Firecrawl /v2/search, /v2/scrape, and /v2/team/credit-usage as MCP
tools, with on-disk TTL caching and a credit guard. See docs/SPECS.md for the
contract this package commits to.
"""
from __future__ import annotations

__version__ = "0.1.0"
