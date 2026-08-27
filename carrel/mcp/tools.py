"""MCP tool exposure utilities for the LLM layer.

This module is the bridge between Carrel's MCP server registry
(:class:`MCPClientRegistry`) and the litellm/OpenAI ``tools=`` shape the
chat model consumes. It deliberately knows nothing about FastAPI, the
DB, or any specific feature (wiki chat, paper chat, etc.) — pure
functions only, so it can be reused as we wire tool-use into more call
sites.

Design notes
------------

* The litellm function-call schema requires unique ``function.name`` per
  tool. Two MCP servers could expose tools with the same name (e.g.
  both have a ``search``), so we prefix with the server name and use
  ``<server>__<tool>`` everywhere we hand names to the model. Underscore
  is allowed by the OpenAI function-name regex.

* Tool *descriptions* and *parameters* are passed through verbatim. The
  MCP server is the source of truth for what the tool does; we don't
  re-translate the schema. The model is expected to read
  ``parameters`` as a JSON Schema object (the litellm/OpenAI standard).

* Dispatching is just a thin wrapper over :meth:`MCPClient.call_tool`
  that concatenates ``TextContent`` blocks into a single string the LLM
  can ingest. For multi-block responses this is a lossy concatenation,
  but every tool we currently use (``brave_web_search``) returns a
  single text block, and the LLM only needs the textual representation
  to ground its next answer.

* Errors from the dispatcher come back as a short string the model can
  see (``"MCP server 'brave_search' is not running"``,
  ``"tool error: invalid query"``). That's deliberate — a tool failure
  should degrade the answer, not abort the whole request. ``MCPError``
  and ``MCPUnavailable`` still propagate out of :func:`dispatch_tool_call`
  because they're configuration / transport failures the caller (the
  FastAPI route) is responsible for mapping to a status code.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from mcp.types import TextContent

from carrel import llm
from carrel.mcp.errors import MCPError, MCPUnavailable

if TYPE_CHECKING:
    from carrel.mcp import MCPClientRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------


def mcp_tool_to_litellm(server: str, tool: Any) -> dict[str, Any]:
    """Project an ``mcp.types.Tool`` into the OpenAI/litellm function shape.

    The function ``name`` is prefixed with the server (``<server>__<tool>``)
    so two servers that expose the same tool name don't collide on the
    model side. ``parameters`` is the upstream ``inputSchema`` verbatim —
    the model is expected to read it as a JSON Schema object.
    """
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    return {
        "type": "function",
        "function": {
            "name": f"{server}__{tool.name}",
            "description": tool.description or "",
            "parameters": dict(schema),
        },
    }


def parse_litellm_name(name: str) -> tuple[str, str]:
    """Reverse of the prefix scheme. Returns ``(server, tool_name)``.

    Raises :class:`ValueError` if the name doesn't match the expected
    ``<server>__<tool>`` shape — a model hallucinating a malformed name
    is the most likely cause.
    """
    if "__" not in name:
        raise ValueError(f"tool name {name!r} is missing the server prefix")
    server, _, tool_name = name.partition("__")
    if not server or not tool_name:
        raise ValueError(f"tool name {name!r} is not in <server>__<tool> form")
    return server, tool_name


# ---------------------------------------------------------------------------
# Registry flattening
# ---------------------------------------------------------------------------


def collect_tools(
    registry: MCPClientRegistry | None,
    *,
    builtins: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the litellm-shaped tool list for every running server.

    Returns ``[]`` when MCP is disabled or not started. Servers that are
    configured but not running (subprocess died) are skipped — we don't
    want the model to be able to invoke them.

    ``builtins`` is an optional list of in-process tool definitions
    (litellm-shaped) to merge in *before* the MCP tools. Builtins are
    listed first so the model sees them early in its prompt. The
    corresponding handlers are looked up at dispatch time via
    :func:`carrel.mcp.builtin_tools.builtin_dispatch_map`.
    """
    out: list[dict[str, Any]] = list(builtins or [])
    if registry is None:
        return out
    for client in registry.servers():
        if not client.is_running:
            continue
        for tool in client.tools:
            out.append(mcp_tool_to_litellm(client.name, tool))
    return out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _format_call_tool_result(result: Any) -> tuple[str, bool]:
    """Collapse a ``CallToolResult`` to ``(text, is_error)``.

    Walks ``result.content`` and concatenates every ``TextContent`` block
    (the only block type our current servers use). Non-text blocks are
    dropped with a warning. The upstream's error flag is read from both
    ``isError`` (the official MCP field name used by the Python SDK) and
    ``is_error`` (an alias some shims provide) — without this, error
    responses from tools like Brave's ``fetch failed`` would be silently
    handed to the LLM as if they were real results.
    """
    is_error = bool(
        getattr(result, "isError", None) or getattr(result, "is_error", False)
    )
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            logger.warning(
                "MCP tool returned non-text content block of type %s; dropping",
                type(block).__name__,
            )
    return ("\n".join(parts) if parts else ""), is_error


async def dispatch_tool_call(
    registry: MCPClientRegistry,
    name: str,
    arguments: dict[str, Any],
    *,
    builtins: dict[str, Callable[[dict[str, Any]], str]] | None = None,
) -> str:
    """Forward one litellm tool call to the appropriate MCP server.

    Returns the textual result. The string is what gets pushed back to
    the model as the ``content`` of a ``role: "tool"`` message.

    If ``builtins`` is provided AND the parsed ``<server>`` is
    :data:`carrel.mcp.builtin_tools.BUILTIN_SERVER_NAME` (``"builtin"``),
    the matching in-process handler runs instead. Any exception the
    handler raises is wrapped with the same ``[tool error]`` prefix that
    upstream MCP errors use, so the wire-format ``is_error`` check in
    ``wiki_chat._run_tool_loop`` keeps working unchanged.

    Raises:
        MCPUnavailable: the named server isn't running (route → 503).
        MCPError: transport / timeout / protocol failure (route → 502).
        ValueError: the name doesn't parse as ``<server>__<tool>``.
    """
    server, tool_name = parse_litellm_name(name)

    # Lazy import to avoid a circular import: builtin_tools imports
    # mcp.tools, and mcp.tools importing builtin_tools at module load
    # would deadlock.
    from carrel.mcp.builtin_tools import BUILTIN_SERVER_NAME

    if server == BUILTIN_SERVER_NAME:
        if not builtins or tool_name not in builtins:
            raise MCPUnavailable(
                f"builtin tool {tool_name!r} is not registered"
            )
        try:
            return builtins[tool_name](arguments)
        except Exception as e:
            # Match the upstream MCP error contract so the wire-format
            # `is_error` flag flips on (prefix check in wiki_chat.py).
            return f"[tool error] {e}"

    client = registry.get(server)
    if client is None or not client.is_running:
        raise MCPUnavailable(f"MCP server {server!r} is not running")
    result = await client.call_tool(tool_name, arguments)
    text, is_error = _format_call_tool_result(result)
    if is_error:
        # Surface the upstream's own error message so the model can
        # retry or fall back; a short prefix keeps it visibly distinct
        # from a successful result.
        return f"[tool error] {text}" if text else "[tool error] (no details)"
    return text


# ---------------------------------------------------------------------------
# Optional: rebuild the OpenAI-shaped tool_call dict the model returned
# (used by callers that want to append it to the assistant message verbatim).
# ---------------------------------------------------------------------------


def litellm_arguments(call: dict[str, Any]) -> dict[str, Any]:
    """Parse ``call['function']['arguments']`` (a JSON string) into a dict.

    Falls back to ``{}`` when the JSON is malformed (some models emit
    partial fragments); the dispatcher will then see no arguments.
    """
    fn = call.get("function") or {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("tool call %r has unparseable arguments: %r", fn.get("name"), raw[:200])
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Shared agentic tool loop
# ---------------------------------------------------------------------------


async def run_agentic_loop(
    messages: list[dict[str, Any]],
    *,
    model: str,
    fallback_model: str | None,
    temperature: float,
    timeout: int,
    on_usage: Any,
    tools: list[dict[str, Any]],
    registry: "MCPClientRegistry",
    builtin_handlers: dict[str, Callable[[dict[str, Any]], str]] | None,
    max_iters: int,
    on_iteration: Callable[[list[str], list[dict[str, Any]], int], None] | None = None,
    on_tool_result: Callable[[str, dict[str, Any], str, bool, int], None] | None = None,
    on_cap_hit: Callable[[str], None] | None = None,
) -> str:
    """Drive the model + tool + model loop until the model emits a final
    text answer (no tool calls) or ``max_iters`` is hit.

    Returns the final concatenated text. Tool calls are dispatched via
    :func:`dispatch_tool_call` and their results appended to ``messages``
    in-place so the next iteration has the same ``tool_calls`` array the
    model saw.

    Three optional callbacks let the caller surface per-iteration,
    per-tool, and cap-hit progress (e.g. to a Job's stats or to a
    WebSocket) without re-plumbing the loop. The chat endpoint uses
    these to emit SSE ``tool_call``/``tool_result`` events and a
    terminal error frame on the cap; the enrich endpoint uses them to
    update ``Job.stats`` mid-run.

    When ``tools`` is empty the function degenerates to a single
    non-streaming call (every byte is accumulated into the return
    value, no ``on_iteration`` fires).

    ``on_cap_hit`` (if given) is called when ``max_iters`` is exhausted
    without a final text answer; its argument is the last buffered
    text (may be empty). It does NOT replace the function's return
    value — the caller still gets the same string back. Use it to
    surface a "cap hit" signal that a simple return value can't carry
    (the return is the same whether the model answered cleanly or we
    ran out of budget).
    """
    if not tools:
        chunks: list[str] = []
        for delta in llm.chat_stream(
            messages,
            model=model,
            fallback_model=fallback_model,
            temperature=temperature,
            timeout=timeout,
            feature="agentic",
            on_usage=on_usage,
        ):
            if delta:
                chunks.append(delta)
        return "".join(chunks)

    last_text = ""
    for it in range(max_iters):
        text_buf: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for delta in llm.chat_stream(
            messages,
            model=model,
            fallback_model=fallback_model,
            temperature=temperature,
            timeout=timeout,
            feature="agentic",
            on_usage=on_usage,
            tools=tools,
            on_tool_calls=lambda c: tool_calls.extend(c),
        ):
            if delta:
                text_buf.append(delta)

        if on_iteration is not None:
            try:
                on_iteration(text_buf, tool_calls, it)
            except Exception:  # noqa: BLE001
                logger.warning("on_iteration callback failed", exc_info=True)

        if not tool_calls:
            return "".join(text_buf)

        # Persist the assistant turn verbatim so the next iteration has
        # the same tool_calls array the model saw.
        messages.append(
            {
                "role": "assistant",
                "content": "".join(text_buf) or None,
                "tool_calls": tool_calls,
            }
        )

        for call in tool_calls:
            name = (call.get("function") or {}).get("name") or ""
            args = litellm_arguments(call)
            try:
                result_str = await dispatch_tool_call(
                    registry, name, args, builtins=builtin_handlers
                )
                is_error = result_str.startswith("[tool error]")
            except MCPUnavailable as e:
                result_str, is_error = f"[unavailable] {e}", True
            except MCPError as e:
                result_str, is_error = f"[error] {e}", True
            except ValueError as e:
                result_str, is_error = f"[tool error] {e}", True

            if on_tool_result is not None:
                try:
                    on_tool_result(name, args, result_str, is_error, it)
                except Exception:  # noqa: BLE001
                    logger.warning("on_tool_result callback failed", exc_info=True)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": result_str,
                }
            )
        last_text = "".join(text_buf)

    # Cap hit without a final answer — return whatever the last buffer
    # held so callers can decide whether to mark the job failed.
    if on_cap_hit is not None:
        try:
            on_cap_hit(last_text)
        except Exception:  # noqa: BLE001
            logger.warning("on_cap_hit callback failed", exc_info=True)
    return last_text
