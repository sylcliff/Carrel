"""Brave web search endpoint.

A thin proxy over the ``brave_web_search`` MCP tool. Kept separate from
``carrel/api/search.py`` because (a) it's powered by a completely
different source (MCP vs the academic metadata fan-out), (b) the
upstream shape (web results) doesn't share the ``SearchResultItem``
schema with library/OpenAlex/S2/arXiv hits, and (c) it's an optional
service — if the MCP subprocess isn't running we return 503 rather than
silently merging an empty list into the main /search response.

Errors are mapped as:
  * ``MCPUnavailable`` → 503 (subprocess not running / disabled)
  * ``MCPError``       → 502 (upstream protocol / timeout error)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from carrel.mcp import get_mcp
from carrel.mcp.errors import MCPError, MCPUnavailable
from carrel.schemas import BraveSearchRequest, BraveSearchResponse
from carrel.search.brave import BRAVE_SERVER_NAME, BraveSearchClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/search/brave", tags=["search"])


def _get_brave_client() -> BraveSearchClient:
    reg = get_mcp()
    if reg is None:
        raise HTTPException(503, "MCP integration is disabled or not started")
    server = reg.get(BRAVE_SERVER_NAME)
    if server is None or not server.is_running:
        raise HTTPException(
            503,
            "brave_search MCP server is not running "
            "(check /mcp/health for details)",
        )
    return BraveSearchClient(reg)


@router.post("", response_model=BraveSearchResponse)
async def brave_search(req: BraveSearchRequest) -> BraveSearchResponse:
    client = _get_brave_client()
    try:
        return await client.web_search(
            query=req.query,
            count=req.count,
            country=req.country,
            search_lang=req.search_lang,
            freshness=req.freshness,
            safesearch=req.safesearch,
        )
    except MCPUnavailable as e:
        # Subprocess died between our availability check and the call —
        # surface as 503 (not 500) so clients know to retry after MCP
        # is restarted, not to treat it as a code bug.
        raise HTTPException(503, str(e)) from e
    except MCPError as e:
        logger.warning("brave_web_search failed: %s", e)
        raise HTTPException(502, f"brave search failed: {e}") from e
