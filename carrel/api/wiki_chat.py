"""Wiki-wide RAG chat (M12, streaming).

``POST /wiki/chat`` streams an LLM answer over Server-Sent Events using the
*union* of wiki pages as context. Unlike :mod:`carrel.api.chat` (per-paper,
chunks), this endpoint picks the top-k wiki pages by cosine similarity over
each page's synopsis embedding (``WikiPage.embedding``, HALFVEC(2048) on
Postgres with HNSW; JSON-encoded list on SQLite) and reads their full bodies
from disk.

Event shapes (all ``data: <json>\\n\\n``):
  * ``{"sources": [{"kind": "...", "slug": "...", "title": "..."}, ...]}`` —
    the wiki pages that informed the answer. The first frame.
  * ``{"t": "token"}`` — one or more text deltas.
  * ``{"type": "tool_call", "name": "...", "args": {...}}`` — the model
    decided to call an MCP tool. Issued only when MCP tools are configured
    and running; the model is given the live tool list at every iteration.
  * ``{"type": "tool_result", "name": "...", "content": "...", "is_error":
    bool}`` — what the tool returned. ``is_error`` is true for transport
    failures and upstream ``CallToolResult.is_error``; the model sees the
    text content either way and decides how to answer.
  * ``{"error": "..."}`` — terminal error frame.
  * ``[DONE]`` — terminal success frame (literal, not JSON).

The transcript is server-persisted in :class:`carrel.models.WikiChatMessage`
(``GET/PUT /wiki/chat/messages``) so the conversation follows the user across
devices and browsers — same pattern as the per-paper chat.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from carrel import embeddings as emb
from carrel import llm, prompts_runtime, usage
from carrel.api.search import _cosine, _decode_embedding
from carrel.db import get_session_dep
from carrel.mcp import (
    MCPError,
    MCPUnavailable,
    builtin_dispatch_map,
    collect_builtin_tools,
    collect_tools,
    dispatch_tool_call,
    get_mcp,
    litellm_arguments,
)
from carrel.mcp.tools import run_agentic_loop
from carrel.models import WikiChatMessage, WikiPage
from carrel.pipeline._llm_recorder import make_record_usage_callback
from carrel.agent_recorder import (
    AgentRecorder,
    agent_step,
    clear_current_recorder,
    pipeline_display_name,
    set_current_recorder,
)
from carrel.schemas import (
    ChatMessageOut,
    WikiChatMessagesIn,
    WikiChatMessagesOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wiki", tags=["wiki-chat"])


_SYSTEM_PROMPT = (
    "你是 wiki 阅读助手。下面会提供多个 wiki 页面（scholar / concept / question）"
    "的内容作为上下文。规则：\n"
    "- 只依据下方提供的 wiki 页面回答用户问题。\n"
    "- 回答使用与问题相同的语言（中文问题用中文，英文问题用英文）。\n"
    "- 引用某条信息时，注明它来自哪一个 wiki 页面（用页面标题，或"
    " \"concept:<term>\" / \"scholar:<name>\" / \"question:<text>\"）。\n"
    "- 多个页面给出不同观点时，并列陈述并分别注明来源。\n"
    "- 如果提供的页面不足以回答问题，明确说明，不要编造页面里没有的内容。\n"
    "- 回答简洁、结构清晰，可使用 Markdown 和 wiki 链接"
    "（[...](concepts/foo.md) / [...](scholars/bar.md) / [...](questions/baz.md)）。\n"
    "- 所有数学公式必须用 TeX 定界符包裹，前端才能渲染：行内公式用 $...$（如 $E=mc^2$），"
    "独立成行的公式用 $$...$$。不要使用 Unicode 上下标或纯文本写法代替公式。"
)

# Sentinel terminal value consumed by the SSE generator.
_DONE = "[DONE]"


class ChatTurn(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatTurn] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Context retrieval
# ---------------------------------------------------------------------------


def _is_postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def _storage_root() -> str:
    from carrel.main import app_config

    return str(app_config.storage.root)


def _top_k_pages_postgres(
    session: Session, q_vec: list[float], top_k: int
) -> list[WikiPage]:
    """Cosine top-k over WikiPage.embedding via pgvector. Returns the page
    rows ordered by ascending cosine distance (most similar first)."""
    distance = WikiPage.embedding.cosine_distance(q_vec)  # type: ignore[attr-defined]
    stmt = (
        select(WikiPage, distance.label("distance"))
        .where(WikiPage.embedding.is_not(None))
        .where(WikiPage.redirects_to.is_(None))
        .order_by(distance)
        .limit(top_k)
    )
    return [row for row, _dist in session.exec(stmt).all()]


def _top_k_pages_sqlite(
    session: Session, q_vec: list[float], top_k: int
) -> list[WikiPage]:
    """In-Python cosine scan over JSON-decoded WikiPage.embedding vectors."""
    rows = session.exec(
        select(WikiPage)
        .where(WikiPage.embedding.is_not(None))
        .where(WikiPage.redirects_to.is_(None))
    ).all()
    scored: list[tuple[float, WikiPage]] = []
    for r in rows:
        vec = _decode_embedding(r.embedding)
        if not vec:
            continue
        scored.append((_cosine(q_vec, vec), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _score, r in scored[:top_k]]


def _fallback_pages(session: Session, top_k: int) -> list[WikiPage]:
    """Most-recently-compiled pages, used when embeddings are unavailable
    or the query vector is unusable."""
    return list(
        session.exec(
            select(WikiPage)
            .where(WikiPage.redirects_to.is_(None))
            .order_by(WikiPage.compiled_at.desc().nullslast())
            .limit(top_k)
        ).all()
    )


def _read_body(path: str) -> str:
    """Read a wiki page's body from disk, returning just the body
    (frontmatter stripped, truncated to ``char_cap`` chars)."""
    from pathlib import Path

    full = Path(_storage_root()) / path
    if not full.exists():
        return ""
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except OSError as e:  # noqa: BLE001
        logger.warning("wiki chat: failed to read %s: %s", full, e)
        return ""
    # Strip the frontmatter (everything between the first pair of "---"
    # lines at the start of the file) so the model sees just the prose.
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + 5 :]
    return text


def _retrieve_pages(
    session: Session, query: str
) -> tuple[list[WikiPage], list[dict[str, str]]]:
    """Return ``(pages, sources)`` for a query.

    Tries top-k by page embedding first; if the embedding call fails or every
    score is zero, falls back to the most-recently-compiled pages. Returns an
    empty list when the wiki is empty (the route emits an SSE error frame
    in that case).
    """
    from carrel.main import app_config  # noqa: PLC0415 — set during lifespan

    top_k = app_config.llm.wiki_chat_top_k

    q_vec: list[float] | None = None
    try:
        vecs = emb.embed_texts(
            [query], model=app_config.embeddings.model, batch_size=1
        )
        if vecs:
            q_vec = vecs[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("wiki chat: embedding query failed, using fallback: %s", e)

    pages: list[WikiPage] = []
    if q_vec:
        try:
            if _is_postgres(session):
                pages = _top_k_pages_postgres(session, q_vec, top_k)
            else:
                pages = _top_k_pages_sqlite(session, q_vec, top_k)
        except Exception as e:  # noqa: BLE001
            logger.warning("wiki chat: vector search failed, using fallback: %s", e)
            pages = []

    if not pages:
        pages = _fallback_pages(session, top_k)

    sources = [
        {"kind": p.kind, "slug": p.slug, "title": p.title or ""} for p in pages
    ]
    return pages, sources


def _build_context_block(pages: list[WikiPage], char_cap: int) -> str:
    parts: list[str] = []
    for i, page in enumerate(pages, start=1):
        body = _read_body(page.path)
        if len(body) > char_cap:
            body = body[:char_cap] + "\n…(truncated)…"
        parts.append(
            f"Page {i} ({page.kind}:{page.slug})\n"
            f"Title: {page.title}\n"
            f"{body}"
        )
    return "\n\n----\n\n".join(parts)


def _build_messages(
    context_block: str,
    history: list[ChatTurn],
    history_limit: int,
    *,
    session: Session | None = None,
) -> list[dict[str, str]]:
    context_msg = prompts_runtime.get_user_template(
        "wiki_chat", _USER_TEMPLATE, session=session
    ).format(context_block=context_block)
    # Keep the most recent turns; drop any system turns from the client.
    trimmed = [t for t in history if t.role != "system"][-history_limit:]
    msgs: list[dict[str, str]] = [
        {"role": "system", "content": prompts_runtime.get_system("wiki_chat", _SYSTEM_PROMPT, session=session)},
        {"role": "user", "content": context_msg},
    ]
    for t in trimmed:
        msgs.append({"role": t.role, "content": t.content})
    return msgs


_USER_TEMPLATE = (
    "<wiki-context>\n{context_block}\n</wiki-context>\n\n"
    "依据以上 wiki 页面回答用户的问题。"
)


def _chat_model() -> str:
    from carrel.main import app_config

    return app_config.llm.chat_model or app_config.llm.summarize_model


def _chat_fallback() -> str | None:
    from carrel.main import app_config

    return app_config.llm.chat_fallback_model or app_config.llm.fallback_model


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------


def _sse(payload: str) -> bytes:
    return f"data: {payload}\n\n".encode("utf-8")


def _event(obj: dict[str, Any]) -> bytes:
    return _sse(json.dumps(obj, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Tool loop (MCP-backed function calling)
# ---------------------------------------------------------------------------


async def _run_tool_loop(
    messages: list[dict[str, Any]],
    *,
    model: str,
    fallback: str | None,
    temperature: float,
    timeout: int,
    on_usage: Any,
    tools: list[dict[str, Any]],
    registry: Any,
    builtin_handlers: dict[str, Any] | None,
    max_iters: int,
) -> AsyncIterator[bytes]:
    """Run the model + tool + model loop, yielding SSE-ready bytes.

    Thin wrapper around :func:`carrel.mcp.tools.run_agentic_loop`. The
    shared helper owns the message bookkeeping; this function only
    translates per-iteration deltas and per-tool results into the SSE
    frames the front-end expects (``{"t": ...}`` for text, ``{"type":
    "tool_call", ...}`` / ``{"type": "tool_result", ...}`` for tools,
    ``{"error": ...}`` on LLM-stream failure or on the cap hit).

    When ``tools`` is empty we stream the response directly so the
    front-end sees text deltas as they arrive (the shared helper
    accumulates them internally for the no-tools code path).
    """
    if not tools:
        try:
            for delta in llm.chat_stream(
                messages,
                model=model,
                fallback_model=fallback,
                temperature=temperature,
                timeout=timeout,
                feature="wiki_chat",
                on_usage=on_usage,
            ):
                yield _event({"t": delta})
        except Exception as e:  # noqa: BLE001
            logger.warning("wiki chat stream failed: %s", e)
            yield _event({"error": str(e)})
        return

    cap_hit = False

    def _on_iter(text_buf: list[str], tool_calls: list[dict[str, Any]], _it: int) -> None:
        # When the model produced a final answer (no tool calls) we
        # flush the text now. When it produced tool calls we stay
        # silent — the agent hasn't finished yet, and the buffered text
        # (any preamble) is persisted into messages[] for the next
        # iteration.
        if not tool_calls:
            for d in text_buf:
                pending.append(_event({"t": d}))

    def _on_tool(
        name: str,
        args: dict[str, Any],
        result_str: str,
        is_error: bool,
        _it: int,
    ) -> None:
        pending.append(_event({"type": "tool_call", "name": name, "args": args}))
        pending.append(_event({
            "type": "tool_result",
            "name": name,
            "content": result_str,
            "is_error": is_error,
        }))

    pending: list[bytes] = []

    def _on_cap(_text: str) -> None:
        nonlocal cap_hit
        cap_hit = True

    try:
        await run_agentic_loop(
            messages,
            model=model,
            fallback_model=fallback,
            temperature=temperature,
            timeout=timeout,
            on_usage=on_usage,
            tools=tools,
            registry=registry,
            builtin_handlers=builtin_handlers,
            max_iters=max_iters,
            on_iteration=_on_iter,
            on_tool_result=_on_tool,
            on_cap_hit=_on_cap,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("wiki chat stream failed: %s", e)
        pending.append(_event({"error": str(e)}))

    for frame in pending:
        yield frame
    if cap_hit:
        yield _event({"error": f"wiki chat tool loop exceeded max iterations ({max_iters})"})


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/chat")
async def wiki_chat(
    req: ChatRequest,
    session: Session = Depends(get_session_dep),
):
    from carrel.main import app_config  # noqa: PLC0415

    query = next((t.content for t in reversed(req.messages) if t.role == "user"), "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="no user message found")

    pages, sources = _retrieve_pages(session, query)
    if not pages:
        # Empty wiki — surface a friendly error on the stream and stop.
        from fastapi.responses import StreamingResponse

        async def _empty_gen():
            yield _event({"error": "wiki is empty — run Compile wiki first"})
            yield _sse(_DONE)

        return StreamingResponse(
            _empty_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    char_cap = app_config.llm.wiki_chat_fulltext_chars
    history_limit = app_config.llm.chat_history_limit
    context_block = _build_context_block(pages, char_cap)
    messages = _build_messages(context_block, req.messages, history_limit, session=session)

    model = _chat_model()
    fallback = _chat_fallback()
    temperature = app_config.llm.chat_temperature
    timeout = app_config.llm.request_timeout_seconds
    max_iters = app_config.llm.wiki_chat_max_tool_iterations

    # Live tool list from the MCP registry + in-process builtins. The
    # builtins (e.g. ``builtin__save_scholar_note``) run inside the FastAPI
    # process and let the agent write to the wiki, which MCP servers can't
    # easily do. ``collect_tools`` returns [] (or just the builtins) when MCP
    # is disabled / not started, which keeps today's behavior intact on
    # installs that don't run any MCP servers.
    registry = get_mcp()
    builtin_handlers = builtin_dispatch_map()
    tools = collect_tools(registry, builtins=collect_builtin_tools())
    on_usage = make_record_usage_callback(session, paper_id="", feature="wiki_chat")

    # Bind a recorder for this turn. We capture the wiki-page retrieval as
    # a step, then the agentic tool loop as a single LLM step (each inner
    # model call is a token-usage event, not a separate step — the loop
    # is one logical "agent run" from the user's perspective).
    rec = AgentRecorder(
        session,
        pipeline_id="wiki_chat",
        pipeline_name=pipeline_display_name("wiki_chat"),
    )
    rec.start(
        context={"query": query[:200]},
        subject=query[:200] or None,
    )

    async def generate():
        # Sources frame first so the UI can render provenance before tokens.
        yield _event({"sources": sources})
        # Bind the recorder for the duration of this async generator.
        # ContextVar tokens are per-async-context so we set + reset here
        # rather than in the outer sync handler.
        rec_token = set_current_recorder(rec)
        try:
            with agent_step(
                "llm",
                label="Wiki LLM agent (tool loop)",
                kind="llm",
                feature="wiki_chat",
            ):
                try:
                    async for frame in _run_tool_loop(
                        messages,
                        model=model,
                        fallback=fallback,
                        temperature=temperature,
                        timeout=timeout,
                        on_usage=on_usage,
                        tools=tools,
                        registry=registry,
                        builtin_handlers=builtin_handlers,
                        max_iters=max_iters,
                    ):
                        yield frame
                except Exception as e:  # noqa: BLE001 - surface on the stream
                    logger.warning("wiki chat tool loop failed: %s", e)
                    yield _event({"error": str(e)})
                    rec.finish(status="failed", error=f"{type(e).__name__}: {e}")
                    return
        finally:
            try:
                clear_current_recorder(rec_token)
            except ValueError:
                # Token was created in a different context (shouldn't
                # happen now that we set it inside the generator, but
                # be defensive — we don't want to crash the stream on
                # cleanup).
                pass
        yield _sse(_DONE)
        rec.finish(summary={"sources": len(sources)})

    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Persisted transcript (cross-device / cross-browser)
# ---------------------------------------------------------------------------

# Reject absurdly large transcripts so a runaway client can't blow up the DB.
_MAX_TURNS = 500
_MAX_CONTENT_CHARS = 100_000


def _to_out(r: WikiChatMessage) -> ChatMessageOut:
    return ChatMessageOut(
        id=r.id or 0,
        role=r.role,
        content=r.content,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.get("/chat/messages", response_model=WikiChatMessagesOut)
def get_wiki_chat_messages(
    session: Session = Depends(get_session_dep),
) -> WikiChatMessagesOut:
    """Return the global wiki chat transcript, oldest turn first."""
    rows = session.exec(
        select(WikiChatMessage).order_by(WikiChatMessage.id)
    ).all()
    latest = rows[-1].updated_at if rows else None
    return WikiChatMessagesOut(
        messages=[_to_out(r) for r in rows],
        updated_at=latest,
    )


@router.put("/chat/messages", response_model=WikiChatMessagesOut)
def put_wiki_chat_messages(
    body: WikiChatMessagesIn,
    session: Session = Depends(get_session_dep),
) -> WikiChatMessagesOut:
    """Replace the wiki chat transcript with ``messages`` (whole-document PUT).

    Only ``user``/``assistant`` turns are kept; empty content is dropped.
    Bumps ``updated_at`` server-side. Same shape as the per-paper chat.
    """
    msgs_in = body.messages

    if len(msgs_in) > _MAX_TURNS:
        raise HTTPException(
            status_code=413,
            detail=f"too many messages (max {_MAX_TURNS})",
        )

    now = datetime.now(UTC)
    # Delete-then-insert is simplest and the transcript is small (<500 turns);
    # ids change, which is fine for a single-user replace-all store.
    existing = session.exec(select(WikiChatMessage)).all()
    for row in existing:
        session.delete(row)

    for turn in msgs_in:
        role = turn.role.strip().lower()
        content = turn.content.strip()
        if role not in ("user", "assistant"):
            continue
        if not content or len(content) > _MAX_CONTENT_CHARS:
            continue
        session.add(
            WikiChatMessage(
                role=role,
                content=content,
                created_at=now,
                updated_at=now,
            )
        )

    session.commit()
    return get_wiki_chat_messages(session=session)
