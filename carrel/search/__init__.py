"""Brave web search client.

Thin wrapper over the ``brave_web_search`` MCP tool. The upstream server
returns the raw Brave REST API payload as a single ``TextContent`` block
in the ``CallToolResult``; we parse the ``web.results`` array and project
each row into Carrel's :class:`BraveSearchItem` schema.
"""
