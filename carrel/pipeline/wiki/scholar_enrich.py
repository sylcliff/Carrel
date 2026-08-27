"""Scholar "Research & enrich" — LLM-with-tools web research for one page.

Spawns an agent run that uses ``brave_search__brave_web_search`` to look up
current information about a scholar online, then calls the in-process
``builtin__save_scholar_note`` tool to append a ``## Web research`` block to
the page's preserved ``<section data-user="true">...</section>``. The block
survives subsequent recompiles (the existing :func:`protect_user_section`
mechanism in :mod:`carrel.pipeline.wiki._merge` keeps the user section
intact across LLM regenerations).

Does NOT regenerate the LLM-authored sections (Summary, Research lines, …).
The user hits "Recompile profile" for that; this feature only enriches the
user-editable block.

The agentic loop is **async** (MCP ``call_tool`` is async; litellm streaming
is async-friendly). The recompile path uses a sync FastAPI ``BackgroundTasks``
job, so this module exposes a sync ``enrich_scholar_wiki`` entry point that
bridges via :func:`asyncio.run` on a fresh event loop. The
:class:`MCPClientRegistry` is safe to re-enter from a new loop because it
holds only plain state (no asyncio primitives of its own).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from sqlmodel import Session

from carrel import llm, prompts_runtime, usage
from carrel.mcp import (
    builtin_dispatch_map,
    collect_builtin_tools,
    collect_tools,
    get_mcp,
)
from carrel.mcp.tools import run_agentic_loop
from carrel.models import WikiPage

logger = logging.getLogger(__name__)


# Whitelist of tools the enrich agent is allowed to call. Listed by litellm
# function name (``<server>__<tool>``). Keeps the model from reaching
# unrelated MCP servers (e.g. a future code-execution MCP) or in-process
# tools that don't make sense for this workflow.
_ALLOWED_TOOLS: set[str] = {
    "brave_search__brave_web_search",
    "builtin__save_scholar_note",
}


_SYSTEM_TEMPLATE = """You are a research assistant enriching a Carrel scholar profile.

The scholar is **{name}** (slug: `{slug}`). Your job is to write a short
**Web research** note that the user will see on their wiki page. The page
already has compiled sections (Summary, Research lines, etc.) from in-library
papers; do NOT try to rewrite them. You only have access to two tools:

1. `brave_search__brave_web_search(query, count?, country?)` — search the
   live web for up-to-date information about the scholar (current role,
   affiliation, recent awards, news, lab/personal website, notable recent
   collaborations).
2. `builtin__save_scholar_note(slug, section_title, content)` — append a
   note inside the scholar page's preserved user-section. Pass
   `slug="{slug}"`, `section_title="Web research"`, and a synthesized
   markdown body. The body MUST be a single markdown block with the
   following shape and nothing else:

       ### Current role & affiliation
       - <one or two bullets; cite URLs inline like [lab](https://...)>

       ### Recent news (last 12 months)
       - <bullets with inline links; omit the section entirely if nothing surfaced>

       ### Awards & honors
       - <bullets; or omit the section>

       ### Lab / personal website
       - <one line; or omit the section>

Hard rules:
- Use ONLY facts that came back from `brave_search__brave_web_search` in
  this session. Do not rely on training knowledge.
- Every bullet that contains a fact must end with an inline link to the
  Brave result that backs it.
- `section_title` is always exactly "Web research" (one line).
- Call `save_scholar_note` exactly ONCE with the full synthesized body.
  After it returns successfully, reply with a one-line summary like
  "Saved Web research note with N bullets." and stop.
- If the first `brave_search__brave_web_search` call returns an error
  string (e.g. "[unavailable] MCP server 'brave_search' is not running"),
  still call `save_scholar_note` with a one-line body saying:
  "Could not fetch web results — check `BRAVE_API_KEY` and the
  brave_search MCP server." That way the user always sees the button
  worked, even when the web layer is down.
- If the search returns no relevant results, write a one-line body
  starting with "No recent web results found for …" and stop.

Do not call any other tool. Do not produce a long preamble. Search,
synthesize, save, done."""


_USER_TEMPLATE = "Research the scholar {name}{affiliation_hint} and save the result."


def _build_system_prompt(slug: str, name: str, *, session: Session | None = None) -> str:
    """Return the SYSTEM message for the enrich agent.

    The prompt is deliberately constrained: it tells the model exactly
    which tools to use, what shape the saved note should take, and what
    to do on a web-search failure (still call ``save_scholar_note`` with
    a one-liner so the user always sees the button worked, even when the
    web layer is down).
    """
    return prompts_runtime.get_system(
        "wiki_enrich", _SYSTEM_TEMPLATE, session=session
    ).format(slug=slug, name=name)


def _build_messages(
    name: str,
    affiliation: str | None,
    *,
    system_prompt: str,
    session: Session | None = None,
) -> list[dict[str, str]]:
    """Return the initial messages list: [system, user]."""
    hint = f" ({affiliation})" if affiliation else ""
    user_content = prompts_runtime.get_user_template(
        "wiki_enrich", _USER_TEMPLATE, session=session
    ).format(name=name, affiliation_hint=hint)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def _filter_tools(all_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the tools in :data:`_ALLOWED_TOOLS`. Stable order."""
    keep = {t for t in _ALLOWED_TOOLS}
    return [t for t in all_tools if t.get("function", {}).get("name") in keep]


async def _enrich_async(
    session: Session,
    cfg: Any,  # app_config (LLMConfig lives under cfg.llm)
    page: WikiPage,
    on_progress: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    """Run the enrich agent. Returns a stats dict (iterations,
    tools_called, notes_saved, errors). Raises on missing tool
    configuration so the caller can fail the job.
    """
    registry = get_mcp()
    builtin_handlers = builtin_dispatch_map()
    all_tools = collect_tools(registry, builtins=collect_builtin_tools())
    tools = _filter_tools(all_tools)
    if not tools:
        raise RuntimeError(
            "no enrichment tools available — is the brave_search MCP "
            "server running? check /mcp/health"
        )

    name = page.title or page.slug
    system_prompt = _build_system_prompt(page.slug, name, session=session)
    messages = _build_messages(
        name, None, system_prompt=system_prompt, session=session
    )  # affiliation not mirrored in WikiPage

    model = (
        cfg.llm.wiki_enrich_model
        or cfg.llm.chat_model
        or cfg.llm.summarize_model
    )
    fallback = (
        cfg.llm.wiki_enrich_fallback_model
        or cfg.llm.chat_fallback_model
        or cfg.llm.fallback_model
    )

    stats: dict[str, Any] = {
        "iterations": 0,
        "tools_called": [],
        "notes_saved": [],
        "errors": [],
    }

    def _on_iter(
        _text_buf: list[str],
        _tool_calls: list[dict[str, Any]],
        it: int,
    ) -> None:
        stats["iterations"] = it + 1
        if on_progress is not None:
            on_progress(
                {
                    "stage": "wiki_enrich",
                    "iteration": it + 1,
                    "detail": f"iteration {it + 1}",
                }
            )

    def _on_tool(
        name: str,
        _args: dict[str, Any],
        _result_str: str,
        is_error: bool,
        _it: int,
    ) -> None:
        stats["tools_called"].append(name)
        if is_error:
            stats["errors"].append(name)
        if name == "builtin__save_scholar_note" and not is_error:
            stats["notes_saved"].append("Web research")
        if on_progress is not None:
            on_progress(
                {
                    "stage": "wiki_enrich",
                    "detail": f"{name}{' (errored)' if is_error else ''}",
                }
            )

    on_usage = usage.make_usage_callback(session, feature="wiki_enrich")

    await run_agentic_loop(
        messages,
        model=model,
        fallback_model=fallback,
        temperature=cfg.llm.chat_temperature,
        timeout=cfg.llm.request_timeout_seconds,
        on_usage=on_usage,
        tools=tools,
        registry=registry,
        builtin_handlers=builtin_handlers,
        max_iters=cfg.llm.wiki_enrich_max_iterations,
        on_iteration=_on_iter,
        on_tool_result=_on_tool,
    )
    return stats


def enrich_scholar_wiki(
    session: Session,
    cfg: Any,
    page: WikiPage,
    *,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Sync entry point. Runs the async enrich agent on a fresh event
    loop and returns the stats dict.

    The function is intentionally synchronous because it's called from a
    ``BackgroundTasks`` worker thread; opening a private event loop
    keeps MCP and the litellm streaming off the request-handling loop.
    """
    return asyncio.run(_enrich_async(session, cfg, page, on_progress))
