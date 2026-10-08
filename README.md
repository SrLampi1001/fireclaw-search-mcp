# Fireclaw Search MCP

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes the Firecrawl `/v2/search`, `/v2/scrape`, and `/v2/team/credit-usage` endpoints as MCP tools for any modern AI agent. Built on **FastMCP 4.0.11** and targeting the **MCP 2026-07-28** revision. Runs over Streamable HTTP (default) or stdio.

Firecrawl is the **deep** path against the free-tier budget (1,000 credits/month). Web search returns the **full page body** as clean markdown — useful for long-tail, question-shaped, or otherwise complex queries where snippets aren't enough. Single-URL scraping is exposed as a separate tool for fetching a specific page an agent already knows about. Every response carries a credit snapshot so the model can see its budget across calls.

No auth on the server itself; loopback bind by design. Expose via **VS Code port forwarding** (recommended) or ngrok.

## What you get

- `web_search(query, max_results=5, force_refresh=False)` — deep web search via Firecrawl `/v2/search` with `formats: ["markdown"]`. Returns `{title, url, markdown, markdown_chars, markdown_truncated_chars}` per result plus a `credits` snapshot.
- `scrape_url(url, force_refresh=False)` — single-URL scrape via `/v2/scrape` returning the page as clean markdown + metadata + `credits` snapshot. Useful for fetching a known page after `web_search` produced an empty or thin result.
- `credit_status()` — read-only view of the team's Firecrawl credit balance via `/v2/team/credit-usage`. Doesn't burn credits.
- `research_topic(topic)` — reusable prompt that plans one `web_search` call and defines the empty-result fallback.
- `fetch_document(url, focus)` — reusable prompt that plans one `scrape_url` call and tells the model to summarise around a focus query.
- On-disk TTL cache (24 h default) so repeat queries within the window don't burn credits.
- Configurable minimum-credits guard so a runaway loop can't drain the monthly budget.

## Quick start

```bash
# Install (Python 3.10+)
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure the Firecrawl API key (free tier: get one at https://firecrawl.dev)
cp .env.example .env
$EDITOR .env

# Run (Streamable HTTP on http://127.0.0.1:8000/mcp)
python3 -m fireclaw_search_mcp
```

Then either:

- **Local use** — point an MCP client at `http://127.0.0.1:8000/mcp` on the same machine.
- **VS Code port forwarding** — open the Ports panel, forward `8000`, copy the tunnel URL, point your client at `<forwarded-url>/mcp`. See [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md).
- **ngrok** — `ngrok http 8000`, use the printed URL the same way.

## Documentation

- [docs/SPECS.md](./docs/SPECS.md) — the contract this server commits to: protocol, tool schemas, response mapping, failure modes, acceptance criteria.
- [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) — connection recipes for every supported client, plus hardening notes for non-loopback exposure.
- [.agents/skills/mcp-fastmcp-2026/](./.agents/skills/mcp-fastmcp-2026/) — the skill this server was built against, kept in-tree for future agents and for the static audit and wire-probe scripts.

## Testing

```bash
.venv/bin/pytest                                  # unit tests, hermetic
FIRECRAWL_NETWORK_TESTS=1 .venv/bin/pytest        # + integration tests, real Firecrawl
.venv/bin/python .agents/skills/mcp-fastmcp-2026/scripts/audit_mcp_project.py .   # static
```

After starting the server:

```bash
.venv/bin/python .agents/skills/mcp-fastmcp-2026/scripts/probe_mcp_server.py http://127.0.0.1:8000/mcp   # wire probe
```

## Why the limits are where they are

Firecrawl's free tier is **1,000 credits/month**. The default search-with-scrape call costs ~2 search credits + 1 credit per scraped result — roughly 7 credits for `max_results=5`. The defaults (`max_results=5`, `min_credits=4`, `cache_ttl=86400` seconds, `max_markdown_chars=8000` per result) keep a single session from draining the budget in a handful of loops while still letting the model answer substantive queries. The per-result markdown cap protects the calling agent's context window.

Every `web_search` and `scrape_url` response includes the `credits` snapshot so the model can see its balance drop across calls and switch to a free tool (e.g. a sibling `duck-search-mcp`) when credits get low. `credit_status()` lets the model peek without burning anything.
