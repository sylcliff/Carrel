"""Tiny stdio MCP server used for live-testing the wiki chat tool loop.

Exposes one tool, ``mock_search``, that returns a canned JSON payload
without ever touching the network. The point is to prove the
``carrel → MCPClientRegistry → stdio subprocess → tool result → back
to the LLM`` round-trip end-to-end on a developer machine that has no
``BRAVE_API_KEY`` configured.

Run via stdio — no flags needed, no daemon mode. The registry spawns
this file with ``command: python`` and ``args: ["-m", "tests.mock_mcp_server"]``
(or equivalent in YAML).
"""
from __future__ import annotations

import json
from mcp.server.fastmcp import FastMCP

app = FastMCP("mock-search")


@app.tool()
def mock_search(query: str, top_k: int = 3) -> str:
    """Return a canned Brave-shaped result for ``query``.

    The wiki chat LLM only sees the string this function returns, so
    the shape mirrors the real Brave payload — JSON with a ``web.results``
    list. ``top_k`` is honored so the model can ask for fewer / more.
    """
    titles = {
        "rag": "Latest RAG paper",
        "mcp": "What is MCP?",
        "default": f"Mock result for {query!r}",
    }
    q_lower = query.lower()
    picked = next((v for k, v in titles.items() if k in q_lower), titles["default"])
    results = [
        {
            "title": f"{picked} #{i + 1}",
            "url": f"https://example.com/mock?q={query}&n={i + 1}",
            "description": f"Canned entry {i + 1} for {query!r} from the mock server.",
        }
        for i in range(max(1, min(top_k, 5)))
    ]
    return json.dumps({"web": {"results": results}}, ensure_ascii=False)


if __name__ == "__main__":
    app.run(transport="stdio")
