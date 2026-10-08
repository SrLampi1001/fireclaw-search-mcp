# FastMCP 4 — working reference

FastMCP 4 is "the FastMCP release for the new MCP": full support for the 2026-07-28 revision on top of the rewritten MCP Python SDK v2, with per-connection negotiation so old clients keep working. Snippets marked **(verified)** were executed against `fastmcp 4.0.11` + `mcp 2.3.0` on 2026-10-08. Everything else is from the official docs (https://gofastmcp.com — append `.md` to a page URL for clean markdown, and see `/llms.txt` for the full index).

## Contents
1. Which "FastMCP" is this? (package disambiguation)
2. Install, pins, and the in-place-upgrade trap
3. Minimal server and running it
4. Dual-era behaviour and the `Client` modes
5. Interactive tools (MRTR) — the replacement for `ctx.elicit()`
6. State without sessions
7. Background tasks (extension)
8. Caching hints
9. Completions, extensions, auth
10. What was removed / renamed (3 → 4)
11. Settings worth knowing
12. Testing patterns

---

## 1. Which "FastMCP" is this?

Four things share the name. Identify which one the project uses *before* editing.

| Import | What it is | 2026-07-28 support |
|---|---|---|
| `from fastmcp import FastMCP` | **Standalone FastMCP** (PrefectHQ/fastmcp, gofastmcp.com) | v4+ ✔ (v2/v3 ✘) |
| `from mcp.server.fastmcp import FastMCP` | FastMCP **1.0**, bundled in MCP Python SDK **v1** | ✘ — and the module is **gone in `mcp` v2** |
| `from mcp.server.mcpserver import MCPServer` | SDK **v2** high-level server (successor to bundled FastMCP 1.0) | ✔ |
| `from mcp.server import Server` / `mcp.server.lowlevel` | SDK low-level server | depends on SDK major version |

Gotcha: `pip install mcp` now resolves to **2.x**. A project that still imports `mcp.server.fastmcp` and doesn't pin `mcp<2` will break on a fresh install. Official guidance for apps on the bundled 1.0: either move to standalone `fastmcp` (usually a single import change) or to `MCPServer`.

FastMCP 4 depends on `mcp>=2,<3`. The `fastmcp` distribution is a thin package that pulls `fastmcp-slim[client,server]` **(secondary)**.

## 2. Install, pins, and the in-place-upgrade trap

```bash
# fresh venv, recommended
uv add "fastmcp>=4,<5"            # or: pip install "fastmcp>=4,<5"
uv add "fastmcp[tasks]"           # only if you use background tasks
```

- **App** → pin the exact tested version (`fastmcp==4.0.x`). **Library** → floor it (`fastmcp>=4.0.0`).
- Check the real current release on PyPI before pinning; FastMCP ships frequent 4.0.x patches (4.0.11 on 2026-10-04).
- **Trap (secondary report):** `pip install -U fastmcp` on top of a 3.x environment can leave an empty importable shell because the package split into extras didn't re-resolve. If `from fastmcp import Client` raises `ImportError`, uninstall `fastmcp` and `fastmcp-slim` and reinstall, or recreate the venv.
- Floors reported for 4.x: `pydantic>=2.12` (secondary).
- Version at runtime: `importlib.metadata.version("fastmcp")` (works everywhere; `fastmcp.__version__` exists in 4.0.11 **(verified)** but don't depend on it).
- `fastmcp version` prints version + platform.

## 3. Minimal server and running it **(verified)**

```python
from fastmcp import FastMCP

mcp = FastMCP("Weather", instructions="Weather lookups.", cache_ttl=300, cache_scope="public")

@mcp.tool
def get_forecast(city: str) -> dict:
    """Return the forecast for a city."""
    ...

if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000)   # Streamable HTTP at /mcp
    # mcp.run()                                               # stdio (default)
```

- `transport="http"` serves Streamable HTTP at `/mcp`. `transport="streamable-http"` also appeared in 3.x-era docs; prefer `"http"` and run `fastmcp run --help` if unsure.
- Do **not** use `transport="sse"` for new work (deprecated HTTP+SSE).
- Transport settings (host/port) go on `run()`, not on `FastMCP(...)`.
- CLI: `fastmcp run server.py`, `fastmcp inspect server.py`, `fastmcp list <url>`, `fastmcp call <url> <tool> ...`, `fastmcp dev` **(verified listing)**.

## 4. Dual-era behaviour and the `Client` modes **(verified)**

One FastMCP 4 server answers both eras, chosen **per connection**:
- Modern request (has `_meta` protocolVersion + required headers) → stateless handling.
- `initialize` → legacy handshake semantics; response protocolVersion was `2025-11-25`.
- Observed: a stock server's `server/discover` listed `supportedVersions: ["2026-07-28"]` only, while `initialize` still worked (inference: the list describes modern support; legacy clients are recognised by `initialize`).

```python
from fastmcp import Client

async with Client("https://example.com/mcp") as c:          # mode="auto": probe modern, fall back
    c.protocol_version      # "2026-07-28" (or "2025-11-25" against an old server)
    c.server_info; c.server_capabilities; c.instructions

legacy = Client("https://example.com/mcp", mode="legacy")   # force handshake era
```

- `Client(mcp_instance)` (in-process) also negotiates modern by default **(verified)**.
- Only pin `mode="legacy"` if the *application* needs session back-channel features (session state, `on_initialize`, server-initiated `ctx.elicit()`).
- `ClientGroup` aggregates several servers behind one tool catalog with `{server}_{tool}` namespacing; each member negotiates independently.
- `Client("server.py")` (bare string → stdio subprocess) now warns; pass a `Path`. String form removal planned for FastMCP 5.
- `StreamableHttpTransport` dropped `sse_read_timeout=`; pass `timeout=` to `Client` (secondary).

## 5. Interactive tools (MRTR) **(verified end-to-end)**

On the modern protocol a tool that needs input **returns** an `InputRequiredResult`; the client fulfils it and calls the tool again; the tool runs from the top and reads `ctx.input_responses`.

```python
import os
from fastmcp import Context, FastMCP
from mcp.server.request_state import RequestStateSecurity
from mcp.types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult

mcp = FastMCP(
    "Admin",
    # Single process: omit (an automatic process-local key is used).
    # Multiple replicas: EVERY replica must get the same >=32-byte secret.
    request_state_security=RequestStateSecurity(keys=[os.environ["REQUEST_STATE_KEY"].encode()]),
)

@mcp.tool
async def delete_project(name: str, ctx: Context) -> str | InputRequiredResult:
    """Delete a project after explicit user confirmation."""
    answers = ctx.input_responses
    if answers is None:                       # round 1: ask. NO side effects up here.
        params = ElicitRequestFormParams(
            message=f"Really delete {name}?",
            requested_schema={"type": "object",
                              "properties": {"ok": {"type": "boolean"}},
                              "required": ["ok"]},
        )
        return InputRequiredResult(
            result_type="input_required",
            input_requests={"confirm": ElicitRequest(method="elicitation/create", params=params)},
        )
    r = answers["confirm"]                    # round 2: act
    if r.action != "accept" or not r.content or not r.content.get("ok"):
        return "Cancelled."
    do_the_deletion(name)
    return f"Deleted {name}."
```

Rules:
- Return annotation `str | InputRequiredResult` (or your normal type | InputRequiredResult).
- **Idempotent top half.** The function re-executes each round.
- A FastMCP `Client` drives the loop through its normal `elicitation_handler`, so client code just sees the final result **(verified)**.
- `ctx.elicit()` still exists but is for **handshake-era connections**; it requires a `response_type` and fails on modern connections. If you must support both eras in one tool, branch on `ctx.request_context.protocol_version` **(secondary)**, or just use MRTR for both if the clients you target are modern.
- Sampling and roots use the same return-and-resume pattern (an `InputRequiredResult` carrying a sampling or roots request) — but both features are Deprecated; prefer calling your own LLM / passing paths as arguments.

## 6. State without sessions

Protocol sessions are gone; application state is explicit.

```python
from fastmcp import FastMCP
from fastmcp.server.sessions import UserSession      # also: SessionId

mcp = FastMCP("Assistant")        # pass session_state_store=<shared store> for >1 replica

@mcp.tool
async def remember(fact: str, session: UserSession) -> str:
    facts = await session.get("facts", default=[])
    facts.append(fact)
    await session.set("facts", facts)
    return f"Remembered {len(facts)} facts."
```

- `UserSession` is injected like `Context`, never appears in the tool schema, and is keyed by the **authenticated** user — it **requires authentication**.
- Need several buckets per user (carts, conversations)? Use `SessionId`, which exposes the handle as an explicit string argument the model passes back.
- Default store is in-memory and process-local. Use a shared persistent `session_state_store` for restarts/replicas.
- `ctx.set_state()` does **not** persist across requests on modern connections (secondary).
- Mounted servers have isolated state stores (v3 change).

## 7. Background tasks (extension) **(verified import/registration)**

```python
from fastmcp import FastMCP
from fastmcp_tasks import TasksExtension          # pip install "fastmcp[tasks]"

mcp = FastMCP("Jobs")
mcp.add_extension(TasksExtension())               # in-memory, single-process by default

@mcp.tool(task=True)                              # tools only (not resources/prompts)
async def build_report(n: int) -> str:
    ...
```

- Without `add_extension(TasksExtension())`, `task=True` tools fail at startup.
- Production durability: configure the Redis/Valkey backend (docket-backed).
- `fastmcp.Client` handles the task handle and polling, so `call_tool` looks identical to an inline call.

## 8. Caching hints **(verified)**

```python
mcp = FastMCP("Catalog", cache_ttl=300, cache_scope="public")   # ttlMs=300000
```
- Stock defaults: `ttlMs: 0`, `cacheScope: "private"` — correct but gives clients no caching benefit. Set a TTL for catalogs that rarely change; use `"private"` whenever the list differs by user or tenant.
- Client-side: `KeyValueResponseCacheStore` can back the client cache with Redis. Cache entries are partitioned by requested component version.

## 9. Completions, extensions, auth

```python
from mcp.types import PromptReference

@mcp.completion
def complete(ref, argument, context):
    if isinstance(ref, PromptReference) and argument.name == "theme":
        return [o for o in ["nature", "love", "adventure"] if o.startswith(argument.value)]
    return None
```
- Registering a completion handler advertises the capability.
- `mcp.add_extension(...)` is the FastMCP-native server-extension API (advertise capabilities, add methods, intercept `tools/call`, own lifespan). `Client(extensions=...)` is the client counterpart.
- Auth: use `FastMCP(..., auth=<provider>)`. v4 additions: identity assertion (`IdentityAssertion(trusted_issuers=[...])` on `OAuthProxy`, **beta**), `require_roles`, scope step-up (`InsufficientScopeError` naming needed scopes), `ClientCredentialsOAuthProvider` for machine-to-machine, DCR `application_type`.
  Default OAuth client storage is `FileTreeStore` (CVE-2025-69872 fix in v3). Don't hand-roll token validation.
- Resource templates reject path traversal, absolute paths and null bytes by default.

## 10. Removed / renamed (3 → 4)

| 3.x | 4.x |
|---|---|
| `ctx.sample()`, `ctx.sample_step()`, `ctx.list_roots()`, `FastMCP(sampling_handler=)` | **Removed** in every era (fail fast). Call an LLM directly, or return an `InputRequiredResult`. **(verified: attributes absent)** |
| `ctx.elicit()` | Legacy-era only; use MRTR for modern |
| `@mcp.tool(task=True)` alone | + `fastmcp[tasks]` + `add_extension(TasksExtension())` |
| `from fastmcp.tools.tool import Tool, ToolResult` | `from fastmcp.tools import Tool, ToolResult` |
| `from fastmcp.resources.resource import Resource` | `from fastmcp.resources import Resource` |
| protocol types from `fastmcp.types` | `from mcp.types import ...` (`fastmcp.types` = FastMCP-only types) |
| `mcp.as_proxy(sub)` / `FastMCP.as_proxy` | `create_proxy(sub)` — `from fastmcp.server import create_proxy`; `as_proxy` and `import_server` no longer exist on `FastMCP` **(verified)** |
| `mcp.import_server(sub)` | `mcp.mount(sub)` (live, not a snapshot) |
| `mcp.add_tool_transformation(n, cfg)` | `mcp.add_transform(ToolTransform({n: cfg}))` — `from fastmcp.server.transforms import ToolTransform` **(verified)** |
| `CachableToolResult` | `CacheableToolResult` (no alias) |
| `McpError(ErrorData(code=, message=))` | SDK v2 class is `mcp.shared.exceptions.MCPError(code, message, data=None)` (`McpError` import from `mcp.shared.exceptions` fails; `fastmcp.exceptions` still exposes both names) **(verified)** |
| `fastmcp.server.proxy`, `fastmcp.server.openapi`, `FastMCPOpenAPI` | `fastmcp.server.providers.proxy` / `.openapi` + `FastMCP(providers=[OpenAPIProvider(spec, client)])` |
| camelCase protocol model fields (`inputSchema`, `isError`) | snake_case (`input_schema`, `is_error`) in SDK v2; old reads bridged with a deprecation warning |
| `httpx` exceptions from FastMCP client calls | FastMCP 4 uses `httpx2` internally (installed alongside **(verified)**); so `except httpx.ConnectError` may silently stop matching — catch FastMCP/`httpx2` exceptions or `fastmcp.exceptions.ToolError` |

Keep deprecation warnings on (`FASTMCP_DEPRECATION_WARNINGS=true`, the default) during migration; set `fastmcp.settings.mcp_camelcase_compat = False` once to turn leftover camelCase reads into hard errors so you find them all.

## 11. Settings worth knowing (`FASTMCP_<NAME>` env vars; full list: gofastmcp.com/more/settings)
- `telemetry_mode` — default `native` (auto OpenTelemetry spans); `off`/`propagation_only`.
- `check_for_updates` — default `stable`; set `off` in CI/containers.
- `stateless_http` — new transport per request (sessionless deployments/Cloud Run).
- `http_host_origin_protection` — opt-in Host/Origin validation for Streamable HTTP.
  Turn it on for anything reachable from a browser context (spec requires Origin checks).
- `mask_error_details` — default `false`: error text passes through unless you raise an explicit `ToolError`/`ResourceError`/`PromptError`. Consider `true` in production.
(Names from secondary reports; confirm against the settings page.)

## 12. Testing patterns **(verified)**

```python
# test_server.py  (pytest-asyncio, asyncio_mode = auto)
import pytest
from fastmcp import Client
from server import mcp

@pytest.fixture(params=["auto", "legacy"])
def mode(request): return request.param

async def test_add_both_eras(mode):
    async with Client(mcp, mode=mode) as c:
        assert (await c.call_tool("add", {"a": 2, "b": 3})).data == 5

async def test_modern_negotiated():
    async with Client(mcp) as c:
        assert c.protocol_version == "2026-07-28"

async def test_confirmation_flow():
    async def handler(message, response_type, params, context):
        return response_type(ok=True)
    async with Client(mcp, elicitation_handler=handler) as c:
        assert "Deleted" in str(( await c.call_tool("delete_project", {"name": "x"})).data)
```
Test *both* eras if you claim dual-era support; test the cancel/decline path of every MRTR tool; test that nothing happens before confirmation.
