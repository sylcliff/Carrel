"""In-process tools exposed to the LLM alongside MCP servers.

MCP tools (Brave search, etc.) are subprocess-backed and routed through
:mod:`carrel.mcp.client`. Built-in tools run in the FastAPI process itself:
no subprocess, no JSON-RPC, no SOCKS bridge — the LLM sees them as just
another tool with a ``builtin__<name>`` prefix. The dispatcher
(:func:`carrel.mcp.tools.dispatch_tool_call`) consults the in-process
registry first, then falls through to MCP.

Why this exists: some capabilities are Carrel-native (writing a note to
the local wiki page, say) and don't justify a whole MCP server. v1 ships
exactly one tool — :func:`_save_scholar_note` — which appends a note to
the user section of a scholar page. Concepts/questions can follow the
same pattern in a follow-up PR.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.types import Tool as _McpTool

from carrel.pipeline.wiki._merge import append_to_user_section

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

# Namespace used as the litellm function-name prefix, matching the
# <server>__<tool> convention in :mod:`carrel.mcp.tools`.
BUILTIN_SERVER_NAME = "builtin"


@dataclass(frozen=True)
class BuiltinTool:
    """An in-process tool, shaped like an MCP tool so the model can't tell
    them apart. ``name`` is the bare tool name (no ``builtin__`` prefix —
    the prefix is added when projecting to litellm form)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]


# The single source of truth for which in-process tools exist. New entries
# are appended here; :func:`collect_builtin_tools` and :func:`dispatch_builtin`
# walk this list.
_REGISTRY: list[BuiltinTool] = []


def _register(tool: BuiltinTool) -> None:
    _REGISTRY.append(tool)


def _as_mcp_shaped(tool: BuiltinTool) -> _McpTool:
    """Project a :class:`BuiltinTool` to the shape ``mcp_tool_to_litellm`` expects.

    We only need ``name`` / ``description`` / ``inputSchema`` to flow through;
    constructing a real :class:`mcp.types.Tool` keeps the projection a one-liner
    with the existing helper.
    """
    return _McpTool(
        name=tool.name,
        description=tool.description,
        inputSchema=tool.input_schema,
    )


def _to_litellm(tool: BuiltinTool) -> dict[str, Any]:
    """Inline equivalent of :func:`carrel.mcp.tools.mcp_tool_to_litellm` to
    avoid a circular import (this module is imported by the package's
    ``__init__`` *before* :mod:`carrel.mcp.tools` finishes its own import)."""
    return {
        "type": "function",
        "function": {
            "name": f"{BUILTIN_SERVER_NAME}__{tool.name}",
            "description": tool.description or "",
            "parameters": dict(tool.input_schema),
        },
    }


def collect_builtin_tools() -> list[dict[str, Any]]:
    """Return the litellm-shaped list of in-process tools, ready to pass to
    :func:`carrel.mcp.tools.collect_tools` as the ``builtins=`` argument.
    """
    return [_to_litellm(t) for t in _REGISTRY]


def dispatch_builtin(
    name: str, arguments: dict[str, Any]
) -> str | None:
    """Run the in-process tool named ``name`` (the bare name, no prefix).

    Returns the handler's string on success, or ``None`` if no in-process
    tool matches — the caller (the MCP dispatcher) interprets ``None`` as
    "fall through to the MCP registry". Handlers that raise are caught and
    re-raised as :class:`ValueError` so the dispatcher's existing
    ``[tool error]`` wrapping handles the wire format.
    """
    for t in _REGISTRY:
        if t.name == name:
            return t.handler(arguments)
    return None


def builtin_dispatch_map() -> dict[str, Callable[[dict[str, Any]], str]]:
    """Return a ``{bare_name: handler}`` map for fast dispatch lookup."""
    return {t.name: t.handler for t in _REGISTRY}


# ---------------------------------------------------------------------------
# save_scholar_note
# ---------------------------------------------------------------------------

# Mirrors the two shapes :func:`carrel.pipeline.wiki._slug.scholar_slug`
# produces (``A\d+`` or ``name--<slug>``). The model can only target slugs
# Carrel itself ever writes; ``..``/path-traversal attempts are impossible
# by construction.
_SLUG_RE = re.compile(r"^(A\d+|name--[a-z0-9][a-z0-9-]*)$")

# Hard cap so a model can't write a megabyte into a markdown file. Real
# notes are paragraphs; anything beyond this is almost certainly a bug.
_MAX_CONTENT_CHARS = 50_000

_SAVE_SCHOLAR_NOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "slug": {
            "type": "string",
            "description": (
                "Scholar page slug, e.g. 'A5023487560' for Michele Parrinello. "
                "The chat's `sources` frame exposes this as the `slug` field for "
                "every page in retrieval context."
            ),
        },
        "section_title": {
            "type": "string",
            "description": (
                "Markdown heading for the new note, e.g. 'Biographical notes'. "
                "Single line; will be emitted as `## <section_title>`."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "Markdown body of the note. Will be placed under the section "
                "heading inside the page's user-notes section. Max 50000 chars."
            ),
        },
    },
    "required": ["slug", "section_title", "content"],
}


def _atomic_write(path: Path, text: str) -> None:
    """Same mkstemp + os.replace pattern as the compile modules. Duplicated
    rather than shared so :mod:`carrel.mcp` doesn't have to import the
    compile pipeline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".wiki-", suffix=".md", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _save_scholar_note(args: dict[str, Any]) -> str:
    slug = str(args.get("slug") or "").strip()
    section_title = str(args.get("section_title") or "").strip()
    content = str(args.get("content") or "").strip()

    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"invalid scholar slug {slug!r} (expected A<digits> or name--<slug>)"
        )
    if not section_title:
        raise ValueError("section_title must be a non-empty single line")
    if "\n" in section_title or "\r" in section_title:
        raise ValueError("section_title must be a single line (no newlines)")
    if not content:
        raise ValueError("content must be non-empty")
    if len(content) > _MAX_CONTENT_CHARS:
        raise ValueError(
            f"content too large: {len(content)} chars (max {_MAX_CONTENT_CHARS})"
        )

    # Late import to dodge a circular import at module load time
    # (carrel.main imports the FastAPI app which imports the mcp package).
    from carrel.main import app_config

    rel = f"wiki/scholars/{slug}.md"
    full = Path(app_config.storage.root) / rel
    if not full.exists():
        raise ValueError(f"scholar page not found: {rel}")

    raw = full.read_text(encoding="utf-8")
    new_text = append_to_user_section(
        raw, section_title=section_title, content=content
    )
    if new_text == raw:
        return f"no-op: {rel} already contains section '{section_title}'"

    _atomic_write(full, new_text)
    logger.info("saved scholar note to %s (section=%r)", rel, section_title)
    return f"Saved note '{section_title}' to {rel}"


_register(
    BuiltinTool(
        name="save_scholar_note",
        description=(
            "Append a note to a scholar's wiki page user-section. The note is "
            "placed under `section_title` inside the page's <section "
            "data-user=\"true\"> block, which the compiler preserves across "
            "recompiles. Use this when the user asks to 'save', 'remember', "
            "or 'add' info about a scholar to their page. The chat's `sources` "
            "frame exposes the page's slug (e.g. 'A5023487560') for every "
            "page in retrieval context — pass that as `slug`."
        ),
        input_schema=_SAVE_SCHOLAR_NOTE_SCHEMA,
        handler=_save_scholar_note,
    )
)
