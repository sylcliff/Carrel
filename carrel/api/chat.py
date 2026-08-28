"""Per-paper RAG chat (streaming).

``POST /papers/{paper_id}/chat`` streams an LLM answer to the user's question
over Server-Sent Events, using the paper's embedded chunks as context. Falls
back to the (truncated) parsed Markdown when the paper hasn't been embedded.

Event shapes (all ``data: <json>\\n\\n``):
  * ``{"sources": ["heading", ...]}`` — first frame; which chunks informed the
    answer (or ``["full text (truncated)"]`` for the fallback).
  * ``{"t": "token"}`` — one or more text deltas.
  * ``{"error": "..."}`` — terminal error frame.
  * ``[DONE]`` — terminal success frame (literal, not JSON).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from carrel import embeddings as emb
from carrel import llm, prompts_runtime, usage
from carrel.api.search import _cosine, _decode_embedding
from carrel.db import get_session_dep
from carrel.api._invalidation import invalidate_paper_mutated
from carrel.models import ChatMessage, Chunk, Paper
from carrel.pipeline.summarize import _prepare_body
from carrel.pipeline._llm_recorder import make_record_usage_callback
from carrel.schemas import (
    ChatMessageOut,
    ChatMessagesIn as ChatHistoryIn,
    ChatMessagesOut as ChatHistoryOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/papers", tags=["chat"])

_SYSTEM_PROMPT = (
    "你是论文阅读助手。请只依据下方提供的论文片段回答用户问题。规则：\n"
    "- 回答使用与问题相同的语言（中文问题用中文，英文问题用英文）。\n"
    "- 引用相关内容时注明来自哪个章节标题。\n"
    "- 如果提供的片段不足以回答问题，明确说明，不要编造论文中没有的结果、数字或结论。\n"
    "- 回答简洁、结构清晰，可使用 Markdown。\n"
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


def _retrieve_chunks(
    session: Session, paper: Paper, query: str, top_k: int
) -> tuple[str, list[str]]:
    """Return (context_block, source_headings) for a paper.

    Uses pgvector/SQLite cosine search over this paper's chunks when they exist;
    otherwise falls back to the truncated full markdown.
    """
    chunk_rows = session.exec(
        select(Chunk).where(Chunk.paper_id == paper.id)
    ).all()

    if chunk_rows:
        from carrel.main import app_config  # set during lifespan

        try:
            q_vecs = emb.embed_texts([query], model=app_config.embeddings.model, batch_size=1)
        except Exception as e:  # noqa: BLE001
            logger.warning("chat: embedding query failed, falling back to full text: %s", e)
            q_vecs = []
        if q_vecs:
            q_vec = q_vecs[0]
            if session.get_bind().dialect.name == "postgresql":
                hits = _rank_postgres(session, paper.id, q_vec, top_k)
            else:
                hits = _rank_sqlite(chunk_rows, q_vec, top_k)
            if hits:
                block, sources = _format_chunks(hits)
                return block, sources

    # Fallback: truncated full text.
    if not paper.md_path:
        raise HTTPException(status_code=409, detail="paper has no parsed markdown yet")
    full = Path(_storage_root()) / paper.md_path
    if not full.exists():
        raise HTTPException(status_code=409, detail=f"parsed markdown missing on disk: {full}")
    body = _prepare_body(full.read_text(encoding="utf-8", errors="replace"), _chat_fulltext_chars())
    header = f"Title: {paper.title}\nAuthors: {_authors(paper)}"
    return f"{header}\n\n{body}", ["full text (truncated)"]


def _rank_postgres(
    session: Session, paper_id: str, q_vec: list[float], top_k: int
) -> list[tuple[str | None, str, float]]:
    distance = Chunk.embedding.cosine_distance(q_vec)  # type: ignore[attr-defined]
    stmt = (
        select(Chunk.heading, Chunk.content_md, distance.label("distance"))
        .where(Chunk.paper_id == paper_id)
        .order_by(distance)
        .limit(top_k)
    )
    out: list[tuple[str | None, str, float]] = []
    for heading, content_md, dist in session.exec(stmt).all():
        out.append((heading, content_md, 1.0 - float(dist)))
    return out


def _rank_sqlite(
    chunk_rows: list[Chunk], q_vec: list[float], top_k: int
) -> list[tuple[str | None, str, float]]:
    scored: list[tuple[str | None, str, float]] = []
    for r in chunk_rows:
        vec = _decode_embedding(r.embedding)
        if not vec:
            continue
        scored.append((r.heading, r.content_md, _cosine(q_vec, vec)))
    scored.sort(key=lambda x: x[2], reverse=True)
    return scored[:top_k]


def _format_chunks(
    hits: list[tuple[str | None, str, float]],
) -> tuple[str, list[str]]:
    parts: list[str] = []
    sources: list[str] = []
    for heading, content_md, _score in hits:
        label = heading or "(preamble)"
        parts.append(f"## {label}\n{content_md}")
        sources.append(label)
    return "\n\n".join(parts), sources


def _build_messages(
    paper: Paper,
    context_block: str,
    history: list[ChatTurn],
    history_limit: int,
    *,
    session: Session | None = None,
) -> list[dict[str, str]]:
    context_msg = prompts_runtime.get_user_template(
        "paper_chat", _USER_TEMPLATE, session=session
    ).format(
        title=paper.title or "",
        authors=_authors(paper),
        context_block=context_block,
    )
    # Keep the most recent turns; drop any system turns from the client.
    trimmed = [t for t in history if t.role != "system"][-history_limit:]
    msgs: list[dict[str, str]] = [
        {"role": "system", "content": prompts_runtime.get_system("paper_chat", _SYSTEM_PROMPT, session=session)},
        {"role": "user", "content": context_msg},
    ]
    for t in trimmed:
        msgs.append({"role": t.role, "content": t.content})
    return msgs


_USER_TEMPLATE = (
    "<paper-context>\n"
    "Title: {title}\n"
    "Authors: {authors}\n\n"
    "{context_block}\n"
    "</paper-context>\n\n"
    "依据以上论文片段回答用户的问题。"
)


def _authors(paper: Paper) -> str:
    if not paper.authors:
        return "unknown"
    names = [a.get("name", "") for a in paper.authors if isinstance(a, dict) and a.get("name")]
    return ", ".join(names) or "unknown"


def _storage_root() -> str:
    from carrel.main import app_config

    return str(app_config.storage.root)


def _chat_model() -> str:
    from carrel.main import app_config

    return app_config.llm.chat_model or app_config.llm.summarize_model


def _chat_fallback() -> str | None:
    from carrel.main import app_config

    return app_config.llm.chat_fallback_model or app_config.llm.fallback_model


def _chat_fulltext_chars() -> int:
    from carrel.main import app_config

    return app_config.llm.chat_fulltext_chars


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------


def _sse(payload: str) -> bytes:
    return f"data: {payload}\n\n".encode("utf-8")


def _event(obj: dict[str, Any]) -> bytes:
    return _sse(json.dumps(obj, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/{paper_id}/chat")
def paper_chat(
    paper_id: str,
    req: ChatRequest,
    session: Session = Depends(get_session_dep),
):
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")
    if paper.md_path is None:
        raise HTTPException(status_code=409, detail="paper not parsed yet (run Download & parse first)")

    query = next((t.content for t in reversed(req.messages) if t.role == "user"), "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="no user message found")

    from carrel.main import app_config

    top_k = app_config.llm.rag_top_k
    history_limit = app_config.llm.chat_history_limit

    # One AgentRun per chat turn. Record the RAG retrieval as a step, then
    # the LLM streaming call. The recorder is bound as the ambient one
    # so the existing LLM usage callback (see
    # :mod:`carrel.pipeline._llm_recorder`) attaches token counts to the
    # right step.
    from carrel.agent_recorder import (
        AgentRecorder,
        agent_step,
        clear_current_recorder,
        pipeline_display_name,
        set_current_recorder,
    )
    rec = AgentRecorder(
        session,
        pipeline_id="paper_chat",
        pipeline_name=pipeline_display_name("paper_chat"),
    )
    rec.start(
        context={"paper_id": paper_id, "query": query[:200]},
        paper_id=paper_id,
        subject=paper.title[:200] if paper.title else None,
    )
    rec_token = set_current_recorder(rec)
    try:
        with agent_step(
            "retrieve",
            label="RAG retrieve",
            kind="step",
            detail={"top_k": top_k, "query_chars": len(query)},
        ) as step:
            context_block, sources = _retrieve_chunks(session, paper, query, top_k)
            step.set_output(f"{len(sources)} sources")
    except Exception:
        clear_current_recorder(rec_token)
        rec.finish(status="failed", error="retrieve failed")
        raise

    messages = _build_messages(paper, context_block, req.messages, history_limit, session=session)

    model = _chat_model()
    fallback = _chat_fallback()
    temperature = app_config.llm.chat_temperature
    timeout = app_config.llm.request_timeout_seconds

    def generate():
        # Sources frame first so the UI can render provenance before tokens.
        yield _event({"sources": sources})
        with agent_step(
            "llm",
            label="LLM answer",
            kind="llm",
            feature="paper_chat",
        ):
            try:
                for delta in llm.chat_stream(
                    messages,
                    model=model,
                    fallback_model=fallback,
                    temperature=temperature,
                    timeout=timeout,
                    feature="paper_chat",
                    on_usage=make_record_usage_callback(
                        session, paper_id=paper_id, feature="paper_chat"
                    ),
                ):
                    yield _event({"t": delta})
            except Exception as e:  # noqa: BLE001 - surface any LLM error on the stream
                logger.warning("chat stream for %s failed: %s", paper_id, e)
                yield _event({"error": str(e)})
                rec.finish(status="failed", error=f"{type(e).__name__}: {e}")
                clear_current_recorder(rec_token)
                return
        yield _sse(_DONE)
        rec.finish(summary={"paper_id": paper_id, "sources": len(sources)})
        clear_current_recorder(rec_token)

    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # don't let proxies buffer the stream
        },
    )


# ---------------------------------------------------------------------------
# Persisted transcript (cross-device / cross-browser)
# ---------------------------------------------------------------------------

# Reject absurdly large transcripts so a runaway client can't blow up the DB.
_MAX_TURNS = 500
_MAX_CONTENT_CHARS = 100_000


@router.get("/{paper_id}/chat/messages", response_model=ChatHistoryOut)
def get_chat_messages(
    paper_id: str,
    session: Session = Depends(get_session_dep),
) -> ChatHistoryOut:
    """Return the paper's saved chat transcript, oldest turn first."""
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")
    rows = session.exec(
        select(ChatMessage)
        .where(ChatMessage.paper_id == paper_id)
        .order_by(ChatMessage.id)
    ).all()
    latest = rows[-1].updated_at if rows else None
    return ChatHistoryOut(
        paper_id=paper_id,
        messages=[
            ChatMessageOut(
                id=r.id,  # type: ignore[arg-type]
                role=r.role,
                content=r.content,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ],
        updated_at=latest,
    )


@router.put("/{paper_id}/chat/messages", response_model=ChatHistoryOut)
def put_chat_messages(
    paper_id: str,
    body: ChatHistoryIn,
    session: Session = Depends(get_session_dep),
) -> ChatHistoryOut:
    """Replace the paper's chat transcript with ``messages`` (whole-document PUT).

    Same shape as notes: the client sends the full ordered list of turns and
    the server replaces what it stored. Only ``user``/``assistant`` turns are
    kept; empty content is dropped. Bumps ``papers.updated_at``.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")

    if len(body.messages) > _MAX_TURNS:
        raise HTTPException(
            status_code=413,
            detail=f"too many messages (max {_MAX_TURNS})",
        )

    now = datetime.now(UTC)
    # Replace all rows for this paper. Delete-then-insert is simplest and the
    # transcript is small (<500 turns); ids change, which is fine for a
    # single-user replace-all store.
    existing = session.exec(
        select(ChatMessage).where(ChatMessage.paper_id == paper_id)
    ).all()
    for row in existing:
        session.delete(row)

    for turn in body.messages:
        role = turn.role.strip().lower()
        content = turn.content.strip()
        if role not in ("user", "assistant"):
            continue
        if not content or len(content) > _MAX_CONTENT_CHARS:
            continue
        session.add(
            ChatMessage(
                paper_id=paper_id,
                role=role,
                content=content,
                created_at=now,
                updated_at=now,
            )
        )

    paper.updated_at = now
    session.add(paper)
    session.commit()
    # L2: chat transcript changed; the per-paper detail entry carries
    # a chat-derived field, so we drop it for precision. No list impact.
    invalidate_paper_mutated(paper_id, mutate={"chat"})

    return get_chat_messages(paper_id, session)
