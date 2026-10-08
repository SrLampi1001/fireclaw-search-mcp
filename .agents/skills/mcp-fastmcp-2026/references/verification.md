# Verification workflow and sources

"It runs" is not verification. A server can start fine and still speak the old protocol, leak sessions, skip required headers or return uncached lists. Run these layers in order and report evidence, not impressions.

## Contents
- V1 Static audit
- V2 Tests (in-process, both eras)
- V3 Wire probe (real HTTP)
- V4 Real-client smoke test
- V5 Reading probe output (what each result means)
- V6 Freshness: is there something newer than this skill knows?
- V7 Source index and fetch order
- V8 Evidence checklist for your final answer

---

## V1 Static audit
```bash
python <skill>/scripts/audit_mcp_project.py <project-dir>            # exit 1 on HIGH
python <skill>/scripts/audit_mcp_project.py <project-dir> --fail-on medium
```
Goal: 0 HIGH, 0 MEDIUM, or each remaining one explained (e.g. an intentional legacy-compat version list). INFO items are suggestions. It is a line scanner — read each hit in context before "fixing" it, and remember it can't see semantic problems (e.g. a side effect placed before an MRTR confirmation).

## V2 Tests (in-process, both eras)
Use `fastmcp.Client(mcp)` with no server process. `mode="auto"` negotiates the modern era; `mode="legacy"` forces the handshake era.

```python
@pytest.fixture(params=["auto", "legacy"])
def mode(request): return request.param

async def test_tools_in_both_eras(mode):
    async with Client(mcp, mode=mode) as c:
        names = {t.name for t in await c.list_tools()}
        assert "my_tool" in names

async def test_modern_negotiated():
    async with Client(mcp) as c:
        assert c.protocol_version == "2026-07-28"
```
Also test, per interactive (MRTR) tool: accept path, decline/cancel path, and that **no side effect happens on the first round** (assert on your fake/mocked backend). Supply an `elicitation_handler` to the `Client` to script the answers. `pytest-asyncio` with `asyncio_mode = auto` (pytest.ini) keeps tests terse.

## V3 Wire probe (real HTTP)
Start the server on a real port, then:
```bash
python <skill>/scripts/probe_mcp_server.py http://127.0.0.1:8000/mcp
python <skill>/scripts/probe_mcp_server.py URL --header "Authorization: Bearer $TOKEN"
python <skill>/scripts/probe_mcp_server.py URL --call safe_readonly_tool --args '{"q":"x"}'
```
Only use `--call` on tools without side effects (or against a sandbox). For stdio servers use `fastmcp inspect server.py`, `fastmcp list`/`fastmcp call`, and the V2 tests; or send `server/discover` over stdin and expect a `DiscoverResult`.

Manual one-liner (useful in CI logs or when Python isn't handy):
```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: server/discover' \
  -d '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{
        "io.modelcontextprotocol/protocolVersion":"2026-07-28",
        "io.modelcontextprotocol/clientInfo":{"name":"curl","version":"1"},
        "io.modelcontextprotocol/clientCapabilities":{}}}}'
```
Expect `"resultType":"complete"`, `supportedVersions` containing `2026-07-28`, and **no `Mcp-Session-Id` response header** (`curl -i`).

## V4 Real-client smoke test
```python
import asyncio
from fastmcp import Client
async def main():
    async with Client("http://127.0.0.1:8000/mcp") as c:      # auto
        print(c.protocol_version, c.server_info, c.server_capabilities)
        print([t.name for t in await c.list_tools()])
asyncio.run(main())
```
And, if you claim legacy support, repeat with `mode="legacy"` and expect `2025-11-25`. If the user has a specific host app (Claude Desktop, Cursor, VS Code), tell them to add the server there and confirm tools appear — you cannot do that for them.

## V5 Reading probe output

| Probe line | If it fails / warns, the likely cause |
|---|---|
| `server/discover answers` FAIL | Not a modern server (SDK v1 / FastMCP ≤3), wrong URL path, or missing auth header |
| `advertises 2026-07-28` FAIL | Server answers discover but doesn't list the version; framework too old or misconfigured |
| `tools/list has ttlMs + cacheScope` FAIL | Hand-rolled/low-level server not emitting the required `CacheableResult` fields |
| `deterministic order` FAIL | Tools stored in a set/dict built from unordered source; sort by name |
| `unsupported version -> -32022` FAIL | Server ignores/never validates the version; may be echoing whatever the client sends |
| `Mcp-Method/body mismatch -> -32020` FAIL | Server (or its proxy) doesn't validate headers vs body — a security issue behind gateways |
| `request without Mcp-* headers is rejected` FAIL | Server accepts un-headered requests as modern |
| `no Mcp-Session-Id on modern responses` FAIL | Sessions still minted for modern requests (stateful config such as `stateless_http=False` in some stacks, or a gateway adding it) |
| `foreign Origin rejected` WARN | No Origin validation. Enable it (FastMCP `http_host_origin_protection`) or confirm a gateway enforces it |
| `legacy initialize` info "accepted" | Dual-era server (desired for mixed clients) |
| `legacy initialize` info "modern-only" | Legacy clients will fail; confirm that's intended |

Expected on a stock FastMCP 4.0.11 server (verified 2026-10-08): everything PASS except the Origin WARN and `ttlMs=0` info lines (until `cache_ttl` is set). A request lacking the modern headers reaches the *legacy* path and receives a 400 plus a minted session header — that is the dual-era design, not a leak.

## V6 Freshness: is there something newer than this skill knows?
This skill's snapshot: **MCP 2026-07-28 is the latest revision; FastMCP 4.0.11 and MCP Python SDK 2.3.0 as of 2026-10-08.** Spec revisions are date-stamped and will keep coming. Before telling the user "this is the latest", check — don't assume:
1. Spec: open https://modelcontextprotocol.io/specification (home links the latest) and the repo's tags `github.com/modelcontextprotocol/modelcontextprotocol/releases`. If a revision dated after 2026-07-28 exists, say so plainly, fetch its changelog, and treat this skill's rules as the *previous* baseline.
2. FastMCP: `pip index versions fastmcp` or https://pypi.org/project/fastmcp/ and https://gofastmcp.com/changelog.
3. SDK: `pip index versions mcp`.
4. Deprecations scheduled for removal: https://modelcontextprotocol.io/specification/2026-07-28/deprecated (Roots/Sampling/Logging earliest removal: first revision on/after 2027-07-28).
If you have no web access, state the snapshot date and that you couldn't re-check.

## V7 Source index and fetch order
When something isn't covered here or you doubt a claim, consult in this order. Append `.md` to Mintlify docs URLs for clean markdown; both sites publish an `llms.txt` index.

1. **Spec (normative):** `https://modelcontextprotocol.io/specification/2026-07-28/…`
   - `changelog` · `basic/index` (`_meta`, error codes) · `basic/versioning` · `basic/transports/streamable-http` · `basic/transports/stdio` · `basic/patterns/mrtr` · `basic/patterns/subscriptions` · `server/discover` · `server/utilities/caching` · `server/tools` · `basic/authorization` · `deprecated` · `schema`
   - Index: `https://modelcontextprotocol.io/llms.txt`
2. **Release narrative:** `https://blog.modelcontextprotocol.io/posts/2026-07-28/`
3. **FastMCP docs:** `https://gofastmcp.com` — `getting-started/whats-new`, `getting-started/upgrading/from-fastmcp-3`, `servers/elicitation`, `servers/tasks`, `servers/sessions`, `deployment/http`, `more/settings`; index `gofastmcp.com/llms.txt`; release notes `gofastmcp.com/changelog`; repo `github.com/PrefectHQ/fastmcp`.
4. **Python SDK v2:** `github.com/modelcontextprotocol/python-sdk` (migration notes).
5. **Your own installed code:** `python -c "import fastmcp, inspect; ..."`, `fastmcp --help`, `help(Context)`. The installed version is the ground truth for what *this project* can do.

Community blog posts help with pitfalls but have been wrong in details (for example one claimed `fastmcp.__version__` was removed; it exists in 4.0.11). Confirm against the installed package or official docs before you encode a community claim.

## V8 Evidence checklist for your final answer
- [ ] Stack identified (which FastMCP/SDK, versions installed)
- [ ] Target era(s) stated (modern / dual-era) and why
- [ ] Audit output: counts and any intentionally-ignored findings
- [ ] Test results in both client modes
- [ ] Probe summary (FAIL/WARN counts, the notable lines)
- [ ] Anything unverifiable (no network, no client app) said explicitly
- [ ] Latest-version check done or its absence disclosed