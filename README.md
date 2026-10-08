# Fireclaw Seach MCP
The objective of this MCP is to create an MCP (using the framework and MCP version specified in the [skill](.agents/skills/mcp-fastmcp-2026/SKILL.md)) that implements the best practices to use the free tier Fireclaw search API.
Using VS Code forwarding to expose a external connection (no Auth) and allow the MCP to be consumed on trusted hosts only (Currently).
Ngirok is a valid fallback, but isn't to be configured in this repository.

## Objectives 
- Offer the Fireclaw web search and scrapping as tools in the MCP.

- Cache the results to remove costs by performing the same search → This comes with the catch that the search can be updated using the API if the user asks for it specifically, or if another mechanism is implemented to allow identifying if the source where the data was retrieved was updated (Really important for pages that give the official documentation on a Framework or programming tool, and the sources need to stay current)

- Use prompts and return the remaining credits to guide the Agent into a conservative usage of the API.

- Provide skills that allow the best practices for extracting and organizing search results, specially in development.

The MCP configuration must allow the user to insert it's own Fireclaw API (in a gitignored file) → If possible, it should allow the configuration from the client side. 