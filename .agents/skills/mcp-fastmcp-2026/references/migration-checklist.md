# Migrating an existing server to MCP 2026-07-28 / FastMCP 4

Use when the project already exists (typically written against 2024-11-05–era examples). Work in this order — each step makes the next one's failures easier to read.

## Contents
- 0. Decide the target and the support promise
- 1. Baseline and branch
- 2. Identify the stack and starting point
- 3. Environment
- 4. Code changes by starting point
- 5. Replace removed patterns (decision table)
- 6. Transport and deployment
- 7. Verify
- 8. Report

## 0. Decide the target and the support promise
Ask (or infer from the repo/README) *who connects*:
- Only modern clients (current Claude/Cursor/agent SDKs built after 2026-07-28)? → target modern; dual-era is still free with FastMCP 4, so keep it unless it costs you something.
- Older clients still in the wild (2024-11-05 / 2025-x)? → **keep dual-era**. Don't gate by client version. FastMCP 4 negotiates per connection. Note the 2024-11-05 HTTP+SSE transport is Deprecated; clients that only speak that need a deliberate decision (run a legacy SSE endpoint separately, or ask them to upgrade) — state this trade-off to the user rather than silently dropping them.

## 1. Baseline and branch
```bash
git switch -c mcp-2026-07-28
# record current behaviour before touching anything
python scripts/audit_mcp_project.py . > audit-before.txt
python scripts/probe_mcp_server.py http://127.0.0.1:8000/mcp > probe-before.txt   # if it can run
pytest -q                                                                           # if tests exist
```

## 2. Identify the stack and starting point
The audit prints "Detected stack". Starting points:

| You have | Path |
|---|---|
| `from mcp.server.fastmcp import FastMCP` (SDK v1 bundled) | → standalone `fastmcp`: change the import; move host/port to `run()`; prompts must return `Message`/str; then continue at 4A |
| `from mcp.server import Server` (low-level, `@server.list_tools()` etc.) | → FastMCP: one `@mcp.tool` per old tool; delete JSON-schema dicts (generated from type hints). FastMCP docs have an "upgrading from the low-level SDK" guide with a copyable agent prompt |
| `fastmcp` 2.x | → 3 first (official 2→3 guide), then 4A |
| `fastmcp` 3.x | → 4A |
| `mcp.server.mcpserver.MCPServer` (SDK v2) | already modern-capable; consider FastMCP for composition/auth/middleware; otherwise see SDK v2 migration notes |

Going 2 → 4 means *both* hops, in order. Official guides (fetch before editing): `gofastmcp.com/getting-started/upgrading/from-fastmcp-2`, `.../from-fastmcp-3`, `.../from-mcp-sdk`, `.../from-low-level-sdk` — each also publishes a copy-paste "audit my app with a coding agent" prompt; use it as a second opinion.

## 3. Environment
- Recreate the virtualenv (don't `pip install -U` over a 3.x env).
- `uv add "fastmcp>=4,<5"` (add `"fastmcp[tasks]"` if tasks are used). Remove any `mcp<2` pin. Check `pydantic>=2.12`.
- Python: whichever FastMCP 4 supports (3.12 verified; check PyPI classifiers).
- `uv lock` / `pip freeze` the result; commit the lockfile.

## 4. Code changes by starting point
### 4A. FastMCP 3 → 4 (the common case)
1. Grep-and-fix the audit's R001–R016 findings (table in `fastmcp-4.md` §10).
2. Anything using `ctx.sample*` / `list_roots` → redesign (see §5).
3. `ctx.elicit()` → MRTR (`fastmcp-4.md` §5).
4. `task=True` → add `TasksExtension` (`fastmcp-4.md` §7).
5. `Client(...)`: leave `mode="auto"`; pin `mode="legacy"` only where session features are
   genuinely required.
6. Set `FASTMCP_MCP_CAMELCASE_COMPAT=false` in tests; fix remaining camelCase reads.
7. `FASTMCP_CHECK_FOR_UPDATES=off` in CI/containers; decide `FASTMCP_TELEMETRY_MODE`.

### 4B. If you're keeping the low-level SDK
Make sure `mcp>=2`; implement `server/discover`; return `resultType` on results; return `InputRequiredResult` for MRTR; send `ttlMs`/`cacheScope` on list results; validate `Mcp-Method`/`Mcp-Name`/`MCP-Protocol-Version` against the body. That's a lot of spec surface — this is the main reason to prefer FastMCP.

## 5. Replace removed patterns (decision table)

| Old pattern | Replace with | Notes |
|---|---|---|
| `await ctx.elicit("Sure?", response_type=bool)` | `InputRequiredResult` with an `elicitation/create` request; read `ctx.input_responses` | Top half of the tool must be side-effect free |
| `await ctx.sample(prompt)` to get a completion | Direct call to your LLM provider SDK (you own model, key, cost) | Or MRTR carrying a sampling request if you truly want the caller's model — but Sampling is Deprecated |
| `await ctx.list_roots()` | Tool parameter / resource URI / server config for the directory | Roots is Deprecated |
| Per-connection state in globals keyed by session | `UserSession` (authenticated) or explicit handle arg + shared store | Never key on `Mcp-Session-Id` |
| Long request held open with progress pings | `@mcp.tool(task=True)` + `TasksExtension` | Redis/Valkey backend for durability |
| `resources/subscribe` handling | Nothing server-side beyond emitting updates; clients use `subscriptions/listen` | |
| "Tools changed" notifications relying on a GET stream | Same notifications; clients opt in via `subscriptions/listen`; set `cache_ttl` | |
| `logging/setLevel` + `notifications/message` | stderr (stdio) / OpenTelemetry | Only emitted if request sets `_meta` logLevel |
| Sticky sessions / shared session store at the load balancer | Remove; any replica serves any request | Keep a shared *request-state key* and *session_state_store* for app features |
| DCR-based OAuth client registration | CIMD-capable auth provider | DCR still works, Deprecated |
| `transport="sse"` | `transport="http"` | |

## 6. Transport and deployment
- Single `/mcp` POST endpoint. Remove any GET/SSE route handlers, session DELETE handler, `Last-Event-ID` handling, and sticky-session config.
- Reverse proxy: disable response buffering for SSE (`X-Accel-Buffering: no` is set by the server; ensure nginx/ingress honours it); raise idle timeouts for `subscriptions/listen`.
- Gateways can now route on `Mcp-Method` / `Mcp-Name` — update WAF/rate-limit rules to use headers instead of body parsing, and make sure intermediaries *forward* unknown `Mcp-Param-*` headers.
- Multi-replica: same `request_state_security` key on every replica; shared `session_state_store`; shared task backend.
- Browser-reachable or local-network servers: enable Origin validation and bind to `127.0.0.1` when local (spec MUST/SHOULD; FastMCP's Origin protection is opt-in).
- Containers: `FASTMCP_CHECK_FOR_UPDATES=off`.

## 7. Verify
Run the full workflow in `verification.md`. At minimum: audit clean of HIGH/MEDIUM, tests pass in both client modes, wire probe has 0 FAIL, and one real client smoke test.

## 8. Report
Give the user: the target era(s), what changed (grouped by the table in §5), anything deliberately left as legacy with the reason, probe output summary, remaining WARNs and what they'd need to do, and how to roll back (branch + previous pins).
