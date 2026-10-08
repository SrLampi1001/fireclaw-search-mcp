"""FastMCP server definition: tools + prompts.

Three tools (`web_search`, `scrape_url`, `credit_status`) over the Firecrawl
v2 API, plus two prompts (`research_topic`, `fetch_document`). Built on
FastMCP 4.0.11 against MCP 2026-07-28. Dual-era; serves both Streamable HTTP
and stdio. Application state (httpx client, Firecrawl client, on-disk cache,
rate limiter, credit tracker) is owned by the FastMCP `lifespan`, so it's
safely constructed at startup and closed at shutdown, and the tools read it
from `ctx.request_context.lifespan_context`.
"""
from __future__ import annotations

import logging
import urllib.parse
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .cache import DiskCache
from .credits import CreditTracker, ToolBudgetError
from .firecrawl import (
    FirecrawlAuthError,
    FirecrawlClient,
    FirecrawlCreditsError,
    FirecrawlError,
    FirecrawlRateLimitError,
)
from .prompts import register_prompts
from .rate_limit import MinIntervalLimiter
from .settings import Settings, clamp_count, configure_logging

log = logging.getLogger(__name__)


INSTRUCTIONS = (
    "Three tools: web_search (deep Firecrawl search returning full page "
    "markdown), scrape_url (single-URL Firecrawl scrape), credit_status "
    "(reads the team's Firecrawl credit balance without burning credits). "
    "Two prompts: research_topic (plans one web_search), fetch_document "
    "(plans one scrape_url). web_search and scrape_url cost Firecrawl "
    "credits; every response includes a `credits` snapshot. Check "
    "`remaining_credits`; if it drops below ~50, the model should switch to "
    "a free sibling search tool. Empty web_search results are normal for "
    "long-tail queries — the tool returns a guidance note instead. Per-"
    "page markdown is capped at ~8k chars; the cut is visible via "
    "`markdown_chars` / `markdown_truncated_chars`."
)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncGenerator[dict, None]:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    cache = DiskCache(settings.cache_dir, settings.cache_ttl_secs)
    rate_limiter = MinIntervalLimiter(settings.min_interval_ms)
    credits = CreditTracker()
    firecrawl: FirecrawlClient | None = None
    if settings.api_key_set:
        firecrawl = FirecrawlClient(
            api_key=settings.api_key,
            base_url=settings.firecrawl_base_url,
            timeout_s=settings.http_timeout_s,
            max_markdown_chars=settings.max_markdown_chars,
        )
        # Best-effort startup probe. Failures log a warning and leave the
        # tracker in the "credits unknown" state; the first real call still
        # proceeds without a pre-check.
        async with httpx.AsyncClient() as probe_client:
            firecrawl.attach(probe_client)
            try:
                snapshot = await firecrawl.credit_usage()
                credits.hydrate(snapshot)
                log.info(
                    "firecrawl credits: remaining=%s plan=%s resets=%s",
                    credits.remaining_credits,
                    credits.plan_credits,
                    credits.billing_period_end,
                )
            except FirecrawlAuthError as e:
                log.warning("firecrawl startup probe: %s", e)
                credits.mark_probe_failed("auth rejected at startup")
            except FirecrawlCreditsError as e:
                log.warning("firecrawl startup probe: %s", e)
                credits.mark_probe_failed("out of credits at startup")
            except FirecrawlError as e:
                log.warning("firecrawl startup probe failed: %s", e)
                credits.mark_probe_failed(str(e))
    else:
        log.warning("FIRECRAWL_API_KEY is not set; tools will return a clear error on first call")
        credits.mark_probe_failed("FIRECRAWL_API_KEY is not set")

    async with httpx.AsyncClient(
        headers={"User-Agent": "fireclaw-search-mcp/0.1 (+https://github.com/)"},
        follow_redirects=True,
    ) as http_client:
        if firecrawl is not None:
            firecrawl.attach(http_client)
        yield {
            "settings": settings,
            "cache": cache,
            "rate_limiter": rate_limiter,
            "credits": credits,
            "firecrawl": firecrawl,
            "http_client": http_client,
        }


mcp = FastMCP(
    name="fireclaw-search-mcp",
    instructions=INSTRUCTIONS,
    cache_ttl=300,        # tool/prompt lists rarely change; safe to share publicly
    cache_scope="public",
    lifespan=lifespan,
)

register_prompts(mcp)


# ---------------------------------------------------------------------- tools --


EMPTY_SEARCH_NOTE = (
    "Firecrawl returned no web results for this query. Rephrase once as a "
    "specific topic name with concrete terms (e.g. 'FastMCP lifespan context' "
    "instead of 'how do I keep state in MCP tools'), and if that also comes "
    "back empty, answer from your own knowledge and say so."
)

EMPTY_SCRAPE_NOTE = (
    "Firecrawl returned no markdown body for this URL. The page may be "
    "empty, paywalled, or behind a JS shell. Try a different URL, or "
    "summarise whatever `metadata.title` and `metadata.description` carry."
)


@mcp.tool(
    name="web_search",
    description=(
        "Deep web search via Firecrawl. Reach for this when the question is "
        "complex, long-tail, or you genuinely need the full page contents as "
        "clean markdown — not just snippets. Costs Firecrawl credits (the "
        "free tier is 1,000/month; a typical call uses 2–7 credits). Every "
        "response includes a `credits` snapshot — check `remaining_credits` "
        "and switch to a free search tool once it drops under ~50. Per-"
        "result markdown is capped at ~8k chars to protect context; the "
        "`markdown_chars` / `markdown_truncated_chars` fields tell you how "
        "much was cut. Pass `force_refresh=true` when the user explicitly "
        "wants fresh results or you know a previous result is stale."
    ),
)
async def web_search(
    query: str = Field(
        description=(
            "The search query. Concrete topic names work best. The query is "
            "trimmed and lowercased before any cache lookup or upstream call."
        ),
        min_length=1,
    ),
    max_results: int = Field(
        default=5,
        description="Max results to return. Clamped to [1, 10] server-side.",
    ),
    force_refresh: bool = Field(
        default=False,
        description=(
            "Bypass the on-disk cache and always re-query Firecrawl even "
            "when a fresh-enough entry exists. Use when the user explicitly "
            "asks for fresh results or you know a previous result is stale."
        ),
    ),
    ctx: Context | None = None,
) -> dict:
    """Deep web search via Firecrawl and return structured results."""
    state = _state_from(ctx)
    settings: Settings = state["settings"]
    cache: DiskCache = state["cache"]
    rate_limiter: MinIntervalLimiter = state["rate_limiter"]
    credits: CreditTracker = state["credits"]
    firecrawl: FirecrawlClient | None = state["firecrawl"]

    echoed = query.strip()
    if not echoed:
        return {
            "query": "",
            "results": [],
            "credits_used_this_call": 0,
            "credits": credits.snapshot(source="cached"),
            "cache": "miss",
            "note": EMPTY_SEARCH_NOTE,
        }
    n = clamp_count(max_results)

    # 1. Cache lookup — the polite path, no network, no credit burn.
    if not force_refresh:
        cached = cache.get(echoed)
        if cached is not None and cached.kind == "search":
            cached_results = cached.response.get("results", [])[:n]
            cached_note = cached.response.get("note", "")
            return {
                "query": echoed,
                "results": cached_results,
                "credits_used_this_call": 0,
                "credits": credits.snapshot(source="cached"),
                "cache": "hit",
                "note": cached_note,
            }

    # 2. Bail early if the Firecrawl client is not configured.
    if firecrawl is None:
        raise ToolError(
            "FIRECRAWL_API_KEY is not set. Set it in .env (or pass "
            "FIRECLAW_API_KEY as the legacy alias) and restart the server, "
            "or use a free sibling search tool (e.g. duck-search-mcp)."
        )

    # 3. Pre-call credit guard. Raises ToolBudgetError -> ToolError if too low.
    try:
        credits.guard(settings.min_credits)
    except ToolBudgetError as e:
        raise ToolError(str(e)) from e

    # 4. Pace upstream calls.
    await rate_limiter.wait()

    # 5. Hit Firecrawl.
    try:
        upstream = await firecrawl.search(query=echoed, limit=n)
    except FirecrawlAuthError as e:
        raise ToolError(
            "Firecrawl rejected the API key. Check FIRECRAWL_API_KEY is set "
            "correctly and the key has not been revoked."
        ) from e
    except FirecrawlCreditsError as e:
        raise ToolError(
            "Firecrawl is out of credits for this billing period. Switch to "
            "a free sibling search tool, or wait for the next cycle."
        ) from e
    except FirecrawlRateLimitError as e:
        raise ToolError(
            "Firecrawl is rate-limiting this key. Wait a few seconds and "
            "try again, or fall back to a free sibling search tool."
        ) from e
    except FirecrawlError as e:
        raise ToolError(f"web_search upstream error: {e}") from e

    results = upstream["results"][:n]
    credits_used = int(upstream.get("credits_used_this_call") or 0)
    credits.charge(credits_used)

    note = "" if results else EMPTY_SEARCH_NOTE
    response = {
        "query": echoed,
        "results": results,
        "credits_used_this_call": credits_used,
        "credits": credits.snapshot(source="cached"),
        "cache": "bypassed" if force_refresh else "miss",
        "note": note,
    }
    # Cache hits are served without re-querying; misses and bypasses are
    # cached so repeat calls within the TTL don't burn credits. The credit
    # snapshot is excluded from the cached payload so every call sees a
    # fresh view.
    cache_response = {
        "results": upstream["results"],
        "note": note,
    }
    cache.set(echoed, "search", {"query": echoed}, cache_response)
    return response


@mcp.tool(
    name="scrape_url",
    description=(
        "Scrape a single known URL via Firecrawl. Reach for this after a "
        "prior search pointed at the page, or when the user already gave "
        "you the URL. Costs 1+ credits (more for large / complex pages). "
        "Returns the page body as clean markdown, with `metadata` for the "
        "title, description, language, and post-redirect URL. Per-page "
        "markdown is capped at ~8k chars to protect context; the cut is "
        "visible via `markdown_chars` / `markdown_truncated_chars`. Pass "
        "`force_refresh=true` when the user explicitly wants fresh content "
        "or you know the page has changed since the cache was populated."
    ),
)
async def scrape_url(
    url: str = Field(
        description=(
            "The URL to scrape. Must be a syntactically valid http(s) URL. "
            "The cache key is the URL itself; redirects are followed "
            "internally and the final URL is exposed as `metadata.url`."
        ),
        min_length=1,
    ),
    force_refresh: bool = Field(
        default=False,
        description=(
            "Bypass the on-disk cache and always re-query Firecrawl even "
            "when a fresh-enough entry exists."
        ),
    ),
    ctx: Context | None = None,
) -> dict:
    """Scrape a single URL via Firecrawl and return the page body + metadata."""
    state = _state_from(ctx)
    settings: Settings = state["settings"]
    cache: DiskCache = state["cache"]
    rate_limiter: MinIntervalLimiter = state["rate_limiter"]
    credits: CreditTracker = state["credits"]
    firecrawl: FirecrawlClient | None = state["firecrawl"]

    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolError(f"scrape_url requires an http(s) URL; got {url!r}")

    if not force_refresh:
        cached = cache.get(url.strip())
        if cached is not None and cached.kind == "scrape":
            body = cached.response
            return {
                "url": url.strip(),
                "final_url": body.get("final_url") or url.strip(),
                "title": body.get("title") or "",
                "description": body.get("description") or "",
                "language": body.get("language") or "",
                "markdown": body.get("markdown") or "",
                "markdown_chars": int(body.get("markdown_chars") or 0),
                "markdown_truncated_chars": int(body.get("markdown_truncated_chars") or 0),
                "truncated": bool(body.get("truncated")),
                "metadata": body.get("metadata") or {},
                "status_code": body.get("status_code"),
                "credits_used_this_call": 0,
                "credits": credits.snapshot(source="cached"),
                "cache": "hit",
                "note": body.get("note", ""),
            }

    if firecrawl is None:
        raise ToolError(
            "FIRECRAWL_API_KEY is not set. Set it in .env (or pass "
            "FIRECLAW_API_KEY as the legacy alias) and restart the server."
        )

    # scrape_url does NOT pre-check the credit counter — a single scrape is
    # only 1 credit and the operator opted into scraping by explicitly
    # asking for a URL. credits_used from the upstream response still
    # decrements the counter after the call.
    await rate_limiter.wait()

    try:
        upstream = await firecrawl.scrape(url=url.strip())
    except FirecrawlAuthError as e:
        raise ToolError(
            "Firecrawl rejected the API key. Check FIRECRAWL_API_KEY is set "
            "correctly and the key has not been revoked."
        ) from e
    except FirecrawlCreditsError as e:
        raise ToolError(
            "Firecrawl is out of credits for this billing period. Wait "
            "for the next cycle or top up the team."
        ) from e
    except FirecrawlRateLimitError as e:
        raise ToolError(
            "Firecrawl is rate-limiting this key. Wait a few seconds and "
            "try again."
        ) from e
    except FirecrawlError as e:
        raise ToolError(f"scrape_url upstream error: {e}") from e

    credits_used = int(upstream.get("credits_used_this_call") or 0)
    credits.charge(credits_used)

    note = EMPTY_SCRAPE_NOTE if not upstream.get("markdown") else ""
    response = {
        "url": url.strip(),
        "final_url": upstream.get("final_url") or url.strip(),
        "title": upstream.get("title") or "",
        "description": upstream.get("description") or "",
        "language": upstream.get("language") or "",
        "markdown": upstream.get("markdown") or "",
        "markdown_chars": int(upstream.get("markdown_chars") or 0),
        "markdown_truncated_chars": int(upstream.get("markdown_truncated_chars") or 0),
        "truncated": bool(upstream.get("truncated")),
        "metadata": upstream.get("metadata") or {},
        "status_code": upstream.get("status_code"),
        "credits_used_this_call": credits_used,
        "credits": credits.snapshot(source="cached"),
        "cache": "bypassed" if force_refresh else "miss",
        "note": note,
    }
    cache_response = {
        "final_url": response["final_url"],
        "title": response["title"],
        "description": response["description"],
        "language": response["language"],
        "markdown": upstream.get("markdown") or "",   # store the full, untruncated body
        "markdown_chars": response["markdown_chars"],
        "markdown_truncated_chars": int(upstream.get("markdown_truncated_chars") or 0),
        "truncated": response["truncated"],
        "metadata": response["metadata"],
        "status_code": response["status_code"],
        "note": note,
    }
    cache.set(url.strip(), "scrape", {"url": url.strip()}, cache_response)
    return response


@mcp.tool(
    name="credit_status",
    description=(
        "Read the current Firecrawl credit balance for the configured key. "
        "Does NOT burn credits — calls `/v2/team/credit-usage` (a metadata "
        "endpoint) and falls back to the in-memory counter on transient "
        "errors. Use this to check the budget before issuing an expensive "
        "batch of calls."
    ),
)
async def credit_status(ctx: Context | None = None) -> dict:
    """Return the current Firecrawl credit snapshot."""
    state = _state_from(ctx)
    firecrawl: FirecrawlClient | None = state["firecrawl"]
    credits: CreditTracker = state["credits"]

    if firecrawl is None:
        return {
            "credits": credits.snapshot(source="cached"),
            "source": "cached",
            "note": "FIRECRAWL_API_KEY is not set; no credit snapshot is available.",
        }

    try:
        snapshot = await firecrawl.credit_usage()
        credits.hydrate(snapshot)
        return {
            "credits": credits.snapshot(source="live"),
            "source": "live",
            "note": "",
        }
    except FirecrawlError as e:
        return {
            "credits": credits.snapshot(source="cached"),
            "source": "cached",
            "note": f"live credit probe failed ({e}); returning the in-memory snapshot.",
        }


# -------------------------------------------------------------------- helpers --


def _state_from(ctx: Context | None) -> dict:
    if ctx is None or ctx.request_context is None:
        raise RuntimeError("tool called without a request context")
    lifespan_state = ctx.request_context.lifespan_context
    if not lifespan_state:
        raise RuntimeError("server lifespan did not initialise state")
    return lifespan_state
