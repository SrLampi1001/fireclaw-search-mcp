# `fireclaw-search-mcp` — MCP server

> **Status:** v0.1 implemented. Speaks MCP 2026-07-28 over Streamable HTTP and stdio, built on FastMCP 4.0.11. The behaviour described below is the contract this server commits to.

A [Model Context Protocol](https://modelcontextprotocol.io/specification/2026-07-28) server that exposes the Firecrawl `/v2/search`, `/v2/scrape`, and `/v2/team/credit-usage` endpoints as MCP tools for any modern AI agent. No agent-specific integration; runs as a standalone streamable HTTP server that any modern MCP client can connect to.

| Tool | Backend | Best for |
| --- | --- | --- |
| `web_search` | Firecrawl `/v2/search` with `scrape_options.formats=["markdown"]` | Complex, long-tail, or question-shaped queries that need the **full page contents** as clean markdown. Costs Firecrawl credits. |
| `scrape_url` | Firecrawl `/v2/scrape` with `formats=["markdown"]` | Fetching a known single URL (a documentation page, an issue, an API reference) the model already has a pointer to. Costs 1+ credits. |
| `credit_status` | Firecrawl `/v2/team/credit-usage` | Reading the current credit balance without burning any. |

| Prompt | Use |
| --- | --- |
| `research_topic` | Plan a single `web_search` call, including the empty-result fallback. |
| `fetch_document` | Plan a single `scrape_url` call, telling the model to focus on a specific question. |

---

## 1. Why this server exists

Firecrawl is the **deep** path against the free-tier budget (1,000 credits/month on the default plan). Where a free snippet-only tool (e.g. `duck-search-mcp`) is fine for entity-like queries, `web_search` returns each result's full page body as clean markdown — useful for long-tail, question-shaped, or otherwise complex queries where snippets aren't enough. `scrape_url` covers the "I already have a URL and just need the page" case. `credit_status` lets the model peek at its budget without burning anything.

Every `web_search` and `scrape_url` response includes a `credits` snapshot (`remaining_credits`, `plan_credits`, `billing_period_end`) so the calling agent can see its balance drop across calls and decide to switch to a free tool when it gets low. The minimum-credits guard refuses calls that would drop below the configured threshold, surfacing a clear error so the model can react.

This server does not try to replace general free search. For entity-like queries, prefer a sibling MCP that hits a free endpoint (e.g. `duck-search-mcp`). For everything that genuinely needs full page contents, reach for `web_search` here.

---

## 2. Protocol contract

The server is built on **FastMCP 4.0.11** and targets the **MCP 2026-07-28** revision. It is dual-era out of the box — modern clients negotiate the stateless protocol, legacy clients that send `initialize` still get a working session.

| Contract | Value |
| --- | --- |
| Protocol revision | `2026-07-28` (advertised via `server/discover`; legacy `initialize` negotiates `2025-11-25`) |
| Transports | Streamable HTTP at `/mcp` (default), stdio (opt-in via `FIRECRAWL_TRANSPORT=stdio`) |
| Bind address | `127.0.0.1` (loopback only; expose via VS Code port forwarding or ngrok — see `docs/DEPLOYMENT.md`) |
| Authentication | **None.** Loopback binding is the only access control. Operate behind a trusted tunnel. |
| Required response fields | `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` headers; tool list advertises `ttlMs`/`cacheScope` |
| Server features | `tools` (three), `prompts` (two), `resources/templates` (none), `extensions: io.modelcontextprotocol/ui` |

Any modern MCP client (Claude Desktop, Cursor, opencode, VS Code Copilot Chat) that supports Streamable HTTP can connect to the forwarded URL without code changes.

---

## 3. Tools

### 3.1 `web_search`

**Input schema** (JSON Schema, surfaced verbatim to the model):

```json
{
  "type": "object",
  "properties": {
    "query":          { "type": "string",  "description": "...",
                        "minLength": 1 },
    "max_results":    { "type": "integer", "description": "...",
                        "default": 5 },
    "force_refresh":  { "type": "boolean", "description": "...",
                        "default": false }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

| Argument | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `query` | string | yes | – | The search query. Trimmed before any cache lookup; the trimmed query is echoed in the response. |
| `max_results` | integer | no | 5 | Max results to return. **Clamped** to `[1, 10]` server-side (out-of-range values are not rejected, they are clamped). Each result is scraped for markdown, so a larger value costs more credits and dumps more into context. |
| `force_refresh` | boolean | no | false | Bypass the on-disk cache and re-query Firecrawl even when a fresh-enough entry exists. Use when the user explicitly asks for fresh results, or when a previous result is known to be stale. |

**Output** (`structured_content`, a JSON object):

```json
{
  "query": "how to configure a custom JSON logger in tokio",
  "results": [
    { "title": "...",
      "url":   "...",
      "description": "...",
      "markdown": "...",
      "markdown_chars": 12345,
      "markdown_truncated_chars": 8000,
      "truncated": true }
  ],
  "credits_used_this_call": 7,
  "credits": {
    "remaining_credits": 812,
    "plan_credits": 1000,
    "billing_period_end": "2026-11-01T00:00:00+00:00"
  },
  "cache": "miss" | "hit" | "bypassed"
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `query` | string | The query as the server received it (after `trim()`). |
| `results` | array of `{title, url, description, markdown, markdown_chars, markdown_truncated_chars, truncated}` | Mapped from Firecrawl's `data.web[]`. Capped at `max_results`. |
| `credits_used_this_call` | integer | From the Firecrawl `search` response's `creditsUsed` field. The server subtracts this from its in-memory counter. `0` on a cache hit. |
| `credits` | object | `{remaining_credits, plan_credits, billing_period_end}` — the current balance, refreshed by the startup probe and decremented by each call. |
| `cache` | `"miss"`, `"hit"`, or `"bypassed"` | Tells the model whether this call hit the cache, the network, or whether the caller opted out of the cache. |

**Per-result fields:**

| Field | Type | Notes |
| --- | --- | --- |
| `title` | string | From the Firecrawl `search` result metadata. |
| `url` | string | The canonical URL. |
| `description` | string | The search-engine description (when Firecrawl returns one and scrape options don't request a body). |
| `markdown` | string | The page body as clean markdown, **truncated to `FIRECRAWL_MAX_MARKDOWN_CHARS` (default 8000) per result**. A truncation marker is appended when truncation happened so the model can see the cut. |
| `markdown_chars` | integer | The original body length **before** truncation. Lets the model know how much was left out. |
| `markdown_truncated_chars` | integer | The cut length (≤ `markdown_chars`). Equal when no truncation happened. |
| `truncated` | boolean | `true` when the markdown was truncated. |

**Description (the model reads this verbatim):**

> Deep web search via Firecrawl. Reach for this when the question is complex, long-tail, or you genuinely need the **full page contents** as clean markdown — not just snippets. Costs Firecrawl credits (the free tier is 1,000/month; a typical call uses 2–7 credits). Every response includes a `credits` snapshot — check `remaining_credits` and switch to a free search tool once it drops under ~50. Per-result markdown is capped at ~8k chars to protect context; the `markdown_chars` / `markdown_truncated_chars` fields tell you how much was cut. Pass `force_refresh=true` when the user explicitly wants fresh results or you know a previous result is stale.

### 3.2 `scrape_url`

**Input schema** (JSON Schema, surfaced verbatim to the model):

```json
{
  "type": "object",
  "properties": {
    "url":           { "type": "string",  "description": "...",
                       "format": "uri",
                       "minLength": 1 },
    "force_refresh": { "type": "boolean", "description": "...",
                       "default": false }
  },
  "required": ["url"],
  "additionalProperties": false
}
```

| Argument | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `url` | string | yes | – | The URL to scrape. Must be a syntactically valid HTTP/HTTPS URL. The cache key is the canonical URL. |
| `force_refresh` | boolean | no | false | Bypass the on-disk cache. Use when the page is known to have changed or the user explicitly asks for fresh content. |

**Output** (`structured_content`, a JSON object):

```json
{
  "url": "https://example.com/docs",
  "final_url": "https://example.com/docs",
  "title": "Example Docs",
  "markdown": "...",
  "markdown_chars": 12345,
  "markdown_truncated_chars": 8000,
  "truncated": true,
  "metadata": {
    "title": "Example Docs",
    "description": "...",
    "language": "en",
    "source_url": "https://example.com/docs",
    "url": "https://example.com/docs",
    "status_code": 200
  },
  "credits_used_this_call": 1,
  "credits": {
    "remaining_credits": 812,
    "plan_credits": 1000,
    "billing_period_end": "2026-11-01T00:00:00+00:00"
  },
  "cache": "miss" | "hit" | "bypassed"
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `url` | string | The URL as the server received it. |
| `final_url` | string | The URL after redirects (from `metadata.url`). |
| `title` | string | The page title from `metadata.title`. |
| `markdown` | string | The page body as clean markdown. Truncated to `FIRECRAWL_MAX_MARKDOWN_CHARS` per result. |
| `markdown_chars` | integer | Original body length before truncation. |
| `markdown_truncated_chars` | integer | The cut length (≤ `markdown_chars`). Equal when no truncation happened. |
| `truncated` | boolean | `true` when the markdown was truncated. |
| `metadata` | object | The subset of Firecrawl's `metadata` the model is most likely to read: `title`, `description`, `language`, `source_url`, `url`, `status_code`. |
| `credits_used_this_call` | integer | From the Firecrawl `scrape` response's `metadata.creditsUsed` or `creditsUsed` field where exposed. `0` on a cache hit. |
| `credits` | object | Live credit snapshot, same shape as `web_search`. |
| `cache` | `"miss"`, `"hit"`, or `"bypassed"` | Tells the model whether the call hit the cache, the network, or whether the caller opted out. |

**Description (the model reads this verbatim):**

> Scrape a single known URL via Firecrawl. Reach for this after a prior search pointed at the page, or when the user already gave you the URL. Costs 1+ credits (more for large / complex pages). Returns the page body as clean markdown, with `metadata` for the title, description, language, and post-redirect URL. Per-page markdown is capped at ~8k chars to protect context; the `markdown_chars` / `markdown_truncated_chars` fields tell you how much was cut. Pass `force_refresh=true` when the user explicitly wants fresh content or you know the page has changed since the cache was populated.

### 3.3 `credit_status`

**Input schema** (empty):

```json
{
  "type": "object",
  "properties": {},
  "additionalProperties": false
}
```

**Output** (`structured_content`, a JSON object):

```json
{
  "credits": {
    "remaining_credits": 812,
    "plan_credits": 1000,
    "billing_period_end": "2026-11-01T00:00:00+00:00"
  },
  "source": "live" | "cached",
  "note": ""
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `credits` | object | The current credit snapshot. |
| `source` | `"live"` or `"cached"` | `live` is a fresh `GET /v2/team/credit-usage` call. `cached` means the in-memory counter was used (probe at startup, or last successful call's decrement). |
| `note` | string | Non-empty when the startup probe never succeeded. Tells the model the snapshot is best-effort. |

**Description (the model reads this verbatim):**

> Read the current Firecrawl credit balance for the configured key. Does NOT burn credits (it hits the `/v2/team/credit-usage` metadata endpoint, then falls back to the in-memory counter on transient errors). Use this to check your budget before issuing an expensive batch of calls.

---

## 4. Response mapping (Firecrawl → MCP)

Both tools apply a small mapping layer from Firecrawl's raw JSON onto the flat `structured_content` documented above.

### 4.1 `web_search`

Firecrawl `/v2/search` returns:

```json
{
  "success": true,
  "data": {
    "web": [
      { "url":          "https://example.com",
        "title":        "Example",
        "description":  "An example page.",
        "markdown":     "...",
        "metadata":     { ... } }
    ]
  },
  "creditsUsed": 7
}
```

The mapper drops every entry where `url` is missing (these are search-engine noise — entries the upstream accepted but couldn't resolve), keeps everything else, caps the list at `max_results` (post-clamp), and applies the per-result markdown truncation.

### 4.2 `scrape_url`

Firecrawl `/v2/scrape` returns:

```json
{
  "success": true,
  "data": {
    "markdown": "...",
    "metadata": {
      "title": "Example",
      "description": "...",
      "language": "en",
      "source_url": "https://example.com",
      "url":       "https://example.com",
      "status_code": 200
    }
  }
}
```

When `data.success` is `false`, the call is treated as an upstream error and surfaced as a `ToolError`. When `data.markdown` is empty but `success: true`, the tool still returns a structured response with empty `markdown` and the metadata, so the model can see that the page was scraped but had no markdown body.

---

## 5. Configuration

All configuration is via environment variables. See `.env.example` for the full list with defaults.

| Variable | Default | Purpose |
| --- | --- | --- |
| `FIRECRAWL_API_KEY` | – | Firecrawl API key. **Required for all tools.** `FIRECLAW_API_KEY` is also accepted as a legacy alias (the project name uses the misspelling; the alias exists only for parity with older deployment scripts — prefer the correctly-spelled variable). |
| `FIRECRAWL_TRANSPORT` | `http` | `http` (Streamable HTTP) or `stdio`. |
| `FIRECRAWL_HOST` | `127.0.0.1` | Bind address for HTTP transport. Loopback by design. |
| `FIRECRAWL_PORT` | `8000` | Port for HTTP transport. |
| `FIRECRAWL_PATH` | `/mcp` | URL path for the MCP endpoint. |
| `FIRECRAWL_CACHE_DIR` | `./cache/firecrawl` | On-disk cache directory. Gitignored. |
| `FIRECRAWL_CACHE_TTL_SECS` | `86400` | Cache freshness window, seconds. `0` disables the cache. |
| `FIRECRAWL_MIN_CREDITS` | `4` | Refuse a `web_search` call if the local credit counter is at or below this value. `0` disables the guard (not recommended on a free-tier key). |
| `FIRECRAWL_MAX_MARKDOWN_CHARS` | `8000` | Per-result markdown cap (~2k tokens). Prevents a single call from blowing the agent's context budget. |
| `FIRECRAWL_MIN_INTERVAL_MS` | `0` | Minimum spacing between upstream Firecrawl calls in milliseconds. `0` disables the limiter. |
| `FIRECRAWL_HTTP_TIMEOUT_S` | `60` | Upstream HTTP request timeout in seconds. |
| `MCP_LOG_LEVEL` | `WARNING` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Logs go to **stderr** (stdout is the JSON-RPC stream). `FASTMCP_LOG_LEVEL` is honoured as a fallback. |

---

## 6. Caching

On-disk TTL cache covering both tools. Key = normalised input (trimmed, inner whitespace collapsed, lowercased for queries; canonical URL for scrapes). File name = `<slug>-<fnv1a64>.json`. Envelope:

```json
{
  "fetched_at_unix": 1730000000,
  "kind": "search" | "scrape",
  "input": { "query": "..." } | { "url": "..." },
  "response": { ... structured_content minus credits_used_this_call and credits ... }
}
```

**Important:** the cache stores the **full, untruncated** markdown body. The truncation in §3 is a presentation-time concern, not a storage-time one. This way, if the operator changes `FIRECRAWL_MAX_MARKDOWN_CHARS`, the next call still has the full body to truncate from. The credit snapshot is **not** cached — every call returns a fresh view, but the cache hit does NOT decrement the credit counter.

**Failure semantics:** a missing, stale, corrupt, or unwritable cache entry **never fails the call**. The tool logs a `warn` and degrades to a plain uncached call.

**Bypass:** `web_search` and `scrape_url` each take a `force_refresh: bool` arg; when `true`, the cache is read for nothing and the call always hits Firecrawl. The response's `cache` field reports `"bypassed"` so the model can verify that the bypass actually happened.

The cache directory is gitignored. Delete any time to force fresh lookups.

---

## 7. Credit tracking

The most important piece of operational logic in the server. Firecrawl costs credits; the model needs to know how many it has left, and the server needs to refuse calls that would exhaust them. The tracking has two parts:

1. **Startup probe** — when the server starts, it calls `Firecrawl.get_credit_usage()` once and caches `remaining_credits`, `plan_credits`, and `billing_period_end` in memory. The probe is a metadata endpoint; it doesn't consume credits. If it fails, the server starts with `remaining_credits = None` and the first call surfaces a clear "credits unknown" note in `credit_status()`.
2. **Per-call decrement** — every successful `web_search` and `scrape_url` response includes a `creditsUsed` field. The server subtracts that from the cached counter after a successful call, so the running balance stays accurate without re-probing.

**Pre-call guard:** before every `web_search`, the server checks the cached counter and raises a `ToolError` with a clear message if there are not enough credits. The message tells the model to fall back to a free sibling tool or call `credit_status()` to confirm the situation. The minimum-credits threshold is `FIRECRAWL_MIN_CREDITS` (default 4; set to `0` to disable — not recommended on a free-tier key).

The full `credits` snapshot is included in **every** successful response, so the model can see its budget drop across calls and act accordingly. `credit_status()` exposes it on demand without burning credits.

---

## 8. Per-result markdown cap (context-budget protection)

Firecrawl can return full pages 50k+ characters long. A single `web_search` call with `max_results=5` could dump 250k+ characters into the agent's context and blow the context budget in one iteration. The cap keeps each result at `FIRECRAWL_MAX_MARKDOWN_CHARS` (default 8000, ~2k tokens), so a 5-result call adds at most ~10k tokens — well under the per-iteration budget. The full content stays in the Firecrawl cache and in this server's on-disk cache; re-querying with a narrower scope re-uses the cache hit (no credits burned).

**Always populate `markdown_chars` and `markdown_truncated_chars`** in the response. The model uses them to decide whether to ask for a narrower follow-up query (e.g. targeting a specific section of the page) or to call `scrape_url` on the same URL with a tighter focus.

---

## 9. Prompts

### 9.1 `research_topic`

A reusable user-message prompt that sets up a single `web_search` call. Args: `topic: str`. Returns a user message that:

1. Tells the model to use `web_search` with a canonical topic name (not a question).
2. Sets `max_results=5`.
3. Defines the empty-result fallback: rephrase once, then fall back to the model's own knowledge and say so.
4. Includes the credit-budget reminder: every response includes `credits`; switch to a free search tool when `remaining_credits` drops under ~50.

Registered on the same FastMCP instance as the tools. No extra setup.

### 9.2 `fetch_document`

A reusable user-message prompt that sets up a single `scrape_url` call with a `focus` question. Args: `url: str`, `focus: str`. Returns a user message that:

1. Tells the model to use `scrape_url` on the URL.
2. Asks the model to summarise the markdown around `focus`, dropping irrelevant sections before quoting.
3. Reminds the model that per-page markdown is capped and the cut is visible via `markdown_truncated_chars`.

Registered on the same FastMCP instance as the tools. No extra setup.

---

## 10. Install / setup

```bash
cd fireclaw-search-mcp
python3 -m venv .venv           # or: uv venv --python 3.12 .venv
source .venv/bin/activate
pip install -e .                # or: uv pip install -e .
cp .env.example .env
$EDITOR .env
```

**Dependencies (pinned, per the project's `pyproject.toml`):**

- `fastmcp==4.0.11` — MCP server framework, the standalone PrefectHQ package.
- `httpx>=0.27` — async HTTP client. The Firecrawl v2 API is a JSON over HTTPS surface with a Bearer token; an SDK adds no value here, and httpx gives us fail-open caching and timeout control with a single dependency.

Tested with Python 3.12. Works on 3.10+.

---

## 11. Run by hand

```bash
# HTTP (default) — listen on http://127.0.0.1:8000/mcp
python3 -m fireclaw_search_mcp

# Stdio — for clients that spawn the server as a child process
FIRECRAWL_TRANSPORT=stdio python3 -m fireclaw_search_mcp
```

A blank stdin in stdio mode lets the server sit idle. Point any MCP client (opencode, Claude Desktop, VS Code, Cursor) at the URL or spawn the process; see `docs/DEPLOYMENT.md` for the connection recipes.

---

## 12. Deployment

Two supported paths. Both rely on the server binding to `127.0.0.1`; neither requires the server itself to know it's being forwarded.

1. **VS Code "Forward a Port"** (recommended). Open the Ports panel, forward `8000`, copy the URL, point your client at `<forwarded-url>/mcp`. The tunnel runs through Microsoft's relay — no public hostname to block.
2. **ngrok** (fallback). `ngrok http 8000`, point your client at `<ngrok-url>/mcp`. Free-tier URLs are publicly enumerable; corporate networks often block them.

For both, the server is started with `python3 -m fireclaw_search_mcp`. No auth is configured — the loopback bind + tunnel auth is the access control. See `docs/DEPLOYMENT.md` for the full step-by-step.

---

## 13. Failure modes and how the model reacts

| What goes wrong | What the tool does | What the model sees |
| --- | --- | --- |
| `FIRECRAWL_API_KEY` not set | `credit_status()` reports the API key is missing; `web_search` / `scrape_url` raise `ToolError("FIRECRAWL_API_KEY not set")` on the first call | A clear error. Operator notices; no accidental credit spend. |
| `FIRECRAWL_API_KEY` invalid (`401`) | `ToolError("Firecrawl rejected the API key")` | A clear HTTP error. Operator notices; model falls back. |
| Firecrawl out of credits (`402` / `insufficient_credits`) | The pre-check raises `ToolError("Firecrawl is out of credits for this billing period")` before the API call | A clear budget error. No credit wasted. The model can call `credit_status()` to confirm. |
| Firecrawl rate limit (`429`) | `ToolError("Firecrawl is rate-limiting this key")` | A rate-limit error. The model can wait a few seconds or fall back. |
| `get_credit_usage()` probe fails at startup | Server starts with `remaining_credits = None`; the first call proceeds without a pre-check; `credit_status()` reports a "credits unknown" note | A normal call with a note. The model can still try; the pre-check is a guard, not a hard requirement. Operator sees a `warn` log. |
| A result's page body is huge | Per-result cap at `FIRECRAWL_MAX_MARKDOWN_CHARS`, with the truncation marker and the `markdown_chars` / `markdown_truncated_chars` fields | A normal success with a clearly marked cut. The model can re-query with a narrower scope or call `scrape_url` on the same URL with a tighter focus. |
| Cache directory unwritable | Logs `warn`; falls through to a plain uncached call | The tool still works, just slower on repeat queries (and credits will be spent). |
| Cache file corrupt | Logs `warn`; falls through to a fresh upstream call | Same. |
| Foreign `Origin` header | Accepted when the server is bound to `127.0.0.1` (DNS rebinding isn't possible) | The probe flags this as a WARN; see `docs/DEPLOYMENT.md` for when to enable `http_host_origin_protection`. |
| Server didn't start (port in use, venv missing, key missing) | Process exits with a clear log line on stderr | The client gets a connection error. No silent fallback. |

---

## 14. Acceptance criteria

The implementation is "good enough to ship" when **all** of the following hold:

1. `pip install -e .` from a clean checkout succeeds on Python 3.10+. ✅
2. With a real `FIRECRAWL_API_KEY` set, `python3 -m fireclaw_search_mcp` starts on `http://127.0.0.1:8000/mcp` and answers `server/discover` with `supportedVersions: ["2026-07-28"]`. ✅
3. The advertised tools are exactly `web_search`, `scrape_url`, and `credit_status`, with the input schemas and descriptions in §3. ✅
4. The advertised prompts are exactly `research_topic` and `fetch_document`. ✅
5. A live `web_search` call against Firecrawl with a real key returns at least one result with title, URL, and (possibly truncated) markdown. ✅
6. The response includes the `credits` snapshot and `credits_used_this_call` is non-zero. ✅
7. A second identical `web_search` call within the cache TTL produces a response with `cache: "hit"` and **no** decrement to the credit counter. ✅
8. A `scrape_url` call against a known URL returns the page body, metadata, and a `credits` snapshot. ✅
9. A second identical `scrape_url` call within the cache TTL produces a response with `cache: "hit"`. ✅
10. Setting `FIRECRAWL_API_KEY` to an invalid value (or unsetting it on a real key) returns `ToolError` with a clear message from the affected tools. ✅
11. Setting `FIRECRAWL_MIN_CREDITS=999` with a fresh free-tier key (1000 credits) refuses the first `web_search` call with the "out of credits" message. ✅
12. A result whose page body exceeds `FIRECRAWL_MAX_MARKDOWN_CHARS` is truncated and the `markdown_chars` / `markdown_truncated_chars` fields are populated correctly. ✅
13. The wire probe (`scripts/probe_mcp_server.py`) reports 0 failures against the live server. ✅
14. The in-process test suite (`pytest`) reports 0 failures in both `mode="auto"` and `mode="legacy"`. ✅

---

## 15. Test plan

Unit tests (hermetic, no network) live in `tests/`:

- `test_normalize.py` — query and URL normalisation, slug, FNV-1a 64-bit hash.
- `test_cache.py` — round trip, normalisation equivalence, stale entry, corrupt file, unwritable dir, atomic write.
- `test_rate_limit.py` — disabled at 0 ms, serial waits spaced, concurrent waits queued.
- `test_firecrawl.py` — search and scrape response mapping (entry filtering, markdown truncation, per-result fields, credit debit), credit-usage probe mapping.
- `test_server.py` — tool/prompt advertisement, basic search, scrape, count clamp, empty result, cache hit, both client modes, force_refresh bypass, credit guard, missing-key error.
- `test_server_integration.py` — real Firecrawl calls, skipped unless `FIRECRAWL_NETWORK_TESTS=1`.

End-to-end (manual):

- `python3 -m fireclaw_search_mcp` then connect from any modern MCP client (opencode, VS Code, Claude Desktop, Cursor) and confirm `web_search` and `scrape_url` appear and return results.
- Call `credit_status()` and confirm it returns a credit snapshot without burning credits.

---

## 16. Out of scope for v0.1

- **No other Firecrawl endpoints.** No `/v2/crawl` (whole-site crawl), no `/v2/extract` (structured extraction), no `/v2/map` (URL discovery). The three tools cover search, single-URL scrape, and credit status only. If a future need arises (e.g. crawl), it can be added as a fourth tool on the same server.
- **No async polling.** None of the three tools Firecrawl exposes that are async (`/v2/crawl`, `/v2/extract`, `/v2/deep-research`) are exposed; this server only uses sync endpoints.
- **No rate-limit-aware retries beyond a single attempt.** The upstream rate limiter (§7 `FIRECRAWL_MIN_INTERVAL_MS`) spaces calls out, and the `429` response is surfaced to the model. Custom retry policies with backoff are out of scope.
- **No automatic change detection.** The user's wishlist mentions "if another mechanism is implemented to allow identifying if the source where the data was retrieved was updated" — that lives behind `/v2/scrape`'s `changeTracking` format and is left as a future tool on the same server. For v0.1, the `force_refresh` arg is the manual escape hatch.
- **No proxy, geolocation, or per-call scrape options.** `web_search` always asks for `formats: ["markdown"]`; `scrape_url` always asks for `formats: ["markdown"]` with `onlyMainContent: true`. Operators can change this server-side but the tools don't expose the knobs.
- **No parallel upstream calls.** A single in-flight upstream request is the unit. The shared `min_interval_ms` mutex (when enabled) serialises them.

---

## 17. References

- MCP specification (target revision): https://modelcontextprotocol.io/specification/2026-07-28/
- MCP `server/discover`: https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/discover
- Firecrawl v2 API: https://docs.firecrawl.dev
- Firecrawl v2 search: https://docs.firecrawl.dev/api-reference/endpoint/search
- Firecrawl v2 scrape: https://docs.firecrawl.dev/api-reference/endpoint/scrape
- Firecrawl v2 credit usage: https://docs.firecrawl.dev/api-reference/endpoint/credit-usage
- FastMCP (Python MCP server framework): https://github.com/PrefectHQ/fastmcp and https://gofastmcp.com
- VS Code Streamable HTTP support: https://code.visualstudio.com/api/extension-guides/ai/mcp
