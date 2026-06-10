"""User(workspace)-level Tool & MCP Registry (SPA-41).

A single source of truth for tools and MCP servers, configured once and referenced
by agent templates (``templates.tool_ids``) and task/experiment overrides instead of
duplicated inline on every template. ``service`` holds CRUD + masking + connection
test + the pure migration dedup; ``resolver`` materializes a template's references
(plus any ``run_config.tools_override``) into the exact tool-name list + MCP server
dicts the agent container consumes.
"""
