# MCP 2026-07-28 — layered reference

Read this when you need the *exact* rule behind something in SKILL.md, or when you are designing something the quick rules don't cover. Everything here is from the official spec/changelog unless marked **(verified)** (observed on a live FastMCP 4.0.11 server) or **(secondary)** (community source, double-check before relying on it).

Canonical sources: https://modelcontextprotocol.io/specification/2026-07-28 · changelog at `/changelog` · full index at https://modelcontextprotocol.io/llms.txt

## Contents
- Layer 0 — Revision model and lifecycle policy
- Layer 1 — Message layer (`_meta`, `resultType`, errors)
- Layer 2 — Version negotiation and discovery
- Layer 3 — Transports (stdio, Streamable HTTP, headers)
- Layer 4 — Interaction patterns (MRTR, subscriptions, state)
- Layer 5 — Server features (tools, resources, prompts, caching)
- Layer 6 — Client features (elicitation, roots, sampling, logging)
- Layer 7 — Extensions (Tasks, MCP Apps, EMA)
- Layer 8 — Authorization
- Deprecated / removed registry

---

## Layer 0 — Revision model and lifecycle policy

- Revisions are **date-versioned** (`2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`, `2026-07-28`). A newer date may exist by the time you read this; check the spec home page.
- Three eras of implementation (spec terminology):
  - **Legacy** — `2025-11-25` and earlier: `initialize` handshake, sessions.
  - **Modern** — `2026-07-28` and later: per-request `_meta`, no handshake.
  - **Dual-era** — supports both. FastMCP 4 is dual-era out of the box.
- **Feature lifecycle policy (SEP-2596):** features are Active → Deprecated → Removed, with a **minimum 12-month deprecation window** and a public registry of deprecated features. Deprecated ≠ broken: it still works, but new code should not adopt it.

## Layer 1 — Message layer

JSON-RPC 2.0 everywhere. No batching (batching was added in 2025-03-26 and removed again in 2025-06-18).

### Reserved `_meta` keys (on request `params._meta` or result `_meta`)

| Key | Direction | Meaning |
|---|---|---|
| `io.modelcontextprotocol/protocolVersion` | request | **Required.** Version the client speaks. |
| `io.modelcontextprotocol/clientCapabilities` | request | **Required.** Replaces capability exchange at init. |
| `io.modelcontextprotocol/clientInfo` | request | SHOULD be sent each request. |
| `io.modelcontextprotocol/serverInfo` | result | Server SHOULD include in each result. Self-reported; never use for security decisions. |
| `io.modelcontextprotocol/logLevel` | request | Per-request log opt-in. Servers MUST NOT emit `notifications/message` without it. |
| `io.modelcontextprotocol/subscriptionId` | notification | Tags notifications on a `subscriptions/listen` stream. |
| `traceparent`, `tracestate`, `baggage` | both | W3C/OpenTelemetry trace propagation (SEP-414). |

### `resultType`
Every result now carries `resultType`: `"complete"` (normal) or `"input_required"` (MRTR interim result). Clients MUST treat a missing `resultType` from an older server as `"complete"`.

### Error codes
- `-32020` → `-32099` are **reserved for the MCP spec**; `-32000` → `-32019` stay implementation-defined.
- `-32020` `HeaderMismatch` (was `-32001` in the draft)
- `-32021` `MissingRequiredClientCapability` (was `-32003`)
- `-32022` `UnsupportedProtocolVersion` (was `-32004`)
- Resource-not-found is now **`-32602`** (Invalid Params), no longer `-32002`.
- If you see `-32001`/`-32003`/`-32004` used for the above in code or docs, it is from the draft/RC era and is wrong.

## Layer 2 — Version negotiation and discovery

There is **no handshake**. Each request declares its version; the server accepts or rejects it independently.

- Unsupported version → server returns `UnsupportedProtocolVersionError` (`-32022`, HTTP 400) with `data: {"supported": [...], "requested": "..."}`. Client picks a mutual version and retries.
- **`server/discover`** — servers MUST implement it. Params are just `_meta`. Result **(verified)**: `resultType`, `supportedVersions`, `capabilities`, `_meta["io.modelcontextprotocol/serverInfo"]`, optional `instructions`, plus cache fields `ttlMs`/`cacheScope`. Clients MAY call it first; it is optional on HTTP and recommended as the compatibility probe on stdio.
- **Dual-era detection (client side):**
  - stdio: send `server/discover` first; any non-modern error ⇒ fall back to `initialize`.
  - HTTP: attempt a modern request; on `400`, inspect the body. A recognised modern JSON-RPC error means "modern server, adjust and retry"; anything else ⇒ legacy server.
  - Cache the era decision per process (stdio) / origin (HTTP).
- **Dual-era server:** a request carrying modern `_meta` is served statelessly; an `initialize` request opts into legacy semantics for that stdio process / HTTP session. **(verified)** on FastMCP 4: modern requests get no `Mcp-Session-Id`; requests lacking modern headers fall into the legacy path and *do* get one. That is expected, not a bug.
- Compatibility matrix highlights: modern client ↔ legacy server **fails** (no automatic downgrade unless the client is dual-era); legacy client ↔ modern-only server **fails**.

## Layer 3 — Transports

### stdio
Unchanged in spirit. Log to **stderr**, never stdout (stdout is the protocol channel). `notifications/cancelled` is used only on stdio.

### Streamable HTTP (the only HTTP transport to build on)
- **One endpoint** (e.g. `/mcp`), **POST only**. Every JSON-RPC message is its own POST.
- Request headers (all REQUIRED on modern POSTs):
  - `Accept: application/json, text/event-stream`
  - `MCP-Protocol-Version: 2026-07-28` — must equal `_meta` protocolVersion
  - `Mcp-Method: <method>` — mirrors body `method`
  - `Mcp-Name: <name|uri>` — for `tools/call`, `resources/read`, `prompts/get`
  - `Mcp-Param-<Name>` — optional, from tool `inputSchema` properties annotated `"x-mcp-header": "<Name>"` (primitive types only; string/integer/boolean; statically reachable via `properties` chains; not `number`; not through `items`/`oneOf`/`$ref`).
- Non-ASCII or padded header values are encoded `=?base64?<b64>?=`.
- **Server validation:** header/body mismatch ⇒ `400` + `-32020`. This is a security control (a gateway routing on a header must not be fooled by a different body).
- Response: either `application/json` or an SSE stream scoped to **that request** (progress notifications, then the final response). Servers SHOULD send `X-Accel-Buffering: no` on SSE.
- Server MUST NOT send JSON-RPC *requests* to the client on any stream (see MRTR).
- **Cancellation = closing the response stream.**
- **Removed:** `Mcp-Session-Id`, the standalone GET endpoint, session DELETE, SSE `Last-Event-ID` resumability. A broken stream loses the in-flight request; the client re-issues it with a **new request id**. A modern-only server answers GET/DELETE with `405`, ignores `Mcp-Session-Id` and `Last-Event-ID`.
- **Security (unchanged, still MUST/SHOULD):** validate `Origin` (403 if invalid) to stop DNS rebinding; bind to `127.0.0.1` when local; authenticate.
- Unknown method ⇒ `404` + JSON-RPC `-32601` (the body distinguishes it from a legacy HTTP+SSE 404).

### HTTP+SSE (2024-11-05 transport) — Deprecated
Two endpoints (SSE GET + POST). Deprecated since 2025-03-26, formally Deprecated under the lifecycle policy. Don't build new work on it; migrate to Streamable HTTP.

## Layer 4 — Interaction patterns

### Multi Round-Trip Requests (MRTR, SEP-2322)
Replaces server-initiated `elicitation/create`, `sampling/createMessage`, `roots/list`.

1. Client calls `tools/call` (id 1).
2. Server returns a **result** (not an error) with `resultType: "input_required"` and `inputRequests: { "<key>": { method: "elicitation/create", params: {...} } }`. **(verified shape)**
3. Client gathers the input and **retries the original request** (new id) with the same params plus `inputResponses: { "<key>": { action: "accept", content: {...} } }`. **(verified shape)**
4. Server returns the final result (`resultType: "complete"`).

Consequences you must design for:
- The handler **re-runs from the top** each round. Do not perform side effects before you have read `inputResponses`; make the early part cheap and idempotent.
- Any state that must survive between rounds travels in `requestState` (opaque, echoed by the client). Because it round-trips through the client it is **untrusted** unless the server signs/encrypts it. FastMCP does this with `RequestStateSecurity`; with multiple replicas every replica needs the same key (≥32 bytes of secret material).
- Out-of-band (URL-mode) elicitation: the client learns the outcome by retrying. The old `notifications/elicitation/complete` and `elicitationId` are removed; encode your own correlation id in `requestState`.

### `subscriptions/listen` (replaces GET stream + `resources/subscribe`)
One long-lived **POST-response SSE stream**. Client opts into: `toolsListChanged`, `promptsListChanged`, `resourcesListChanged`, `resourceSubscriptions`. Server acknowledges (`notifications/subscriptions/acknowledged`) then streams only what was requested, tagged with `io.modelcontextprotocol/subscriptionId`. Progress and log notifications do **not** go here; they ride the response stream of their own request. Send periodic SSE comment lines (`:\r\n`) as keep-alive.

### Stateless protocol, stateful application
The spec's guidance: if a server needs cross-call state, **mint an explicit handle** (e.g. `basket_id`) from a tool and have the model pass it back as an argument. The model can see and thread the handle; hidden transport state it could not. List endpoints no longer vary per connection, so tool catalogs must not depend on who is connected.

## Layer 5 — Server features

- **Caching (SEP-2549):** `tools/list`, `prompts/list`, `resources/list`, `resources/read`, `resources/templates/list` (and `server/discover`) results are `CacheableResult`s with required `ttlMs` (ms freshness hint) and `cacheScope` (`"public"` = shared intermediaries may cache; `"private"` = per-user). Complements `listChanged`. **(verified)** FastMCP defaults: `ttlMs: 0`, `cacheScope: "private"`.
- **Deterministic ordering:** servers SHOULD return `tools/list` in a stable order, so client caches and upstream LLM prompt caches stay valid across reconnects.
- **Schemas loosened (SEP-2106):** `inputSchema`/`outputSchema` accept any JSON Schema 2020-12 keywords (with `$ref` resolution and composition-bound rules); `structuredContent` may be any JSON value (not just an object).
- Tool annotations, `outputSchema` + `structuredContent`, resource links, `title` fields and icons from earlier revisions all remain.
- `ping` is **removed**.

## Layer 6 — Client features

- **Elicitation:** form mode and URL mode (URL mode from 2025-11-25). Both now flow through MRTR rather than server-initiated requests.
- **Roots, Sampling, Logging — Deprecated (SEP-2577).** Still functional for ≥12 months; don't add them to new work. Suggested migrations:
  - Roots → pass dirs/files via tool parameters, resource URIs, or server config.
  - Sampling → call your LLM provider directly from the server.
  - Logging → log to stderr (stdio) or emit OpenTelemetry.
  - `includeContext: "thisServer" | "allServers"` also Deprecated; omit it or use `"none"`.
- `logging/setLevel` and `notifications/roots/list_changed` are **removed**.

## Layer 7 — Extensions

- Advertised in `capabilities.extensions`: a map of reverse-DNS id → settings object (`{}` = supported, no settings). Present in both `ClientCapabilities` and `ServerCapabilities`.
- If only one side supports an extension, the supporting side MUST fall back to core behaviour or reject with an appropriate error.
- **Tasks — `io.modelcontextprotocol/tasks` (SEP-2663).** Was experimental core in 2025-11-25. Now: poll with `tasks/get`, send input with `tasks/update`; `tasks/result` (blocking) and `tasks/list` are gone; servers may return a task handle unsolicited without per-request opt-in. `tasks/cancel` also exists **(secondary)**.
- **MCP Apps — `io.modelcontextprotocol/ui`** (interactive server-rendered UI; client advertises `mimeTypes: ["text/html;profile=mcp-app"]`). **(verified)** FastMCP 4's discover result advertises `io.modelcontextprotocol/ui` by default.
- **Enterprise-Managed Authorization (EMA)** — an official extension for enterprise identity; FastMCP 4's "identity assertion" feature (beta) relates to it.

## Layer 8 — Authorization (HTTP transports)

Baseline built up across revisions: OAuth 2.1 with PKCE; MCP server = OAuth **resource server** publishing Protected Resource Metadata (RFC 9728); clients use Resource Indicators (RFC 8707); incremental scope consent / step-up challenges; OIDC discovery.
New in 2026-07-28:

- `iss` parameter (RFC 9207): authorization servers SHOULD return it; clients MUST validate it against the recorded issuer **before** redeeming the code (closes the AS mix-up attack). SEP-2468.
- Dynamic Client Registration (RFC 7591) is **Deprecated** in favour of **Client ID Metadata Documents (CIMD)**. DCR remains for older authorization servers.
- When DCR is used, clients MUST send an appropriate `application_type` (native vs web) to avoid redirect-URI rejection of `localhost` for CLI/desktop apps. SEP-837.
- Client credentials are bound to the issuing authorization server: key persisted credentials by issuer, never reuse across issuers, re-register if the AS changes. SEP-2352.
- Don't hand-roll any of this. Use the framework's auth providers (FastMCP: `fastmcp.server.auth`) and keep tokens out of logs.

---

## Deprecated / removed registry (quick scan)

| Item | State in 2026-07-28 | Use instead |
|---|---|---|
| `initialize` / `notifications/initialized` | Removed (modern era) | per-request `_meta`, `server/discover` |
| `Mcp-Session-Id`, session DELETE | Removed | explicit handles / app-level state |
| HTTP GET stream, `resources/subscribe`/`unsubscribe` | Removed | `subscriptions/listen` |
| SSE `Last-Event-ID` resumability | Removed | re-issue request with new id |
| `ping`, `logging/setLevel`, `notifications/roots/list_changed` | Removed | — |
| Server→client `elicitation/create`, `sampling/createMessage`, `roots/list` requests | Replaced | MRTR `InputRequiredResult` |
| `notifications/elicitation/complete`, `elicitationId` | Removed | retry + `requestState` |
| Core `tasks/*` (experimental) | Moved | Tasks extension |
| Roots, Sampling, Logging | Deprecated (earliest removal: first revision on/after 2027-07-28) | see Layer 6 |
| HTTP+SSE transport | Deprecated | Streamable HTTP |
| DCR (RFC 7591) | Deprecated | CIMD |
| `includeContext` `thisServer`/`allServers` | Deprecated | omit / `"none"` |
