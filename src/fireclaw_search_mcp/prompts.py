"""MCP prompts registered on the FastMCP instance.

Per the project README: include reusable prompts that make sure any agent
querying via the MCP gets the most from each call.
- `research_topic` — plans one `web_search` call and sets the empty-result
  fallback + the credit-budget reminder (SPECS §9.1).
- `fetch_document` — plans one `scrape_url` call with a focus question
  (SPECS §9.2).
"""
from __future__ import annotations

from fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    @mcp.prompt(
        name="research_topic",
        description=(
            "Plan a web_search call for a topic. Steers the model toward a "
            "canonical topic name, the empty-result fallback, and the credit-"
            "budget reminder."
        ),
    )
    def research_topic(topic: str) -> str:
        """Generate a user message that sets up one `web_search` call."""
        return (
            f"Research the topic '{topic}'.\n"
            f"1. Use the web_search tool with a canonical topic name "
            f"(e.g. 'Rust programming language', NOT 'how do I write async code in Rust'). "
            f"Pass max_results=5.\n"
            f"2. Every response includes a `credits` snapshot. If "
            f"`remaining_credits` is below ~50, switch to a free sibling search "
            f"tool (e.g. `duck-search-mcp`) for follow-up lookups.\n"
            f"3. If the result is empty, rephrase once as a canonical topic "
            f"name and try again.\n"
            f"4. If still empty, fall back to your own knowledge and clearly "
            f"state that the search returned no results.\n"
            f"5. When a result's markdown is truncated (look for "
            f"`markdown_truncated_chars` > 0 and `truncated: true`), narrow the "
            f"follow-up query or call `scrape_url` on the same URL with a "
            f"focus."
        )

    @mcp.prompt(
        name="fetch_document",
        description=(
            "Plan a scrape_url call on a known URL with a focus question. "
            "Tells the model to summarise the markdown around the focus and "
            "to respect the per-page markdown cap."
        ),
    )
    def fetch_document(url: str, focus: str) -> str:
        """Generate a user message that sets up one `scrape_url` call."""
        return (
            f"Fetch the page at '{url}' and answer: {focus}.\n"
            f"1. Use the scrape_url tool on '{url}'. The response contains "
            f"`markdown` plus `metadata.title` / `metadata.description` /\n"
            f"   `metadata.language` / `metadata.url` (post-redirect).\n"
            f"2. Per-page markdown is capped at ~8k chars; the cut is "
            f"visible via `markdown_truncated_chars` and `truncated: true`.\n"
            f"3. If the markdown is empty or the page errors out, summarise "
            f"whatever the metadata says and clearly state the gap.\n"
            f"4. Every response includes a `credits` snapshot. If "
            f"`remaining_credits` is below ~10, skip this call and answer "
            f"from the page title alone.\n"
            f"5. Quote sparingly — paraphrase around the focus, cite only "
            f"URLs the tool returned."
        )
