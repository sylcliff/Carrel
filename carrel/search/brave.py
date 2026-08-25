"""Brave web search client.

Thin wrapper over the ``brave_web_search`` MCP tool. The upstream server
returns the raw Brave REST API payload as a single ``TextContent`` block
in the ``CallToolResult``; we parse the ``web.results`` array and project
each row into Carrel's :class:`BraveSearchItem` schema.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from mcp.types import TextContent

from carrel.mcp import MCPClientRegistry, MCPError, MCPUnavailable
from carrel.schemas import BraveSearchItem, BraveSearchResponse

logger = logging.getLogger(__name__)

# The brave_web_search tool name as exposed by @brave/brave-search-mcp-server.
BRAVE_WEB_SEARCH_TOOL = "brave_web_search"

# Server name as registered in the MCPConfig.servers map.
BRAVE_SERVER_NAME = "brave_search"


def _parse_brave_response(
    raw_content: list[Any],
) -> list[dict[str, Any]]:
    """Extract the list of web result dicts from a CallToolResult.

    The MCP server returns the native Brave REST payload as JSON inside a
    single ``TextContent`` block. We only consume the first text block —
    the schema doesn't mix in non-text content for this tool.
    """
    for block in raw_content:
        if isinstance(block, TextContent):
            try:
                data = json.loads(block.text)
            except json.JSONDecodeError:
                logger.warning("brave_web_search returned non-JSON text content")
                return []
            web = data.get("web") or {}
            results = web.get("results")
            if isinstance(results, list):
                return results
            return []
    # No TextContent block (or empty content) — treat as no results rather
    # than raising; the tool is "search the web" and an empty page is valid.
    return []


def _to_item(row: dict[str, Any]) -> BraveSearchItem:
    """Project one native Brave result row into Carrel's schema.

    Defensive: every field except ``title`` and ``url`` is optional and
    may be absent on certain result types (e.g. FAQ, news).
    """
    return BraveSearchItem(
        title=row.get("title") or "",
        url=row.get("url") or "",
        description=row.get("description"),
        age=row.get("age"),
        language=row.get("language"),
        family_friendly=row.get("family_friendly"),
        extra_snippets=list(row.get("extra_snippets") or []),
    )


class BraveSearchClient:
    """Stateless adapter around the ``brave_search`` MCP server.

    Holds a reference to the registry (not the client directly) so the
    underlying subprocess can be replaced by the lifespan without
    re-instantiating this object.
    """

    def __init__(self, registry: MCPClientRegistry) -> None:
        self._registry = registry

    async def web_search(
        self,
        *,
        query: str,
        count: int = 10,
        country: str | None = None,
        search_lang: str | None = None,
        freshness: str | None = None,
        safesearch: str | None = None,
    ) -> BraveSearchResponse:
        client = self._registry.get(BRAVE_SERVER_NAME)
        if client is None or not client.is_running:
            raise MCPUnavailable(
                f"MCP server {BRAVE_SERVER_NAME!r} is not running"
            )

        arguments: dict[str, Any] = {"query": query, "count": count}
        if country:
            arguments["country"] = country
        if search_lang:
            arguments["search_lang"] = search_lang
        if freshness:
            arguments["freshness"] = freshness
        if safesearch:
            arguments["safesearch"] = safesearch

        t0 = time.monotonic()
        result = await client.call_tool(BRAVE_WEB_SEARCH_TOOL, arguments)
        took_ms = int((time.monotonic() - t0) * 1000)

        if getattr(result, "is_error", False):
            # The MCP tool returned a soft error (e.g. validation). The
            # content is typically a TextContent with the error message;
            # surface it as MCPError so the route maps to 502.
            msg = ""
            for block in result.content or []:
                if isinstance(block, TextContent):
                    msg = block.text
                    break
            raise MCPError(f"brave_web_search failed: {msg or 'unknown error'}")

        raw_results = _parse_brave_response(result.content or [])
        items = [_to_item(r) for r in raw_results]
        # Drop the empty / malformed rows (e.g. an item missing both title
        # and url would have rendered as two empty strings). Callers
        # shouldn't see those.
        items = [i for i in items if i.title and i.url]
        return BraveSearchResponse(
            query=query,
            results=items,
            total=len(items),
            took_ms=took_ms,
        )
