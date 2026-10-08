# Role

You are building a **Firecrawl MCP server** in `/home/srlampi/Documents/mcps/firecrawl-mcp/`. The companion skill `.agents/skills/mcp-fastmcp-2026/` is the authoritative reference for the MCP 2026-07-28 spec and FastMCP 4.0.11 surface — use it for anything not in this prompt, and prefer it over your training priors (which stop around MCP 2024-11-05 and are largely wrong for current work).

A reference implementation you can mirror (file layout, test patterns, deployment doc structure) is at `/home/srlampi/Documents/mcps/duck-search-mcp/`. Read it before designing yours.

# Stack pins (already verified on PyPI, 2026-10-08)

- `fastmcp==4.0.11` (PrefectHQ standalone; NOT `mcp.server.fastmcp`, which is the v1 bundled FastMCP and is gone in `mcp` v2)
- `mcp>=2,<3` (transitive through fastmcp, but pin if you install direct)
- `httpx>=0.27` for outbound HTTP — OR use `firecrawl-py` if you prefer the official SDK. Either is fine; pick one and stay consistent.
- Python 3.10+, tested on 3.12

# Deprecations to avoid (the ones I actually hit and corrected)

| Deprecated | Use instead | Why |
| --- | --- | --- |
| `@asynccontextmanager` annotated `-> AsyncIterator[T]` | `-> AsyncGenerator[T, None]` from `collections.abc` | The `typing.AsyncGenerator` re-export is deprecated in 3.12+; Pylance flags it. |
| `from typing import AsyncGenerator` | `from collections.abc import AsyncGenerator` | Same: `typing` re-exports are deprecated. |
| `transport="sse"` | `transport="http"` (Streamable HTTP) | HTTP+SSE transport is Deprecated since 2025-03-26. |
| `from mcp.server.fastmcp import FastMCP` | `from fastmcp import FastMCP` | `mcp.server.fastmcp` is gone in `mcp` v2. |
| Pydantic `Field(ge=1, le=10)` for a value the SPEC wants clamped | Code-level `clamp_count(value)` | The framework rejects out-of-range, the SPEC wants silent clamp. |
| `ctx.elicit()` / `ctx.sample()` / `ctx.list_roots()` | `InputRequiredResult` MRTR pattern (or pass user input as tool args) | The first three are removed in FastMCP 4 and Deprecated in the spec. |
| Module-level singletons keyed by connection | FastMCP `lifespan` → `ctx.request_context.lifespan_context` | Sessions are gone in 2026-07-28; any request can hit any replica. |

If you find yourself about to write any of the left column, stop and reach for the right column.

# Code patterns (lifted from the working duck-search-mcp)

```python
# 1. FastMCP constructor — set cache hints on the instance, transport on run().
mcp = FastMCP(
    name="firecrawl-mcp",
    instructions="...",                 # model-facing; keep it honest
    cache_ttl=300,                       # tool/prompt lists rarely change
    cache_scope="public",                # "private" only if lists vary per user
    lifespan=lifespan,                   # owns httpx, cache, rate limiter, etc.
)

# 2. Lifespan — AsyncGenerator[dict, None], not AsyncIterator.
@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncGenerator[dict, None]:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    async with httpx.AsyncClient(...) as http_client:
        yield {"settings": settings, "cache": cache, "http_client": http_client, ...}

# 3. Tool — name on the decorator, type hints + docstring drive the schema.
@mcp.tool(name="scrape_url", description="...")  # the model reads this verbatim
async def scrape_url(url: str, ctx: Context | None = None) -> dict:
    state = ctx.request_context.lifespan_context
    ...

# 4. Run — transport is per-call, host/port on run(), bind 127.0.0.1.
mcp.run(transport="http", host="127.0.0.1", port=8000, path="/mcp")
Other rules that apply:
- Loopback bind only for local. If you ever bind to 0.0.0.0, enable http_host_origin_protection AND add auth (e.g. JWTVerifier).
- Logs to stderr (stdout is the JSON-RPC stream).
- Read env from a single settings.py, never hardcode URLs/keys.
- Empty upstream responses are not errors. Return {"results": [], "note": "..."} and let the model rephrase or fall back. The duck-search SPECS §6 has the canonical phrasing.
- Cache failures fail open. Missing/stale/corrupt/unwritable cache must NEVER break the call. Log a warning, fall through.
Tests — mock at the function boundary, NOT at the lifespan
The lifespan-mock pattern (patching mcp._lifespan after construction) does not work reliably. Mock the outbound function directly:
@pytest.fixture
def fake_firecrawl(monkeypatch, tmp_cache_dir):
    fake = FakeFirecrawl()
    monkeypatch.setattr(server_module, "scrape_thing", fake)
    return fake

async def test_scrape_returns_structured_content(fake_firecrawl):
    fake_firecrawl.set("https://example.com", {"markdown": "Hello."})
    async with Client(mcp) as c:
        result = await c.call_tool("scrape_url", {"url": "https://example.com"})
        assert result.structured_content["markdown"] == "Hello."
Other test patterns from the duck-search-mcp suite that translate:
- Test in both mode="auto" and mode="legacy" via @pytest.fixture(params=["auto", "legacy"]). FastMCP 4 is dual-era, claim it.
- Per-test cache dir via monkeypatch.setenv("CACHE_DIR", str(tmp_path)) — never let tests share a cache.
- Gate network tests behind an env var (FIRECRAWL_NETWORK_TESTS=1) so CI stays fast and hermetic.
- Cache failure paths — corrupt file, unwritable dir, stale TTL. The cache must fall through, never raise.
- Empty upstream response — assert the empty-result shape with a non-empty note.
- Clamp behavior — count=0 → 1, count=11 → 10, count=None → 5, depending on what your SPEC says.
Verification (do not skip; "it starts" proves nothing)
# 1. Static — 0 HIGH, 0 MEDIUM in src/ and tests/
.venv/bin/python .agents/skills/mcp-fastmcp-2026/scripts/audit_mcp_project.py .

# 2. In-process — both modes
.venv/bin/pytest                                    # hermetic
FIRECRAWL_NETWORK_TESTS=1 .venv/bin/pytest          # + network

# 3. Wire — start the server in one terminal, then:
.venv/bin/python .agents/skills/mcp-fastmcp-2026/scripts/probe_mcp_server.py http://127.0.0.1:8000/mcp
# Expect 0 failures, 1 expected WARN about Origin (we bind loopback).

# 4. Live curl — confirm a real scrape call returns markdown
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/call' -H 'Mcp-Name: scrape_url' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"scrape_url","arguments":{"url":"https://example.com"},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"curl","version":"1"},"io.modelcontextprotocol/clientCapabilities":{}}}}'
Deployment (mirror duck-search-mcp's docs/DEPLOYMENT.md)
- Default transport: Streamable HTTP on 127.0.0.1:8000/mcp. Stdio is opt-in via env.
- VS Code port forwarding is the recommended tunnel (no public hostname to block). Document the Ports panel steps + per-client config snippets (opencode, Claude Desktop, VS Code Copilot, Cursor).
- ngrok is the documented fallback. Mention that free-tier URLs are often blocked by corporate firewalls.
- Add a hardening section for the case where someone binds to 0.0.0.0 (enable http_host_origin_protection, add JWTVerifier).
- If Firecrawl needs an API key, document it in .env.example, never commit a real one, and read it via the same settings.py you used for everything else.
If anything contradicts the skill
The skill snapshot is 2026-10-08. Before declaring anything "the latest":
- pip index versions fastmcp (or https://pypi.org/project/fastmcp/)
- https://gofastmcp.com/changelog
- https://modelcontextprotocol.io/specification/ (home links the current revision)
If a newer release exists, use it and say so. If a doc or the installed package contradicts the skill, the installed package wins — note the discrepancy to the user.

**Why this shape:**
- The role + skill reference up front keeps the model from re-deriving the spec from memory (which is what got us the original 2.x / stdio-only SPECS in the first place).
- The deprecation table is the single most valuable part — those are the items I actually had to correct, not abstract guidance.
- The code-pattern block is a copy-pasteable scaffold, not prose. A model reads `cache_scope="public"` faster than "set the cache scope to public for shared catalogs".
- The test fixture snippet bakes in the lesson from when the lifespan-mock pattern silently failed on me.
- The verification block is a shell script, not a checklist. Easy to actually run.
- "If anything contradicts the skill" closes the loop the user opened: priors are suspect, ground truth wins.