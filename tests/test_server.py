"""In-process tests for the FastMCP server.

These run against the in-memory FastMCP instance via `fastmcp.Client`. The
outbound Firecrawl call is mocked at the `FirecrawlClient.search` /
`FirecrawlClient.scrape` / `FirecrawlClient.credit_usage` class methods
(per the start-up-prompt's "mock at the function boundary, not at the
lifespan" rule). The real network is exercised separately in
`test_server_integration.py` (skipped unless
`FIRECRAWL_NETWORK_TESTS=1`).
"""
from __future__ import annotations

import pytest
from fastmcp import Client

import fireclaw_search_mcp.firecrawl as fc_module
import fireclaw_search_mcp.server as server_module
from fireclaw_search_mcp.server import EMPTY_SEARCH_NOTE, mcp


class FakeFirecrawl:
    """Pluggable canned responses keyed by query/url. Wire into the running
    server by patching the methods on `FirecrawlClient` (see the
    `fake_firecrawl` fixture).
    """

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], dict] = {}
        self.exceptions: dict[tuple[str, str], BaseException] = {}
        self.search_calls: list[str] = []
        self.scrape_calls: list[str] = []
        self.credit_usage_calls: int = 0

    def set_search(self, query: str, payload: dict) -> None:
        self.responses[("search", query)] = payload

    def set_scrape(self, url: str, payload: dict) -> None:
        self.responses[("scrape", url)] = payload

    def set_credit_usage(self, payload: dict) -> None:
        self.responses[("credit_usage", "")] = payload

    def set_search_error(self, query: str, exc: BaseException) -> None:
        self.exceptions[("search", query)] = exc

    def set_scrape_error(self, url: str, exc: BaseException) -> None:
        self.exceptions[("scrape", url)] = exc

    def set_credit_usage_error(self, exc: BaseException) -> None:
        self.exceptions[("credit_usage", "")] = exc

    async def search(self, query: str, limit: int = 5, *, include_markdown: bool = True) -> dict:
        self.search_calls.append(query)
        if ("search", query) in self.exceptions:
            raise self.exceptions[("search", query)]
        return self.responses.get(("search", query), {
            "results": [], "credits_used_this_call": 0, "web_raw_count": 0,
        })

    async def scrape(self, url: str) -> dict:
        self.scrape_calls.append(url)
        if ("scrape", url) in self.exceptions:
            raise self.exceptions[("scrape", url)]
        return self.responses.get(("scrape", url), {
            "markdown": "", "markdown_chars": 0, "markdown_truncated_chars": 0,
            "truncated": False, "title": "", "description": "", "language": "",
            "source_url": url, "final_url": url, "status_code": 200,
            "metadata": {}, "credits_used_this_call": 0,
        })

    async def credit_usage(self) -> dict:
        self.credit_usage_calls += 1
        if ("credit_usage", "") in self.exceptions:
            raise self.exceptions[("credit_usage", "")]
        return self.responses.get(("credit_usage", ""), {
            "remaining_credits": 1000, "plan_credits": 1000,
            "billing_period_start": None, "billing_period_end": "2026-11-01T00:00:00Z",
        })


@pytest.fixture(autouse=True)
def _fresh_cache_per_test(monkeypatch):
    """test_server.py additionally pins a fake API key and silences logs.

    Cache dir comes from the autouse `_per_test_cache` fixture in conftest.
    """
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key-for-unit-tests")
    monkeypatch.delenv("FIRECLAW_API_KEY", raising=False)
    monkeypatch.setenv("MCP_LOG_LEVEL", "ERROR")
    yield


@pytest.fixture
def fake_firecrawl(monkeypatch):
    """Replace the FirecrawlClient methods with a controllable fake.

    Class methods are patched so the lifespan's FirecrawlClient instance
    routes every call to the fake. This is the fixture-mock-pattern from
    the start-up-prompt: mock the outbound function boundary, NOT the
    lifespan.
    """
    fake = FakeFirecrawl()
    fake.set_credit_usage({
        "remaining_credits": 1000,
        "plan_credits": 1000,
        "billing_period_start": "2026-10-01T00:00:00Z",
        "billing_period_end": "2026-11-01T00:00:00Z",
    })

    async def _search(self, query: str, limit: int = 5, *, include_markdown: bool = True) -> dict:
        return await fake.search(query, limit, include_markdown=include_markdown)

    async def _scrape(self, url: str) -> dict:
        return await fake.scrape(url)

    async def _credit_usage(self) -> dict:
        return await fake.credit_usage()

    monkeypatch.setattr(fc_module.FirecrawlClient, "search", _search)
    monkeypatch.setattr(fc_module.FirecrawlClient, "scrape", _scrape)
    monkeypatch.setattr(fc_module.FirecrawlClient, "credit_usage", _credit_usage)
    return fake


# ---------------------------------------------------------------- tests -----


async def test_tools_list_advertises_three_tools(fake_firecrawl):
    async with Client(mcp) as c:
        tools = await c.list_tools()
        names = sorted(t.name for t in tools)
        assert names == ["credit_status", "scrape_url", "web_search"]


async def test_prompts_list_advertises_two_prompts(fake_firecrawl):
    async with Client(mcp) as c:
        prompts = await c.list_prompts()
        names = sorted(p.name for p in prompts)
        assert names == ["fetch_document", "research_topic"]


async def test_research_topic_prompt_returns_user_message(fake_firecrawl):
    async with Client(mcp) as c:
        p = await c.get_prompt("research_topic", {"topic": "PostgreSQL"})
        assert p.messages
        text = p.messages[0].content.text
        assert "PostgreSQL" in text
        assert "canonical topic name" in text


async def test_fetch_document_prompt_returns_user_message(fake_firecrawl):
    async with Client(mcp) as c:
        p = await c.get_prompt("fetch_document", {
            "url": "https://example.com/docs",
            "focus": "the public schema introspection API",
        })
        text = p.messages[0].content.text
        assert "https://example.com/docs" in text
        assert "public schema introspection API" in text


async def test_basic_search_returns_structured_results(fake_firecrawl):
    fake_firecrawl.set_search("Rust programming language", {
        "results": [
            {"title": "Rust (programming language)", "url": "https://example.com/rust",
             "description": "A systems language", "markdown": "Rust is a systems language.",
             "markdown_chars": 28, "markdown_truncated_chars": 28, "truncated": False},
        ],
        "credits_used_this_call": 3,
        "web_raw_count": 1,
    })
    async with Client(mcp) as c:
        result = await c.call_tool("web_search", {"query": "Rust programming language"})
        sc = result.structured_content
        assert sc["query"] == "Rust programming language"
        assert sc["cache"] == "miss"
        assert sc["credits_used_this_call"] == 3
        assert sc["results"][0]["title"] == "Rust (programming language)"
        assert sc["results"][0]["url"].startswith("https://example.com/rust")
        assert "credits" in sc


async def test_query_is_trimmed_and_echoed(fake_firecrawl):
    fake_firecrawl.set_search("hello", {
        "results": [], "credits_used_this_call": 0, "web_raw_count": 0,
    })
    async with Client(mcp) as c:
        result = await c.call_tool("web_search", {"query": "  hello  "})
        assert result.structured_content["query"] == "hello"
        # Probe + the actual search call.
        assert ("hello",) == tuple(
            c for c in fake_firecrawl.search_calls if c == "hello"
        ) and len(fake_firecrawl.search_calls) == 1


async def test_max_results_is_clamped_to_1_through_10(fake_firecrawl):
    many = [
        {"title": f"r{i}", "url": f"https://example.com/{i}",
         "description": "d", "markdown": "m",
         "markdown_chars": 1, "markdown_truncated_chars": 1, "truncated": False}
        for i in range(12)
    ]
    payload = {"results": many, "credits_used_this_call": 1, "web_raw_count": 12}
    fake_firecrawl.set_search("anything", payload)
    async with Client(mcp) as c:
        r1 = await c.call_tool("web_search", {"query": "anything", "max_results": 0})
        assert len(r1.structured_content["results"]) == 1
        r2 = await c.call_tool("web_search", {"query": "anything", "max_results": 11})
        assert len(r2.structured_content["results"]) == 10
        r3 = await c.call_tool("web_search", {"query": "anything"})
        assert len(r3.structured_content["results"]) == 5


async def test_empty_search_response_returns_empty_with_note(fake_firecrawl):
    fake_firecrawl.set_search("how do I write async code in Rust as a beginner with tokio 2026", {
        "results": [], "credits_used_this_call": 2, "web_raw_count": 0,
    })
    async with Client(mcp) as c:
        result = await c.call_tool(
            "web_search",
            {"query": "how do I write async code in Rust as a beginner with tokio 2026"},
        )
        sc = result.structured_content
        assert sc["results"] == []
        assert sc["note"] == EMPTY_SEARCH_NOTE


async def test_second_call_within_ttl_serves_from_cache(fake_firecrawl):
    fake_firecrawl.set_search("cached query", {
        "results": [
            {"title": "Cached", "url": "https://example.com/c",
             "description": "d", "markdown": "Body",
             "markdown_chars": 4, "markdown_truncated_chars": 4, "truncated": False},
        ],
        "credits_used_this_call": 1,
        "web_raw_count": 1,
    })
    async with Client(mcp) as c:
        r1 = await c.call_tool("web_search", {"query": "cached query"})
        r2 = await c.call_tool("web_search", {"query": "cached query"})
        assert r1.structured_content["cache"] == "miss"
        assert r2.structured_content["cache"] == "hit"
        assert fake_firecrawl.search_calls == ["cached query"]
        # Cache hits must still include the credits snapshot and the note
        # field so the model can see its budget and react to empty results
        # on a cache hit the same way as on a fresh call. (Regression test
        # for the bug where cache hits dropped these fields.)
        r2_sc = r2.structured_content
        assert "credits" in r2_sc
        assert r2_sc["credits"]["remaining_credits"] is not None
        assert "note" in r2_sc


async def test_scrape_url_cache_hit_includes_credits_snapshot(fake_firecrawl):
    fake_firecrawl.set_scrape("https://example.com/cached", {
        "markdown": "Body", "markdown_chars": 4, "markdown_truncated_chars": 4,
        "truncated": False, "title": "Cached",
        "description": "", "language": "",
        "source_url": "https://example.com/cached",
        "final_url": "https://example.com/cached",
        "status_code": 200,
        "metadata": {"title": "Cached", "description": "", "language": "",
                     "source_url": "https://example.com/cached",
                     "url": "https://example.com/cached", "status_code": 200},
        "credits_used_this_call": 1,
    })
    async with Client(mcp) as c:
        r1 = await c.call_tool("scrape_url", {"url": "https://example.com/cached"})
        r2 = await c.call_tool("scrape_url", {"url": "https://example.com/cached"})
        assert r1.structured_content["cache"] == "miss"
        assert r2.structured_content["cache"] == "hit"
        assert "credits" in r2.structured_content


async def test_force_refresh_bypasses_cache(fake_firecrawl):
    fake_firecrawl.set_search("fresh", {
        "results": [
            {"title": "R1", "url": "https://example.com/a",
             "description": "d", "markdown": "m1",
             "markdown_chars": 2, "markdown_truncated_chars": 2, "truncated": False}
        ],
        "credits_used_this_call": 1, "web_raw_count": 1,
    })
    async with Client(mcp) as c:
        r1 = await c.call_tool("web_search", {"query": "fresh"})
        r2 = await c.call_tool("web_search", {"query": "fresh", "force_refresh": True})
        assert r1.structured_content["cache"] == "miss"
        assert r2.structured_content["cache"] == "bypassed"
        assert fake_firecrawl.search_calls == ["fresh", "fresh"]


async def test_search_invalid_query_returns_empty_with_note(fake_firecrawl):
    async with Client(mcp) as c:
        result = await c.call_tool("web_search", {"query": "   "})
        sc = result.structured_content
        assert sc["query"] == ""
        assert sc["results"] == []
        assert sc["note"] == EMPTY_SEARCH_NOTE
        assert sc["credits_used_this_call"] == 0


async def test_search_missing_api_key_returns_tool_error(fake_firecrawl, monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECLAW_API_KEY", raising=False)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "FIRECRAWL_API_KEY" in str(ei.value)


async def test_search_auth_error_returns_tool_error(fake_firecrawl):
    from fireclaw_search_mcp.firecrawl import FirecrawlAuthError
    fake_firecrawl.set_search_error("anything", FirecrawlAuthError("401"))
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "rejected" in str(ei.value).lower()


async def test_search_credits_error_returns_tool_error(fake_firecrawl):
    from fireclaw_search_mcp.firecrawl import FirecrawlCreditsError
    fake_firecrawl.set_search_error("anything", FirecrawlCreditsError("402"))
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "out of credits" in str(ei.value).lower()


async def test_search_rate_limit_error_returns_tool_error(fake_firecrawl):
    from fireclaw_search_mcp.firecrawl import FirecrawlRateLimitError
    fake_firecrawl.set_search_error("anything", FirecrawlRateLimitError("429"))
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "rate" in str(ei.value).lower()


async def test_search_generic_error_returns_tool_error(fake_firecrawl):
    from fireclaw_search_mcp.firecrawl import FirecrawlError
    fake_firecrawl.set_search_error("anything", FirecrawlError("boom"))
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "upstream error" in str(ei.value).lower()


async def test_credit_guard_refuses_call_below_threshold(fake_firecrawl, monkeypatch):
    """Set `FIRECRAWL_MIN_CREDITS` above the remaining balance; the next call refuses."""
    monkeypatch.setenv("FIRECRAWL_MIN_CREDITS", "4")
    fake_firecrawl.set_credit_usage({
        "remaining_credits": 3,
        "plan_credits": 1000,
        "billing_period_start": "2026-10-01T00:00:00Z",
        "billing_period_end": "2026-11-01T00:00:00Z",
    })
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "3 credits" in str(ei.value)
        assert "minimum is 4" in str(ei.value)


async def test_scrape_url_basic(fake_firecrawl):
    fake_firecrawl.set_scrape("https://example.com/docs", {
        "markdown": "Body", "markdown_chars": 4, "markdown_truncated_chars": 4,
        "truncated": False, "title": "Example Docs",
        "description": "An example", "language": "en",
        "source_url": "https://example.com/docs",
        "final_url": "https://example.com/canonical",
        "status_code": 200,
        "metadata": {
            "title": "Example Docs", "description": "An example",
            "language": "en", "source_url": "https://example.com/docs",
            "url": "https://example.com/canonical", "status_code": 200,
        },
        "credits_used_this_call": 1,
    })
    async with Client(mcp) as c:
        result = await c.call_tool("scrape_url", {"url": "https://example.com/docs"})
        sc = result.structured_content
        assert sc["url"] == "https://example.com/docs"
        assert sc["final_url"] == "https://example.com/canonical"
        assert sc["markdown"] == "Body"
        assert sc["metadata"]["title"] == "Example Docs"
        assert sc["cache"] == "miss"
        assert sc["credits_used_this_call"] == 1


async def test_scrape_url_invalid_url_rejected(fake_firecrawl):
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("scrape_url", {"url": "not-a-url"})
        assert "http" in str(ei.value).lower()


async def test_scrape_url_second_call_hits_cache(fake_firecrawl):
    fake_firecrawl.set_scrape("https://example.com/cached", {
        "markdown": "Body", "markdown_chars": 4, "markdown_truncated_chars": 4,
        "truncated": False, "title": "Cached",
        "description": "", "language": "",
        "source_url": "https://example.com/cached",
        "final_url": "https://example.com/cached",
        "status_code": 200,
        "metadata": {"title": "Cached", "description": "", "language": "",
                     "source_url": "https://example.com/cached",
                     "url": "https://example.com/cached", "status_code": 200},
        "credits_used_this_call": 1,
    })
    async with Client(mcp) as c:
        r1 = await c.call_tool("scrape_url", {"url": "https://example.com/cached"})
        r2 = await c.call_tool("scrape_url", {"url": "https://example.com/cached"})
        assert r1.structured_content["cache"] == "miss"
        assert r2.structured_content["cache"] == "hit"


async def test_scrape_url_missing_api_key_returns_tool_error(fake_firecrawl, monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECLAW_API_KEY", raising=False)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("scrape_url", {"url": "https://example.com/x"})
        assert "FIRECRAWL_API_KEY" in str(ei.value)


async def test_credit_status_returns_snapshot(fake_firecrawl):
    fake_firecrawl.set_credit_usage({
        "remaining_credits": 812,
        "plan_credits": 1000,
        "billing_period_start": "2026-10-01T00:00:00Z",
        "billing_period_end": "2026-11-01T00:00:00Z",
    })
    async with Client(mcp) as c:
        result = await c.call_tool("credit_status", {})
        sc = result.structured_content
        assert sc["source"] == "live"
        assert sc["credits"]["remaining_credits"] == 812
        assert sc["credits"]["plan_credits"] == 1000


async def test_credit_status_falls_back_when_probe_fails(fake_firecrawl):
    from fireclaw_search_mcp.firecrawl import FirecrawlError
    fake_firecrawl.set_credit_usage_error(FirecrawlError("network blip"))
    async with Client(mcp) as c:
        result = await c.call_tool("credit_status", {})
        sc = result.structured_content
        assert sc["source"] == "cached"
        assert "live credit probe failed" in sc["note"]


async def test_credit_status_no_key(fake_firecrawl, monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECLAW_API_KEY", raising=False)
    async with Client(mcp) as c:
        result = await c.call_tool("credit_status", {})
        sc = result.structured_content
        assert sc["source"] == "cached"
        assert "FIRECRAWL_API_KEY" in sc["note"]


async def test_modern_protocol_negotiated(fake_firecrawl):
    async with Client(mcp) as c:
        assert c.protocol_version == "2026-07-28"


async def test_legacy_era_also_calls_the_tool(fake_firecrawl):
    fake_firecrawl.set_search("Rust", {
        "results": [], "credits_used_this_call": 0, "web_raw_count": 0,
    })
    async with Client(mcp, mode="legacy") as c:
        result = await c.call_tool("web_search", {"query": "Rust", "max_results": 3})
        sc = result.structured_content
        assert sc["query"] == "Rust"
        assert sc["note"] == EMPTY_SEARCH_NOTE


async def test_legacy_era_lists_tools(fake_firecrawl):
    async with Client(mcp, mode="legacy") as c:
        tools = await c.list_tools()
        names = sorted(t.name for t in tools)
        assert names == ["credit_status", "scrape_url", "web_search"]
