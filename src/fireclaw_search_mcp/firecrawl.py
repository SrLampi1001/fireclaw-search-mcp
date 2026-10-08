"""Firecrawl v2 HTTP client and response mapping.

Per SPECS §4: a thin async httpx wrapper around /v2/search, /v2/scrape, and
/v2/team/credit-usage, with bearer auth via the FIRECRAWL_API_KEY env var.
The mapping layer projects Firecrawl's raw JSON onto flat shapes the tools
return via `structured_content`. Per-result markdown truncation is handled
here so each `web_search` call materialises with `markdown_chars` /
`markdown_truncated_chars` already populated.

The server deliberately uses raw httpx instead of the official SDK so we can
keep the dependency surface to one (httpx already pulled in for FastMCP's
async runtime) and so the response mapping is fully testable without SDK
mocking. Behaviour matches the v2 API spec at docs.firecrawl.dev.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.firecrawl.dev/v2"
USER_AGENT = "fireclaw-search-mcp/0.1"


class FirecrawlError(RuntimeError):
    """Raised on a Firecrawl transport or non-2xx HTTP error."""


class FirecrawlAuthError(FirecrawlError):
    """Raised when Firecrawl rejects the API key (HTTP 401)."""


class FirecrawlCreditsError(FirecrawlError):
    """Raised when Firecrawl reports out-of-credits (HTTP 402)."""


class FirecrawlRateLimitError(FirecrawlError):
    """Raised when Firecrawl rate-limits (HTTP 429)."""


class FirecrawlClient:
    """Async HTTP client for the three Firecrawl endpoints this server uses."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 60.0,
        max_markdown_chars: int = 8000,
    ) -> None:
        if not api_key:
            raise FirecrawlError("FIRECRAWL_API_KEY is not set; tools will refuse to call Firecrawl")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_markdown_chars = max_markdown_chars
        self._client: httpx.AsyncClient | None = None

    def attach(self, client: httpx.AsyncClient) -> None:
        """Bind a shared httpx client; lifespan-managed."""
        self._client = client

    async def aclose(self) -> None:
        # No-op; the shared client is owned by the lifespan.
        self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise FirecrawlError("Firecrawl client not attached to a shared httpx session")
        return self._client

    async def search(
        self,
        query: str,
        limit: int = 5,
        *,
        include_markdown: bool = True,
    ) -> dict[str, Any]:
        """Run Firecrawl /v2/search. `include_markdown=False` returns web entries
        without scrape (saves credits; description/snippet only)."""
        body: dict[str, Any] = {
            "query": query,
            "limit": max(1, min(int(limit), 10)),
        }
        if include_markdown:
            body["scrapeOptions"] = {"formats": ["markdown"], "onlyMainContent": True}
        payload = await self._post("/search", body)
        return _map_search_response(payload, self._max_markdown_chars)

    async def scrape(self, url: str) -> dict[str, Any]:
        """Run Firecrawl /v2/scrape on a single URL. Returns the mapped response
        with markdown truncated and metadata projected onto the SPECS §3.2 shape."""
        body = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
        }
        payload = await self._post("/scrape", body)
        return _map_scrape_response(payload, self._max_markdown_chars)

    async def credit_usage(self) -> dict[str, Any]:
        """Run Firecrawl /v2/team/credit-usage. Maps to the SPECS §3.3 credits object."""
        payload = await self._get("/team/credit-usage")
        return _map_credit_usage_response(payload)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._http.post(
                self._base_url + path,
                json=body,
                headers=self._headers(),
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as e:
            raise FirecrawlError(f"Firecrawl transport error: {e.__class__.__name__}: {e}") from e
        return _parse_response(resp)

    async def _get(self, path: str) -> dict[str, Any]:
        try:
            resp = await self._http.get(
                self._base_url + path,
                headers=self._headers(),
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as e:
            raise FirecrawlError(f"Firecrawl transport error: {e.__class__.__name__}: {e}") from e
        return _parse_response(resp)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


# ----------------------------------------------------------------------- I/O --


def _parse_response(resp: httpx.Response) -> dict[str, Any]:
    """Validate an HTTP response and return the JSON body. Raises typed errors."""
    if resp.status_code == 401:
        raise FirecrawlAuthError("Firecrawl rejected the API key (401 Unauthorized)")
    if resp.status_code == 402:
        # Firecrawl signals out-of-credits with 402 (also `insufficient_credits` in body).
        raise FirecrawlCreditsError("Firecrawl is out of credits for this billing period (402)")
    if resp.status_code == 429:
        raise FirecrawlRateLimitError("Firecrawl rate-limited this key (429 Too Many Requests)")
    if resp.status_code >= 400:
        text = (resp.text or "")[:200]
        raise FirecrawlError(f"Firecrawl upstream HTTP {resp.status_code}: {text!r}")
    try:
        body = resp.json()
    except ValueError as e:
        raise FirecrawlError(f"Firecrawl returned non-JSON body (status {resp.status_code}): {e}") from e
    if isinstance(body, dict) and body.get("success") is False:
        err = body.get("error") or body.get("code") or "unknown"
        if body.get("code") == "insufficient_credits" or "402" in str(err):
            raise FirecrawlCreditsError(f"Firecrawl out of credits: {err}")
        raise FirecrawlError(f"Firecrawl reported failure: {err}")
    return body


# ------------------------------------------------------------------ mappings --


def _map_search_response(payload: dict[str, Any], max_markdown_chars: int) -> dict[str, Any]:
    """Project /v2/search payload onto the SPECS §3.1 shape."""
    data = payload.get("data") or {}
    web = data.get("web") or []
    results: list[dict[str, Any]] = []
    for entry in web:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            # Skip search-engine noise — entries with no resolved URL.
            continue
        markdown = entry.get("markdown") or ""
        markdown_chars = len(markdown)
        truncated = markdown_chars > max_markdown_chars
        if truncated:
            markdown = _truncate_markdown(markdown, max_markdown_chars)
        results.append(
            {
                "title": entry.get("title") or "",
                "url": url,
                "description": entry.get("description") or "",
                "markdown": markdown,
                "markdown_chars": markdown_chars,
                "markdown_truncated_chars": len(markdown) if truncated else markdown_chars,
                "truncated": truncated,
            }
        )
    credits_used = _coerce_int(payload.get("creditsUsed")) or _sum_provider_costs(web)
    return {
        "results": results,
        "credits_used_this_call": credits_used,
        "web_raw_count": len(web),
    }


def _map_scrape_response(payload: dict[str, Any], max_markdown_chars: int) -> dict[str, Any]:
    """Project /v2/scrape payload onto the SPECS §3.2 shape."""
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    raw_markdown = data.get("markdown") or ""
    markdown_chars = len(raw_markdown)
    truncated = markdown_chars > max_markdown_chars
    if truncated:
        markdown = _truncate_markdown(raw_markdown, max_markdown_chars)
    else:
        markdown = raw_markdown
    metadata = data.get("metadata") or {}
    projected_metadata = {
        "title": _first_str(metadata.get("title")),
        "description": _first_str(metadata.get("description")),
        "language": _first_str(metadata.get("language")),
        "source_url": metadata.get("sourceURL") or metadata.get("source_url") or "",
        "url": metadata.get("url") or "",
        "status_code": metadata.get("statusCode") or metadata.get("status_code"),
    }
    credits_used = _coerce_int(payload.get("creditsUsed"))
    if credits_used is None:
        provider_cost = (metadata.get("provider") or {}).get("creditsCost")
        credits_used = _coerce_int(provider_cost) or 1
    return {
        "markdown": markdown,
        "markdown_chars": markdown_chars,
        "markdown_truncated_chars": len(markdown) if truncated else markdown_chars,
        "truncated": truncated,
        "title": projected_metadata["title"],
        "description": projected_metadata["description"],
        "language": projected_metadata["language"],
        "source_url": projected_metadata["source_url"],
        "final_url": projected_metadata["url"],
        "status_code": projected_metadata["status_code"],
        "metadata": projected_metadata,
        "credits_used_this_call": credits_used,
    }


def _map_credit_usage_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Project /v2/team/credit-usage payload onto the SPECS §3.3 credits object."""
    data = payload.get("data") or {}
    return {
        "remaining_credits": _coerce_int(data.get("remainingCredits")),
        "plan_credits": _coerce_int(data.get("planCredits")),
        "billing_period_start": data.get("billingPeriodStart"),
        "billing_period_end": data.get("billingPeriodEnd"),
        "raw": data,
    }


# ----------------------------------------------------------------- helpers --


_TRUNCATION_MARKER = "\n\n[…markdown truncated to fit the context budget; ask for a narrower scope or call scrape_url with a focus…]"


def _truncate_markdown(markdown: str, max_chars: int) -> str:
    """Slice to leave room for the marker, then append it. Total stays <= max_chars.

    Python `str` slicing is char-safe so we never need to deal with partial
    multi-byte sequences here. The marker is short and stable so the model
    can pattern-match on it.
    """
    room = max_chars - len(_TRUNCATION_MARKER)
    if room <= 0:
        # Defensive: the configured cap is too small for the marker itself.
        # Truncate to the cap so we still produce a bounded body.
        return markdown[:max_chars]
    return markdown[:room] + _TRUNCATION_MARKER


def _sum_provider_costs(web: Iterable[Any]) -> int:
    total = 0
    for entry in web:
        if not isinstance(entry, dict):
            continue
        cost = (entry.get("metadata") or {}).get("provider", {}).get("creditsCost")
        cost = _coerce_int(cost)
        if cost:
            total += cost
    return total


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_str(value: Any) -> str:
    """Metadata `title`/`description`/`language` may come as str or list[str];
    flatten to a single string for the model-facing response."""
    if isinstance(value, list):
        return next((str(v) for v in value if v), "")
    if value is None:
        return ""
    return str(value)
