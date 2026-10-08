# `rust-agent-firecrawl-mcp` — MCP server

> **Status:** spec only, not yet implemented. The behaviour described below is the **minimum required to be considered a viable replacement for the current `web_search_deep` tool**. Implementation happens in this repo; integration with the agent happens by editing `config/mcp.toml` in the `rust-agent` repo. (/home/srlampi/Documents/projects/rust-agent/)

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes a single deep-search tool to the `rust-agent` over stdio:

| Tool | Backend | Cost | Best for |
| --- | --- | --- | --- |
| `web_search` | Firecrawl `/v2/search` with `scrape_options.formats=["markdown"]` | 1,000 credits/month free tier; ~2–7 credits per call | Complex, long-tail, or question-shaped queries that need the **full page contents** as clean markdown. |

The Rust agent spawns this server as a child process via the official [`rmcp`](https://github.com/modelcontextprotocol/rust-sdk) crate, lists the tool, and registers it as a regular `rig::tool::Tool`. From the agent's point of view the tool is indistinguishable from any other.

---

## 1. Why this server exists

This server is one of **three** that together replace the current bundled `mcp-servers/web-search/` MCP. The bundle is being split so each backend (DDG, Firecrawl, Tavily) can be enabled, disabled, scaled, and rate-limited independently. The agent code is **unaware** of the split — it still sees one `web_search`-shaped tool per server, with the server-name prefix disambiguating which backend answered.

Firecrawl is the **deep** path. Where the DDG tool returns snippets only, this tool returns each result's full page body as clean markdown — useful for long-tail, question-shaped, or otherwise complex queries where snippets aren't enough. The cost is Firecrawl credits, which the agent must budget. Every response includes a `credits` snapshot so the calling model can see the balance drop across calls and switch to the free path (`duckduckgo__web_search` or `tavily__web_search`) when it gets low.

The other two MCPs in the set:

- **DuckDuckGo** for entity-like queries (free, no key, snippets only).
- **Tavily** for general question-shaped queries (free tier, agent-tuned structured results, no markdown body by default — `firecrawl__web_search` is the right tool when the model genuinely needs the page contents).

---

## 2. Protocol contract

The agent (per [`docs/mcp.md`](https://github.com/example/rust-agent/blob/main/docs/mcp.md) §8) requires:

1. Speaks **MCP 2024-11-05** (the version `rmcp 0.8` negotiates).
2. Accepts JSON-args objects on `tools/call`.
3. Returns either `structured_content` (preferred) or at least one text content block.
4. Advertises at least one tool on `tools/list`.

Anything beyond this contract is a server-side decision.

---

## 3. Tool

### `web_search` (single tool)

**Input schema** (JSON Schema, surfaced verbatim to the model):

```json
{
  "type": "object",
  "properties": {
    "query":       { "type": "string",  "description": "..." },
    "max_results": { "type": "integer", "minimum": 1, "maximum": 10,
                     "default": 5,      "description": "..." }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

| Argument | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `query` | string | yes | – | The search query. The Firecrawl API takes the query as `q`. |
| `max_results` | integer | no | 5 | Number of search results to return. Clamped to `[1, 10]`. Each result is scraped for markdown, so a larger value costs more credits and dumps more into context. |

**Output** (`structured_content`, a JSON object):

```json
{
  "query": "how to configure a custom JSON logger in tokio",
  "results": [
    { "title": "...",
      "url":   "...",
      "markdown": "...",
      "markdown_chars": 12345,
      "markdown_truncated_chars": 8000 }
  ],
  "credits_used_this_call": 7,
  "credits": {
    "remaining_credits": 812,
    "plan_credits": 1000,
    "billing_period_end": "2025-11-01T00:00:00+00:00"
  },
  "cache": "miss" | "hit"
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `query` | string | The query as the server received it. |
| `results` | array of `{title, url, markdown, markdown_chars, markdown_truncated_chars}` | See below. Capped at `max_results`. |
| `credits_used_this_call` | integer | From the Firecrawl `search` response's `creditsUsed` field. The server subtracts this from its in-memory counter. |
| `credits` | object | `{remaining_credits, plan_credits, billing_period_end}` — the current balance, refreshed by the startup probe and decremented by each call. |
| `cache` | `"miss"` or `"hit"` | Whether the response was served from the on-disk Firecrawl cache. |

**Per-result fields:**

| Field | Type | Notes |
| --- | --- | --- |
| `title` | string | From the Firecrawl search result metadata. |
| `url` | string | The canonical URL. |
| `markdown` | string | The page body as clean markdown, **truncated to `DEEP_MAX_MARKDOWN_CHARS` (default 8000) per result**. A `…truncated…` marker is appended so the model can see the cut. |
| `markdown_chars` | integer | The original body length **before** truncation. Lets the model know how much was left out. |
| `markdown_truncated_chars` | integer | The cut length (≤ `markdown_chars`). Equal when no truncation happened. |

**Description (the model reads this verbatim):**

> Paid, deep web search via Firecrawl. Reach for this **only** when the question is complex, long-tail, or you genuinely need the **full page contents** as clean markdown — not just snippets.
> Costs Firecrawl credits (~2–7 per call, depending on `max_results`). Every response includes a `credits` snapshot: check `remaining_credits` and switch to `duckduckgo__web_search` or `tavily__web_search` once it drops under ~50. Per-result markdown is capped at ~8k chars to protect the agent's context budget; the `markdown_chars` / `markdown_truncated_chars` fields tell you how much was cut.
> Use `max_results` to cap the result list (default 5, max 10).

---

## 4. Credit tracking

This is the most important piece of operational logic in the server. Firecrawl costs credits; the model needs to know how many it has left, and the server needs to refuse calls that would exhaust them.

The tracking has two parts:

1. **Startup probe** — when the server starts, it calls `Firecrawl.get_credit_usage()` once and caches `remaining_credits`, `plan_credits`, and `billing_period_end` in memory. The probe is a metadata endpoint; it doesn't consume credits. If it fails, the server starts with `remaining_credits = 0` and the first call will see a clear "credits unknown" error.
2. **Per-call decrement** — every `search()` response includes a `creditsUsed` field. The server subtracts that from the cached counter after a successful call, so the running balance stays accurate without re-probing.

**Pre-call guard:** before every call, the server checks the cached counter and raises `is_error: true` with a clear message if there are not enough credits. The message tells the model to fall back to `duckduckgo__web_search` or `tavily__web_search`. The minimum-credits threshold is `FIRECRAWL_MIN_CREDITS` (default 4; set to `0` to disable — not recommended on a free-tier key, a runaway agent loop can otherwise burn all 1,000 credits in a few iterations).

The full `credits` snapshot is included in **every** successful response, so the model can see its budget drop across calls and act accordingly.

---

## 5. Configuration

All configuration is via environment variables. The Rust agent forwards the relevant ones when it spawns the server via the `env_pass` allow-list in `config/mcp.toml` — see the agent's [`docs/mcp.md`](https://github.com/example/rust-agent/blob/main/docs/mcp.md) §3 for the env-forwarding semantics.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `FIRECRAWL_API_KEY` | yes | – | Firecrawl API key. Get a free one at <https://www.firecrawl.dev>. Free tier: 1,000 credits/month. |
| `FIRECRAWL_API_KEY_2` | no | – | Alias for `FIRECRAWL_API_KEY`. Accepted because the agent's `.env` historically mis-spells it as `FIRECLAW_API_KEY` (one W). The server reads `FIRECRAWL_API_KEY` first, then `FIRECLAW_API_KEY`, then `FIRECRAWL_API_KEY_2`. |
| `FIRECRAWL_MIN_CREDITS` | no | `4` | Refuse a call if the local credit counter is at or below this value. `0` disables the guard. |
| `DEEP_MAX_MARKDOWN_CHARS` | no | `8000` | Per-result markdown cap (~2k tokens). Prevents a single call from blowing the agent's context budget. |
| `DEEP_TRUNCATION_MARKER` | no | `"\n\n[…markdown truncated to fit the context budget; the full body is in the Firecrawl cache…]"` | String appended to a truncated body. The model sees the cut and knows it's intentional. |
| `FIRECRAWL_CACHE_DIR` | no | `./cache/firecrawl` | On-disk cache directory. Gitignored. |
| `FIRECRAWL_CACHE_TTL_SECS` | no | `86400` | Cache freshness window, seconds. `0` disables. |
| `MCP_LOG_LEVEL` | no | `WARNING` | `DEBUG` / `INFO` / `WARNING`. Logs go to **stderr** (stdout is the JSON-RPC stream). |

---

## 6. Per-result markdown cap (context-budget protection)

Firecrawl can return full pages 50k+ characters long. A single `web_search` call with `max_results=5` could dump 250k+ characters into the agent's context and blow the context budget in one iteration. The cap keeps each result at `DEEP_MAX_MARKDOWN_CHARS` (default 8000, ~2k tokens), so a 5-result call adds at most ~10k tokens — well under the per-iteration budget. The full content stays in the Firecrawl cache; re-querying with a narrower scope re-uses the cache hit (no credits burned).

**Always populate `markdown_chars` and `markdown_truncated_chars`** in the response. The model uses them to decide whether to ask for a narrower follow-up query (e.g. targeting a specific section of the page).

---

## 7. Caching

On-disk TTL cache mirroring the format of the bundled MCP's Firecrawl cache. Key = normalised query. File name = `<slug>-<fnv1a64>.json`. Envelope:

```json
{ "fetched_at_unix": 1730000000,
  "query": "...",
  "results": [ { "title": "...", "url": "...",
                 "markdown": "...",     // full, untruncated
                 "markdown_chars": 12345 } ],
  "credits_used_this_call": 7,
  "fetched_credits_snapshot": { ... } }
```

**Important:** the cache stores the **full, untruncated** markdown body. The truncation in §6 is a presentation-time concern, not a storage-time one. This way, if the operator changes `DEEP_MAX_MARKDOWN_CHARS`, the next call still has the full body to truncate from.

Cache hits **do not** decrement the credit counter — that's the point of the cache. The `cache: "hit"` field in the response makes this visible to the model.

The cache directory is gitignored. Delete any time to force fresh lookups.

---

## 8. Install / setup

```bash
cd rust-agent-firecrawl-mcp
python3 -m venv .venv      # or: uv venv --python 3.12 .venv
source .venv/bin/activate
pip install -e .
```

**Dependencies (suggested):**

- `fastmcp>=2.0` — MCP server framework. (The project is `prefecthq/fastmcp` on GitHub but published as `fastmcp` on PyPI; it is the standard framework for building MCP servers in Python and powers most of the Python MCP ecosystem.)
- `firecrawl-py>=4.0` — official Firecrawl SDK. The v2 client lives under `firecrawl.v2` and exposes `search()` plus `get_credit_usage()`.

Tested with Python 3.10+. Other interpreters are fine as long as the `rmcp` 0.8 client on the agent side negotiates `2024-11-05` (the version `fastmcp` 2.x ships).

---

## 9. Run by hand (for debugging)

```bash
# Set the API key first.
export FIRECRAWL_API_KEY=fc-...

# The server speaks JSON-RPC over stdio. A blank stdin will let
# it sit idle; pipe a real `initialize` + `tools/list` exchange
# to see the registered tool schema. The startup probe against
# `get_credit_usage()` will run before the first call, so a
# bad key surfaces as a clear error in the logs.
python3 -m firecrawl_mcp
```

---

## 10. Integration with `rust-agent`

Add a single entry to `config/mcp.toml` in the `rust-agent` repo:

```toml
[[mcp.servers]]
name = "firecrawl"
command = ["/abs/path/to/rust-agent-firecrawl-mcp/.venv/bin/python",
           "/abs/path/to/rust-agent-firecrawl-mcp/firecrawl_mcp/__main__.py"]
env_pass = ["FIRECRAWL_API_KEY", "PATH", "HOME"]
enabled = true
```

Restart the agent. The model now sees the tool under its qualified name `firecrawl__web_search`. To temporarily disable without removing the entry, set `enabled = false` (the agent parses and validates the entry but skips spawning).

The agent's `.env` (or wherever the operator keeps the key) should export `FIRECRAWL_API_KEY`. The agent's `config/mcp.toml` entry's `env_pass` allow-list forwards it to the spawned subprocess.

The default `web-search` MCP entry in `config/mcp.toml` (the bundled DDG + Firecrawl one) **must be removed or disabled** before the new Firecrawl entry is enabled, or the agent will register two `web_search` shaped tools and the model will be confused about which to use.

---

## 11. Failure modes and how the model reacts

| What goes wrong | What the tool does | What the model sees |
| --- | --- | --- |
| `FIRECRAWL_API_KEY` not set | `is_error: true` on first call (lazy — no startup probe without a key) | "Firecrawl API key not set; reach for `duckduckgo__web_search` or `tavily__web_search` instead." |
| `FIRECRAWL_API_KEY` invalid (`401`) | `is_error: true` with the 401 message | "Firecrawl rejected the API key." Operator notices; model falls back. |
| Firecrawl out of credits (`402`) | The pre-check raises `is_error: true` **before** the API call | "Firecrawl is out of credits for this billing period. Use `duckduckgo__web_search` or `tavily__web_search`." No credit wasted. |
| Firecrawl rate limit (`429`) | The SDK retries with backoff (default 3 attempts); on exhaustion, `is_error: true` | A rate-limit error. The model can wait or fall back. |
| `get_credit_usage()` probe fails at startup | Server starts with `remaining_credits = 0`; the first call returns an "credits unknown" error | The model can still try the call; the pre-check is a guard, not a hard requirement. Operator sees a `warn` log. |
| A result's page body is huge | Per-result cap at `DEEP_MAX_MARKDOWN_CHARS`, with the truncation marker and the `markdown_chars` / `markdown_truncated_chars` fields | A normal success with a clearly marked cut. The model can re-query with a narrower scope to read more. |
| Cache directory unwritable | Logs `warn`; falls through to a plain uncached call | The tool still works, just slower on repeat queries (and credits will be spent). |
| MCP server child dies | The `rmcp` client surfaces an `ErrorData`; the tool returns a protocol-level error | A connection error. The supervisor loop's "two empty iterations" rule nudges the model. |
| Server didn't spawn (venv missing or no key) | `build_agent` returns an error at agent startup | The agent refuses to start with a clear message naming the server and the command. **No silent fallback** — the design choice documented in `docs/mcp.md` §7. |

---

## 12. Context-budget protection

Two complementary mechanisms keep the agent from blowing the MiniMax-M3 1M-token context window on its own:

1. **Per-result markdown cap** (this server, §6). Keeps each result's contribution to the agent's context bounded.
2. **Context-pressure guard in the supervisor loop** (agent side, `loop_strategy::CONTEXT_PRESSURE_PROMPT`). When the estimated chat-history size crosses `RUST_AGENT_CONTEXT_PRESSURE_THRESHOLD_TOKENS` (default 800k, ~80% of MiniMax-M3's 1M window), the supervisor injects a directive to stop searching, commit findings to the skill-set document, and call `finalize_skill_set`. The prompt fires at most once per run.

Together these mean: even in the worst case (8 iterations × 8 multi-turn tool calls × a 5-result deep search per call), the agent caps its input at roughly 8 × 8 × 10k = 640k tokens, with the pressure guard catching anything that slips through. The model has ~200k tokens of headroom to generate its final answer.

---

## 13. Acceptance criteria (minimum viable)

The implementation is "good enough to unblock the agent" when **all** of the following hold:

1. `pip install -e .` from a clean checkout succeeds on Python 3.10+.
2. `python3 -m firecrawl_mcp` starts, runs the credit-usage probe (visible in stderr at `MCP_LOG_LEVEL=INFO`), and responds to a manual `initialize` + `tools/list` JSON-RPC exchange.
3. The advertised tool is exactly `web_search`, with the input schema and description in §3.
4. A live call against Firecrawl with a real key returns at least one result with title, URL, and (possibly truncated) markdown.
5. The response includes the `credits` snapshot and `credits_used_this_call` is non-zero.
6. A second identical call within the cache TTL produces a response with `cache: "hit"` and **no** decrement to the credit counter.
7. Setting `FIRECRAWL_API_KEY` to an invalid value (or unsetting it on a real key) returns `is_error: true` with a clear message.
8. Setting `FIRECRAWL_MIN_CREDITS=999` with a fresh free-tier key (1000 credits) refuses the first call with the "out of credits" message.
9. A result whose page body exceeds `DEEP_MAX_MARKDOWN_CHARS` is truncated and the `markdown_chars` / `markdown_truncated_chars` fields are populated correctly.
10. Spawning this server from the agent's `config/mcp.toml` succeeds, the model sees the tool as `firecrawl__web_search`, and a one-shot request (`cargo run -- run --request "How do I write a custom JSON logger in tokio?"`) triggers at least one call to it and ends with a non-empty final reply that cites one of the returned URLs.

---

## 14. Test plan

Unit tests (hermetic, no network):

- `cache_round_trip_serves_second_call_from_disk` — write a cache entry, read it back, assert `cache: "hit"` and that the credit counter was **not** decremented.
- `cache_stores_full_untruncated_markdown` — write a result whose body is 20k chars, read it back, assert the cached `markdown` is the full 20k, and the response-time truncation happens after the cache read.
- `count_is_clamped_to_1_through_10` — same shape as the DDG tests.
- `per_result_markdown_is_capped` — feed a result with a 50k-char body, assert response `markdown` is ≤ 8000 chars, the truncation marker is present, and `markdown_chars` / `markdown_truncated_chars` are populated.
- `credit_counter_decrements_after_successful_call` — start with `remaining_credits = 100`, simulate a call with `creditsUsed = 7`, assert the post-call counter is 93.
- `credit_guard_refuses_call_below_min_credits` — start with `remaining_credits = FIRECRAWL_MIN_CREDITS`, assert the call raises `is_error: true` without hitting the network.
- `missing_or_invalid_api_key_returns_clear_error` — unset `FIRECRAWL_API_KEY`, assert the first call returns `is_error: true` with a "key not set" message; set it to an invalid value, assert the call returns `is_error: true` with a "rejected" message.

Integration tests (network, gated on the venv and a real key existing):

- `mcp_server_advertises_web_search` — handshake, `tools/list`, assert exactly one tool named `web_search`.
- `basic_search_returns_results_with_credits_snapshot` — call with a canonical query, assert `results.len() >= 1`, `credits_used_this_call > 0`, and the `credits` object is present.
- `second_call_within_ttl_serves_from_cache` — call twice, assert the second has `cache: "hit"`.
- `truncation_marker_appears_on_long_pages` — find a query whose first result is > `DEEP_MAX_MARKDOWN_CHARS` (a known Wikipedia long article is a good fixture), assert the marker is present and the per-result fields are correct.

End-to-end (manual, smoke checklist from `docs/testing.md`):

- `cargo run -- run --request "Design a tokio JSON logger skill set"`
  — assert the trace contains at least one `firecrawl__web_search` call, and the final Markdown cites at least one URL from the results.

---

## 15. Out of scope for v0.1

- **No other Firecrawl endpoints.** No `/v2/scrape` (single-URL scrape), no `/v2/crawl` (whole-site crawl), no `/v2/extract` (structured extraction). The single tool is search + scrape in one call. URL-by-URL scrape is out of scope for v0.1; if the agent ever needs it, it can be added as a second tool on the same server.
- **No async polling.** Firecrawl's `/v2/extract` is async; the search endpoint is sync. This server only uses the sync search endpoint.
- **No rate-limit-aware retries beyond the SDK default.** The Firecrawl SDK retries 429s with backoff (default 3 attempts). Custom retry policies are out of scope.
- **No HTTP transport.** Stdio only, per `docs/mcp.md` §6. When the agent's HTTP transport lands, this server may be wrapped behind a small stdio-to-HTTP shim, or a parallel HTTP entry point can be added later — out of scope for v0.1.

---

## 16. References

- Agent integration overview: [`docs/mcp.md`](https://github.com/example/rust-agent/blob/main/docs/mcp.md) in the `rust-agent` repo.
- Agent's `tools::mcp` module-level reference: [`docs/modules/tools-mcp.md`](https://github.com/example/rust-agent/blob/main/docs/modules/tools-mcp.md).
- Existing bundled DDG + Firecrawl MCP (split source for this work): [`mcp-servers/web-search/`](https://github.com/example/rust-agent/tree/main/mcp-servers/web-search) in the `rust-agent` repo — specifically `server.py` (the `web_search_deep` / `_do_firecrawl_deep_search` path) and `docs/architecture.md` (the credit-tracking and per-result-markdown-cap contract). When the bundled MCP is retired, that document moves here.
- Firecrawl API: <https://docs.firecrawl.dev>.
- `firecrawl-py` SDK: <https://github.com/mendableai/firecrawl-py>.
- MCP specification: <https://modelcontextprotocol.io>.
- `fastmcp` (Python MCP server framework): <https://github.com/PrefectHQ/fastmcp>.
