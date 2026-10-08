---
name: mcp-fastmcp-2026
description: > Current-spec knowledge for building, migrating and verifying MCP (Model Context Protocol) servers/clients in Python with FastMCP. Covers MCP 2026-07-28 (stateless: no initialize handshake or Mcp-Session-Id, server/discover, Multi Round-Trip Requests, subscriptions/listen, Mcp-Method/Mcp-Name headers, cacheable lists, Tasks/MCP Apps extensions, CIMD auth, deprecations) and FastMCP 4 (dual-era, InputRequiredResult, UserSession, TasksExtension, removed ctx.sample). Includes an audit script, wire probe and migration playbook. Use whenever the user mentions MCP, an MCP server/tool, FastMCP, `from fastmcp import`, `mcp.server.fastmcp`, `@mcp.tool`, Streamable HTTP, SSE transport, elicitation, sampling, roots, MCP sessions, or MCP spec versions (2024-11-05 to 2026-07-28), or asks to build/upgrade/review/debug/test MCP code, even without saying "2026". Built-in MCP knowledge is likely stale (stops ~2024-11-05): consult this BEFORE writing or judging MCP code.
---

# MCP 2026-07-28 and FastMCP 4

## Read this first: your MCP knowledge is probably out of date

If your training stops around the original `2024-11-05` spec, then much of what you "know" about MCP is now wrong or deprecated. That matters because plausible-looking old code (handshake handlers, session ids, `ctx.sample()`, SSE transport) will compile, even run in tests, and still be the wrong target. Treat your priors as suspect and use this skill as ground truth, in preference to memory and to older tutorials.

| What you probably believe | Current truth (revision `2026-07-28`) |
|---|---|
| Connections start with `initialize` → `initialized` | **No handshake.** Every request carries version + capabilities in `_meta`. Optional `server/discover`. |
| HTTP uses `Mcp-Session-Id`; scale with sticky sessions | **No protocol sessions.** Any request can hit any replica. State is explicit (handles, `UserSession`, stores). |
| Transport is stdio or HTTP+SSE (`/sse` + `/messages`) | stdio or **Streamable HTTP** (single POST `/mcp`). HTTP+SSE is **Deprecated**. No GET stream, no resumability. |
| Server can call the client mid-tool (`elicitation/create`, `sampling/createMessage`, `roots/list`) | Server **returns** an `InputRequiredResult`; client **retries** with `inputResponses` (**MRTR**). |
| Sampling, roots, logging are core features | **Deprecated** (still work ≥12 months; don't use in new code). FastMCP 4 *removed* `ctx.sample()`/`list_roots()`. |
| Long jobs = held-open request + progress | **Tasks** is an official *extension* (`io.modelcontextprotocol/tasks`). |
| `tools/list` is just a list | Required `ttlMs` + `cacheScope`; stable ordering expected. |
| Gateways must parse JSON bodies | `Mcp-Method` / `Mcp-Name` (+ `Mcp-Param-*`) headers are required and validated against the body. |
| OAuth client registration = DCR | **CIMD** preferred; DCR Deprecated; `iss` (RFC 9207) validation. |
| "FastMCP" = `from mcp.server.fastmcp import FastMCP` | That's FastMCP **1.0**, bundled in SDK v1, **gone in `mcp` v2**. Modern = standalone `fastmcp>=4` (or SDK v2 `MCPServer`). |

Intermediate revisions you may also have missed: `2025-03-26` (Streamable HTTP, OAuth 2.1), `2025-06-18` (structured output, elicitation), `2025-11-25` (experimental Tasks, URL elicitation, CIMD). See `references/version-history.md`.

## Snapshot and the freshness rule

Snapshot date **2026-10-08**: latest spec **2026-07-28**; **FastMCP 4.0.11**; **MCP Python SDK 2.3.0**. Spec revisions are date-stamped and keep coming, and FastMCP ships frequent patches. So: never tell the user "this is the latest" without checking (`references/verification.md` §V6). If a newer revision or release exists, say so and fetch its changelog; this skill then becomes the previous baseline. If a doc or the installed package contradicts this skill, **they win**. Note the discrepancy to the user.

## Step 0: identify the stack before touching code

"FastMCP" is overloaded. Run the audit (it prints "Detected stack"), or grep imports:

| Import found | Meaning | Can speak 2026-07-28? |
|---|---|---|
| `from fastmcp import FastMCP` | Standalone FastMCP (PrefectHQ, gofastmcp.com) | v4+ yes; v2/v3 no |
| `from mcp.server.fastmcp import FastMCP` | FastMCP 1.0 inside SDK v1 | No (and removed in `mcp` v2) |
| `from mcp.server.mcpserver import MCPServer` | SDK v2 high-level server | Yes |
| `from mcp.server import Server` | SDK low-level | Only with `mcp>=2` and lots of manual spec work |

Also read the lockfile/`pyproject.toml` pins and `pip list | grep -i mcp` for the *installed* versions; they determine what's actually possible. If the user says "FastMCP" but the code uses the bundled 1.0, tell them: it's a different library.

## The model in one screen: the layers

Full detail: `references/spec-layers.md`. Think of the current MCP as these layers:

0. **Revision & lifecycle**: date-versioned; eras: *legacy* (≤2025-11-25, handshake),
   *modern* (≥2026-07-28), *dual-era* (both). Deprecations have a ≥12-month window.
1. **Message**: JSON-RPC 2.0; reserved `_meta` keys (`io.modelcontextprotocol/protocolVersion`, `clientCapabilities`, `clientInfo`, `serverInfo`, `logLevel`, trace context); every result has `resultType` (`complete` | `input_required`); MCP error codes `-32020..-32099` (`-32020` HeaderMismatch, `-32021` MissingRequiredClientCapability, `-32022` UnsupportedProtocolVersion); resource-not-found is `-32602`.
2. **Negotiation & discovery**: version declared per request; mismatch → `-32022` with `supported[]`; `server/discover` (servers MUST implement).
3. **Transport**: stdio; Streamable HTTP = one POST endpoint, per-request JSON or SSE response, mandatory `MCP-Protocol-Version`/`Mcp-Method`/`Mcp-Name` headers validated against the body, closing the stream = cancel, `Origin` validation, bind localhost.
4. **Interaction patterns**: MRTR for server→client needs; `subscriptions/listen` for change notifications; explicit handles for app state; progress on the request's own stream.
5. **Server features**: tools/resources/prompts (+ completion), cacheable lists, stable order, any-JSON-Schema-2020-12 schemas, `structuredContent` any JSON.
6. **Client features**: elicitation (form/URL) via MRTR; roots/sampling/logging Deprecated.
7. **Extensions**: `capabilities.extensions` map; **Tasks**, **MCP Apps** (`…/ui`), Enterprise-Managed Authorization; fall back to core if the peer lacks the extension.
8. **Authorization**: OAuth 2.1 + PKCE, protected-resource metadata, resource indicators, `iss` validation, CIMD over DCR, issuer-bound credentials, scope step-up.

## Rules for writing code (with the reasons)

1. **Target `2026-07-28`, serve both eras with FastMCP 4.** One deployment negotiates per connection, so you never need to fork or gate by client version. Reason: old clients keep working and new ones get the scalable protocol.
2. **No handshake logic and no session ids.** Don't write `initialize` handlers, `on_initialize` hooks, or key anything on `Mcp-Session-Id`. Reason: they don't exist in the modern era, and code depending on them silently breaks behind a load balancer.
3. **State is explicit.** Cross-call state = a handle the tool mints and the model passes back, or `UserSession`/`SessionId` (requires auth) with a *shared* store when replicated. Never module-level dicts keyed by connection. Reason: any replica serves any request; hidden state also can't be seen or threaded by the model.
4. **Server needs user input → return `InputRequiredResult`.** Don't call `ctx.elicit()` in new modern code. The tool function **re-runs from the top each round**, so keep the part before `ctx.input_responses` free of side effects (confirm *before* acting). Multi-replica: all replicas share one `request_state_security` key (≥32 bytes).
5. **Don't adopt Deprecated or removed features.** No sampling, roots, protocol logging, `transport="sse"`, DCR, or `includeContext` in new code. Replacements: call your own LLM; pass paths as tool args; stderr/OpenTelemetry; Streamable HTTP; CIMD. Reason: they're scheduled for removal and FastMCP 4 already removed some.
6. **Long-running work → Tasks extension** (`fastmcp[tasks]`, `mcp.add_extension(TasksExtension())`, `@mcp.tool(task=True)`), not a held-open call.
7. **Set cache hints** for catalogs that rarely change (`FastMCP(cache_ttl=300,cache_scope="public")`; use `"private"` if lists vary per user). Defaults are`ttlMs=0`/`private`: correct but uncacheable.
8. **HTTP serving**: `mcp.run(transport="http", host=..., port=...)` (host/port belong on `run()`); no GET/SSE endpoints, no resumability logic; enable Origin validation for anything a browser can reach; bind `127.0.0.1` when local; authenticate remote servers with the framework's auth providers, never hand-rolled token checks.
9. **Use SDK v2 shapes**: protocol types come from `mcp.types` with **snake_case** fields (`input_schema`, `is_error`); `MCPError(code, message, data=None)`; imports moved (see `references/fastmcp-4.md` §10).
10. **Pin deliberately.** Apps: `fastmcp==4.0.x` (the version you tested). Libraries: `fastmcp>=4.0.0`. Fresh venv when upgrading from 3.x; add `fastmcp[tasks]` only if used.
11. **Don't invent API.** If you aren't certain a FastMCP/SDK symbol exists in the installed version, check it (`python -c "import …"`, `help()`, `fastmcp --help`) or fetch the docs (`gofastmcp.com/llms.txt`) before writing it.
12. **Be honest about legacy clients.** Dropping the 2024-11-05 HTTP+SSE transport can strand old clients. Surface that trade-off; don't decide silently.

## Core patterns (verified on FastMCP 4.0.11)

Minimal modern server:
```python
from fastmcp import FastMCP

mcp = FastMCP("Catalog", instructions="Product catalog lookups.",
              cache_ttl=300, cache_scope="public")

@mcp.tool
def search(query: str, limit: int = 10) -> list[dict]:
    """Search products by name."""
    ...

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000)   # or mcp.run() for stdio
```

Confirm-before-acting (MRTR), the replacement for `ctx.elicit()`:
```python
from fastmcp import Context
from mcp.types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult

@mcp.tool
async def delete_project(name: str, ctx: Context) -> str | InputRequiredResult:
    """Delete a project after the user confirms."""
    answers = ctx.input_responses
    if answers is None:                                  # round 1: ask, no side effects
        params = ElicitRequestFormParams(
            message=f"Really delete {name}?",
            requested_schema={"type": "object",
                              "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
        return InputRequiredResult(result_type="input_required",
            input_requests={"confirm": ElicitRequest(method="elicitation/create", params=params)})
    r = answers["confirm"]                               # round 2: act
    if r.action != "accept" or not (r.content or {}).get("ok"):
        return "Cancelled."
    delete_it(name)
    return f"Deleted {name}."
```
More (per-user state, tasks, completions, auth, settings, removed APIs, testing) →
`references/fastmcp-4.md`.

## Working procedure

**Building a new server**
1. Step 0 (confirm it's standalone `fastmcp>=4`); create/activate a fresh venv; install.
2. Write tools with type hints + docstrings (they become the schema and the model-facing
   description). Add cache hints, auth if remote, tasks if long-running, MRTR if
   confirmation/input is needed. Avoid every item in Rule 5.
3. Run the verification workflow below. Report evidence.

**Upgrading / reviewing an existing server**: follow `references/migration-checklist.md`
(baseline → identify → environment → code → transport → verify → report).

**Answering a question or reviewing a snippet**: cross-check against the "stale beliefs" table and the registry at the end of `references/spec-layers.md`. If the snippet uses an old pattern, say what era it belongs to, why it's wrong for the modern one, and give the current replacement.

## Verification (do not skip; "it starts" proves nothing)

From the skill directory (replace paths):
```bash
python scripts/audit_mcp_project.py <project>                # static: legacy/removed patterns (exit 1 on HIGH)
pytest -q                                                    # tests: run in BOTH Client modes ("auto" and "legacy")
python scripts/probe_mcp_server.py http://127.0.0.1:8000/mcp # wire: real HTTP conformance checks (exit 1 on FAIL)
```
- The probe checks `server/discover`, `resultType`, `ttlMs`/`cacheScope`, deterministic order, `-32022` on a bad version, `-32020` on header/body mismatch, rejection of header-less requests, absence of `Mcp-Session-Id` on modern responses, legacy `initialize` (info), and foreign-`Origin` handling. Use `--call TOOL --args JSON` only on side-effect-free tools. Details and how to read each line: `references/verification.md`.
- Expected on a stock FastMCP 4 server: all PASS except an Origin WARN (opt-in feature) and `ttlMs=0` notes until `cache_ttl` is set. A header-less request reaching the legacy path and getting a session id is the dual-era design, not a bug.
- Stdio servers: `fastmcp inspect server.py` plus in-process `Client` tests.
- Finish with the evidence checklist (`references/verification.md` §V8): stack, target era(s), audit counts, test results in both modes, probe summary, anything you could not verify, and whether you checked for a newer spec/FastMCP release.

## Reference map (read only what the task needs)

| File | Read when |
|---|---|
| `references/spec-layers.md` | You need the exact rule: `_meta` keys, error codes, headers, MRTR mechanics, extensions, auth, deprecation registry |
| `references/version-history.md` | Reading old code/tutorials; explaining what changed since 2024-11-05 |
| `references/fastmcp-4.md` | Writing FastMCP code: patterns, state, tasks, auth, removed/renamed API table, settings, testing |
| `references/migration-checklist.md` | Upgrading an existing project; decision table for replacing removed patterns |
| `references/verification.md` | Running/interpreting the checks; freshness check; source index and fetch order |
| `scripts/audit_mcp_project.py` | Static scan + stack detection (stdlib only) |
| `scripts/probe_mcp_server.py` | Wire-level conformance probe for HTTP servers (stdlib only) |
