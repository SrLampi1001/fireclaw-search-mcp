# MCP revision history: 2024-11-05 → 2026-07-28

Purpose: your built-in knowledge probably stops at `2024-11-05`. This file bridges the gap revision by revision so you can read old code, old tutorials and old docs *correctly* and know what each one got wrong by today's standards.

Items marked ◆ come from the official 2026-07-28 changelog/spec; the 2025 revisions are summarised from the official changelogs at a high level — if a detail matters, fetch `https://modelcontextprotocol.io/specification/<revision>/changelog`.

## Contents
- 2024-11-05 — the original
- 2025-03-26
- 2025-06-18
- 2025-11-25
- 2026-07-28 — the stateless revision
- How to read an old tutorial
- Cheat sheet: "old habit → current practice"

---

## 2024-11-05 — the original
- Transports: **stdio** and **HTTP+SSE** (a GET opens an SSE stream that emits an `endpoint` event; client POSTs to that endpoint).
- Lifecycle: `initialize` request → result → `notifications/initialized`. Capability exchange happens here. Stateful connection.
- Server features: tools, resources, prompts. Client features: roots, sampling.
- Utilities: logging, ping, pagination, cancellation, progress.
- No structured tool output, no elicitation, no OAuth framework in the spec, no tool annotations.

## 2025-03-26
- **Streamable HTTP** introduced, replacing HTTP+SSE (which became deprecated).
- **OAuth 2.1**-based authorization framework added.
- JSON-RPC **batching** added (later removed).
- **Tool annotations** (read-only / destructive / idempotent / open-world hints).
- Progress notifications gained a `message`; **audio** content type; completions capability.

## 2025-06-18
- JSON-RPC **batching removed**.
- **Structured tool output**: `outputSchema` + `structuredContent`.
- MCP servers formally classified as **OAuth resource servers** (Protected Resource Metadata, RFC 9728); **Resource Indicators** (RFC 8707) required of clients.
- **Elicitation** (form mode) introduced: server asks the user for input mid-call.
- **Resource links** in tool results; `title` display names; richer `_meta`.
- `MCP-Protocol-Version` header required on HTTP requests after init.

## 2025-11-25
- **Tasks** (experimental): durable, pollable long-running requests.
- **URL-mode elicitation** for sensitive out-of-band flows (credentials, payments).
- **Sampling with tool calling.**
- **Client ID Metadata Documents (CIMD)** recommended for client registration over DCR.
- OIDC discovery, incremental scope consent / step-up auth, icons metadata, richer elicitation schemas (enums, defaults), early **extensions** concept.
- Still handshake + `Mcp-Session-Id` based.

## 2026-07-28 — the stateless revision
Full detail in `spec-layers.md`. Headlines ◆:
1. No handshake, no sessions: every request self-describes via `_meta`; `server/discover` (mandatory on servers, optional for clients).
2. MRTR replaces server-initiated elicitation/sampling/roots requests.
3. `subscriptions/listen` replaces GET stream + resource subscribe.
4. `Mcp-Method` / `Mcp-Name` (+ `Mcp-Param-*`) headers for gateway routing.
5. `ttlMs` + `cacheScope` on list/read results; deterministic tool order.
6. Tasks → official extension; extensions framework (`capabilities.extensions`).
7. Auth hardening: RFC 9207 `iss`, CIMD over DCR, `application_type`, issuer-bound credentials.
8. Roots, Sampling, Logging, HTTP+SSE, DCR **Deprecated**; formal 12-month lifecycle policy.
9. `resultType` on every result; resource-not-found → `-32602`; error-code allocation policy (`-32020..-32099` reserved for MCP).
10. Removed: `ping`, `logging/setLevel`, roots list-changed notification, SSE resumability.

---

## How to read an old tutorial
Check the revision it targets before copying anything:

| Tutorial mentions… | It targets | Treat as |
|---|---|---|
| `endpoint` SSE event, `SseServerTransport`, `/sse` + `/messages` | 2024-11-05 | Deprecated transport |
| `from mcp.server.fastmcp import FastMCP` | Python SDK v1 (bundled FastMCP 1.0) | Gone in `mcp` v2 |
| `initialize` handler / `on_initialize` / capability checks at connect | ≤2025-11-25 | Legacy era only |
| `Mcp-Session-Id`, session stores, sticky sessions | ≤2025-11-25 | Not needed/removed |
| `await ctx.elicit(...)` inside a tool | 2025-06-18+ | Legacy era only |
| `ctx.sample(...)`, `list_roots()` | any ≤2025-11-25 | Deprecated; removed in FastMCP 4 |
| `@mcp.tool(task=True)` working with no extra setup | FastMCP 3 | Needs `TasksExtension` in 4 |
| JSON-RPC batch arrays | 2025-03-26 | Removed in 2025-06-18 |

## Cheat sheet: old habit → current practice
- "Server needs to ask the user something" → return `InputRequiredResult`, don't call back mid-execution.
- "Keep conversation state on the connection" → explicit handle argument, or `UserSession`/`SessionId`, backed by a shared store when replicated.
- "Notify client when the tool list changes" → client opts in via `subscriptions/listen`; also set `cache_ttl` so clients can cache sensibly.
- "Run a slow job" → Tasks extension, not a held-open request.
- "Choose a version at init" → declare it on every request; handle `UnsupportedProtocolVersionError`.
- "Log through the protocol" → stderr / OpenTelemetry.
- "Register OAuth clients dynamically" → CIMD.
