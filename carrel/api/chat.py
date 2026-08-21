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
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from carrel import embeddings as emb
from carrel import llm
from carrel.api.search import _cosine, _decode_embedding
from carrel.db import get_session_dep
from carrel.models import Chunk, Paper
from carrel.pipeline.summarize import _prepare_body

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/papers", tags=["chat"])

_SYSTEM_PROMPT = (
    "你是论文阅读助手。请只依据下方提供的论文片段回答用户问题。规则：\n"
    "- 回答使用与问题相同的语言（中文问题用中文，英文问题用英文）。\n"
    "- 引用相关内容时注明来自哪个章节标题。\n"
    "- 如果提供的片段不足以回答问题，明确说明，不要编造论文中没有的结果、数字或结论。\n"
    "- 回答简洁、结构清晰，可使用 Markdown。"
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
    paper: Paper, context_block: str, history: list[ChatTurn], history_limit: int
) -> list[dict[str, str]]:
    context_msg = (
        f"<paper-context>\n"
        f"Title: {paper.title}\n"
        f"Authors: {_authors(paper)}\n\n"
        f"{context_block}\n"
        f"</paper-context>\n\n"
        f"依据以上论文片段回答用户的问题。"
    )
    # Keep the most recent turns; drop any system turns from the client.
    trimmed = [t for t in history if t.role != "system"][-history_limit:]
    msgs: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": context_msg},
    ]
    for t in trimmed:
        msgs.append({"role": t.role, "content": t.content})
    return msgs


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
    context_block, sources = _retrieve_chunks(session, paper, query, top_k)
    messages = _build_messages(paper, context_block, req.messages, history_limit)

    model = _chat_model()
    fallback = _chat_fallback()
    temperature = app_config.llm.chat_temperature
    timeout = app_config.llm.request_timeout_seconds

    def generate():
        # Sources frame first so the UI can render provenance before tokens.
        yield _event({"sources": sources})
        try:
            for delta in llm.chat_stream(
                messages,
                model=model,
                fallback_model=fallback,
                temperature=temperature,
                timeout=timeout,
            ):
                yield _event({"t": delta})
        except Exception as e:  # noqa: BLE001 - surface any LLM error on the stream
            logger.warning("chat stream for %s failed: %s", paper_id, e)
            yield _event({"error": str(e)})
            return
        yield _sse(_DONE)

    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # don't let proxies buffer the stream
        },
    )
