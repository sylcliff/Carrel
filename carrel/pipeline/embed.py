"""Embedding pipeline (M5).

Drives a paper through the final state-machine step::

    parsed --(chunk + embed)--> ready

Reads the parsed Markdown at ``Paper.md_path`` (relative to
``cfg.storage.root``), splits it into chunks, embeds each chunk via Ark, and
writes one row per chunk to the ``chunks`` table. The Paper row's
``status`` becomes ``ready`` on success or ``failed`` on error (with the
message on ``Paper.error``).

Idempotent: if the paper already has chunks, the step is a no-op (matching
how M3 reuses on-disk PDFs/MD to skip work).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, delete, select

from carrel import embeddings as emb
from carrel.chunking import chunk_markdown
from carrel.config import CarrelYAML
from carrel.models import Chunk, Paper, PaperStatus

logger = logging.getLogger(__name__)


class EmbedError(Exception):
    """Embedding failed for a paper; will mark the row failed."""


# Mirrors process.ProgressCallback shape.
ProgressCallback = Callable[[dict], None]


def embed_paper(
    session: Session,
    cfg: CarrelYAML,
    paper_id: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> Paper:
    """Chunk + embed one paper; advance its status to ``ready``."""
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise EmbedError(f"paper not found: {paper_id}")

    def _emit(progress: dict) -> None:
        if on_progress is not None:
            on_progress({
                "paper_id": paper.id,
                "paper_title": paper.title,
                "stage": "embed",
                **progress,
            })

    if not paper.md_path:
        raise EmbedError("paper has no md_path; run /process first")

    md_path = Path(cfg.storage.root) / paper.md_path
    if not md_path.exists():
        raise EmbedError(f"parsed markdown missing on disk: {md_path}")

    md = md_path.read_text(encoding="utf-8", errors="replace")

    # Idempotency: if chunks already exist and the paper is already ready,
    # don't redo the embedding call (it's the expensive step).
    existing = session.exec(
        select(Chunk).where(Chunk.paper_id == paper.id)
    ).all()
    if existing and paper.status == PaperStatus.ready.value:
        _emit({"detail": f"Already embedded ({len(existing)} chunks)"})
        return paper

    # Reset to parsed for the fresh attempt, then wipe stale chunks.
    paper.error = None
    paper.status = PaperStatus.parsed.value
    if existing:
        session.exec(delete(Chunk).where(Chunk.paper_id == paper.id))
        session.commit()

    _emit({"detail": "Chunking Markdown…"})
    chunks = chunk_markdown(
        md,
        target_tokens=cfg.chunking.target_tokens,
        overlap_tokens=cfg.chunking.overlap_tokens,
        min_tokens=cfg.chunking.min_tokens,
    )
    if not chunks:
        # A paper with no body content is not embeddable; keep it parsed.
        paper.status = PaperStatus.parsed.value
        paper.error = "no chunkable content in parsed markdown"
        paper.updated_at = datetime.now(UTC)
        session.add(paper)
        session.commit()
        _emit({"detail": "No chunkable content"})
        return paper

    _emit({"detail": f"Embedding {len(chunks)} chunks…"})
    try:
        vectors = emb.embed_texts(
            [c.content_md for c in chunks],
            model=cfg.embeddings.model,
            batch_size=cfg.embeddings.batch_size,
            timeout=cfg.embeddings.request_timeout_seconds,
        )
        expected_dim = cfg.embeddings.dim
        for vec in vectors:
            if len(vec) != expected_dim:
                raise EmbedError(
                    f"embedding dim {len(vec)} != configured {expected_dim}; "
                    f"check embeddings.dim in config.yaml"
                )
    except EmbedError as e:
        # Config-level error: leave failed so the user sees it.
        paper.status = PaperStatus.failed.value
        paper.error = str(e)[:1000]
        paper.updated_at = datetime.now(UTC)
        session.add(paper)
        session.commit()
        logger.warning("embed %s failed: %s", paper_id, e)
        raise
    except Exception as e:
        # Transient errors: mark failed (a retry will move it back to parsed).
        paper.status = PaperStatus.failed.value
        paper.error = f"{type(e).__name__}: {e}"[:1000]
        paper.updated_at = datetime.now(UTC)
        session.add(paper)
        session.commit()
        logger.warning("embed %s failed: %s", paper_id, e)
        raise EmbedError(str(e)) from e

    for c, vec in zip(chunks, vectors, strict=True):
        session.add(Chunk(
            paper_id=paper.id,
            chunk_index=c.index,
            heading=c.heading or None,
            content_md=c.content_md,
            token_count=c.token_count,
            embedding=vec,
        ))

    paper.status = PaperStatus.ready.value
    paper.updated_at = datetime.now(UTC)
    session.add(paper)
    session.commit()
    session.refresh(paper)
    _emit({"detail": f"Done — {len(chunks)} chunks"})
    logger.info("embedded %s -> %d chunks", paper.id, len(chunks))
    return paper


def select_pending_embed(session: Session, limit: int = 20) -> list[Paper]:
    """Papers that are parsed but not yet ready (or previously failed embed)."""
    stmt = (
        select(Paper)
        .where(
            Paper.in_library.is_(True),
            Paper.md_path.is_not(None),
            Paper.status.in_([
                PaperStatus.parsed.value,
                PaperStatus.summarized.value,
                PaperStatus.failed.value,
            ]),
        )
        .order_by(Paper.created_at.desc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def embed_pending(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 20,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Embed a batch of parsed papers; returns counts {candidates, ready, failed}."""
    papers = select_pending_embed(session, limit=limit)
    counts = {"candidates": len(papers), "ready": 0, "failed": 0}
    total = len(papers)

    def _wrap(i: int, title: str):
        def _cb(progress: dict) -> None:
            if on_progress is not None:
                on_progress({**progress, "index": i, "total": total, "title": title})
        return _cb

    for i, paper in enumerate(papers, start=1):
        try:
            embed_paper(session, cfg, paper.id, on_progress=_wrap(i, paper.title))
            counts["ready"] += 1
        except EmbedError:
            counts["failed"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("embed %s crashed: %s", paper.id, e)
            counts["failed"] += 1

    logger.info(
        "embed batch done: candidates=%d ready=%d failed=%d",
        counts["candidates"], counts["ready"], counts["failed"],
    )
    return counts
