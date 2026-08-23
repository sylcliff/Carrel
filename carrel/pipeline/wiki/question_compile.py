"""Compile a question wiki page from per-paper extractions (M8b).

For each normalized question the LLM extracted from at least one paper body,
this synthesizes a Markdown page that names the question, summarizes how the
library addresses it (or doesn't), and links back to the contributing papers.
Inputs come from :mod:`carrel.pipeline.paper_extract` (rows in
``paper_questions``); the compiler itself never re-reads paper text.

This module is the structural twin of
:mod:`carrel.pipeline.wiki.concept_compile` — same threshold / hash-skip /
stub / reconcile patterns — but the LLM prompt is lighter: a single
``summary`` paragraph plus a one-clause ``why_it_matters`` field.  Concepts
get tags; questions get ``question_status`` (the existing
``WikiPage.question_status`` column at ``models.py:370``), defaulting to
``"open"`` and reserved for a future iteration where the LLM proposes a
status.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from carrel import embeddings, llm
from carrel.config import CarrelYAML
from carrel.models import WikiKind, WikiPage, WikiSource
from carrel.pipeline.summarize import _prepare_body
from carrel.pipeline.wiki import _frontmatter, _merge, _reindex, _slug
from carrel.pipeline.wiki._questions_agg import (
    EVIDENCE_THRESHOLD,
    aggregate,
    papers_for_question,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]

COMPILER_VERSION = 1
_MAX_PAPERS = 25
_MAX_INPUT_CHARS = 8_000
_EMBED_CHARS = 1500
# Status applied to every newly-compiled question page.  v1 does not ask the
# LLM to set status; a future iteration can use the LLM to flip rows to
# ``contested``, ``partially_solved``, or ``resolved`` based on the evidence
# pack.
DEFAULT_QUESTION_STATUS = "open"


class QuestionError(Exception):
    """Question compilation failed (no key, bad LLM output, IO error, etc.)."""


_SYSTEM_PROMPT = (
    "You are an expert research librarian writing an open-questions page for a "
    "personal research library. A question is a research problem the library's "
    "papers keep raising (e.g. \"How can retrieval stay current with fast-moving "
    "knowledge bases?\").\n"
    "Rules:\n"
    "- Ground every claim in the supplied paper snippets.\n"
    "- summary: one short paragraph (≤ 80 words) describing the question and "
    "the state of evidence in the library.\n"
    "- why_it_matters: a single short clause (≤ 30 words) on why this question "
    "is worth tracking.\n"
    "- confidence: your estimate 0..1 (capped by evidence quantity later).\n"
    "- Respond with ONLY a JSON object of the form: "
    '{"summary": "...", "why_it_matters": "...", "confidence": 0.0}'
)


def _paper_snippet(idx: int, paper: Any) -> str:
    bits = [f"[{idx}] {paper.title}"]
    venue = paper.venue or "unknown venue"
    year = paper.publication_date.year if paper.publication_date else "n.d."
    bits.append(f"    Venue/year: {venue} · {year}")
    body = paper.tldr_en or paper.abstract or ""
    if body:
        bits.append(f"    Abstract: {_prepare_body(body, 1000)}")
    return "\n".join(bits)


def _build_user_prompt(*, question_display, papers, old_body):
    parts = [f"Question: {question_display}"]
    parts.append(
        f"Library papers raising this question ({len(papers)} shown, newest first):"
    )
    parts.extend(_paper_snippet(i, p) for i, p in enumerate(papers, start=1))
    if old_body:
        parts.append(
            "Previous version of this page (revise and update):\n"
            + _prepare_body(old_body, 2500)
        )
    parts.append("\nReturn the JSON object now, with no commentary.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_live_body(*, question_display, data, papers):
    summary = str(data.get("summary") or "").strip()
    why = str(data.get("why_it_matters") or "").strip()
    lines = [f"# {question_display}", ""]
    if summary:
        lines += ["## Summary", summary, ""]
    if why:
        lines += ["## Why it matters", why, ""]
    if papers:
        lines += ["## Sources", ""]
        for i, p in enumerate(papers, start=1):
            year = p.publication_date.year if p.publication_date else "n.d."
            lines.append(f"[^{i}]: [{p.title}](/papers/{p.id}) ({year})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_stub_body(*, question_display, paper_count):
    # Stubs are written only when there is no evidence at all (defensive:
    # aggregation requires ≥1 backing paper, so a real stub with paper_count=0
    # should not happen in practice).  We keep this path so a future
    # zero-evidence entity is never an LLM call.
    noun = "paper" if paper_count == 1 else "papers"
    return (
        f"# {question_display}\n\n"
        f"_Not enough evidence to compile yet "
        f"(currently {paper_count} {noun})._\n"
    )


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def _confidence(model_value, *, evidence_count):
    try:
        conf = float(model_value)
    except (TypeError, ValueError):
        conf = 0.4
    conf = max(0.0, min(1.0, conf))
    # M8c: with the evidence threshold dropped to 1, a single-paper question
    # still gets a live LLM-compiled page; we no longer cap single-paper
    # confidence artificially low — let the LLM's own self-rating stand.
    if evidence_count <= 5:
        ceiling = 0.85
    else:
        ceiling = 0.9
    return round(min(conf, ceiling), 3)


# ---------------------------------------------------------------------------
# Atomic write + DB upsert
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
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


def _evidence_hash(paper_ids):
    return hashlib.sha256(",".join(sorted(paper_ids)).encode("utf-8")).hexdigest()


def _embed_page(cfg, title, summary, why):
    text = " ".join([title, summary, why]).strip()[:_EMBED_CHARS]
    if not text:
        return None
    try:
        vecs = embeddings.embed_texts([text], model=cfg.embeddings.model)
        return vecs[0] if vecs else None
    except Exception as e:  # noqa: BLE001
        logger.warning("wiki question embed failed: %s", e)
        return None


def _existing_page(session, slug):
    return session.exec(
        select(WikiPage).where(
            WikiPage.kind == WikiKind.question.value,
            WikiPage.slug == slug,
            WikiPage.redirects_to.is_(None),
        )
    ).first()


def _candidate_for_question(candidates, question_normalized):
    for c in candidates:
        if c.question_normalized == question_normalized:
            return c
    return None


# ---------------------------------------------------------------------------
# Per-question compile
# ---------------------------------------------------------------------------


def compile_question(
    session: Session,
    cfg: CarrelYAML,
    question_normalized: str,
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> WikiPage:
    """Compile (or recompile) one question's wiki page.  Raises QuestionError."""
    candidates = aggregate(session)
    candidate = _candidate_for_question(candidates, question_normalized)
    if candidate is None:
        raise QuestionError(
            f"question not found in library: {question_normalized!r}"
        )

    slug = _slug.slugify(candidate.question_display)
    rel_path = _slug.page_path(WikiKind.question.value, slug)
    full_path = Path(cfg.storage.root) / rel_path

    papers = papers_for_question(session, question_normalized)
    if not papers:
        raise QuestionError(
            f"no in-library papers for {question_normalized!r}"
        )

    def _emit(**progress):
        if on_progress is not None:
            on_progress({
                "question": question_normalized,
                "title": candidate.question_display,
                "stage": "question_compile",
                **progress,
            })

    evidence_count = len(papers)
    evidence_hash = _evidence_hash([p.id for p in papers])
    existing = _existing_page(session, slug)

    # Hash-skip: if the existing live page already has the same evidence
    # hash, the page is up to date — no LLM call, no file write.
    if (
        not force
        and existing is not None
        and existing.compiled_at is not None
        and not existing.stub
    ):
        prev_hash_from_file = None
        if full_path.exists():
            try:
                meta, _body = _frontmatter.parse(
                    full_path.read_text(encoding="utf-8")
                )
                prev_hash_from_file = meta.get("evidence_hash")
            except OSError:
                prev_hash_from_file = None
        if prev_hash_from_file == evidence_hash:
            _emit(detail="Up to date")
            return existing

    # Stub path — below the evidence threshold we never call the LLM.
    if evidence_count < EVIDENCE_THRESHOLD:
        _emit(detail=f"Below threshold ({evidence_count}); writing stub")
        return _write_stub(
            session, cfg, slug, candidate.question_display,
            evidence_count, evidence_hash, papers,
        )

    if not (
        llm.has_key_for(cfg.llm.summarize_model)
        or llm.has_key_for(cfg.llm.fallback_model)
    ):
        raise QuestionError(
            "no LLM API key configured (set DEEPSEEK_API_KEY or VOLCANO_API_KEY)"
        )

    # Previous compiled prose (without the user section) for revision context.
    old_body = None
    if existing and full_path.exists():
        try:
            _old_meta, old_full = _frontmatter.parse(full_path.read_text(encoding="utf-8"))
            old_body = _merge.extract_user_section(old_full) or old_full
        except OSError:
            old_body = None

    prompt = _build_user_prompt(
        question_display=candidate.question_display,
        papers=papers[:_MAX_PAPERS],
        old_body=old_body,
    )
    prompt = prompt[:_MAX_INPUT_CHARS + 2000]

    _emit(detail=f"Synthesizing {candidate.question_display}…")
    try:
        data = llm.chat_json(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            model=cfg.llm.summarize_model,
            fallback_model=cfg.llm.fallback_model,
            temperature=cfg.llm.temperature,
            timeout=cfg.llm.request_timeout_seconds,
        )
    except llm.LLMError as e:
        raise QuestionError(str(e)) from e
    if not isinstance(data, dict) or not data.get("summary"):
        raise QuestionError("LLM returned no usable question summary")

    body = _render_live_body(
        question_display=candidate.question_display,
        data=data,
        papers=papers,
    )

    # Preserve any prior user-authored section.
    old_text = full_path.read_text(encoding="utf-8") if full_path.exists() else None
    body = _merge.protect_user_section(old_text, body)

    confidence = _confidence(data.get("confidence"), evidence_count=evidence_count)

    now = datetime.now(UTC)
    meta = {
        "kind": WikiKind.question.value,
        "title": candidate.question_display,
        "slug": slug,
        "entity_key": f"question:{slug}",
        "compiled_at": now.isoformat(),
        "compiler_version": COMPILER_VERSION,
        "confidence": confidence,
        "evidence_count": evidence_count,
        "source_paper_ids": [p.id for p in papers],
        "question_status": DEFAULT_QUESTION_STATUS,
        "evidence_hash": evidence_hash,
    }

    text = _frontmatter.dump(meta, body)
    _atomic_write(full_path, text)

    # Upsert the index row from what we just wrote.
    page = _reindex.upsert_page_from_disk(
        session, cfg, WikiKind.question.value, slug
    )
    if page is None:
        raise QuestionError(f"failed to index written page: {rel_path}")
    if page.id is not None:
        for old in session.exec(
            select(WikiSource).where(WikiSource.wiki_page_id == page.id)
        ).all():
            session.delete(old)
        session.flush()
        for p in papers:
            session.add(
                WikiSource(
                    wiki_page_id=page.id,
                    paper_id=p.id,
                    chunk_id=None,
                    heading="abstract",
                    quote=(p.tldr_en or p.abstract or "")[:1000] or None,
                    role="context",
                )
            )
        vec = _embed_page(
            cfg,
            candidate.question_display,
            str(data.get("summary") or ""),
            str(data.get("why_it_matters") or ""),
        )
        if vec is not None:
            page.embedding = vec
        page.confidence = confidence
        page.evidence_count = evidence_count
        page.question_status = DEFAULT_QUESTION_STATUS
        page.stub = False
        session.add(page)
        session.commit()

    _reindex.recompute_backlinks(session)
    _emit(detail=f"Compiled {candidate.question_display}")
    logger.info("compiled question page %s (%s)", slug, candidate.question_display)
    return page


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _write_stub(
    session,
    cfg,
    slug,
    question_display,
    evidence_count,
    evidence_hash,
    papers,
) -> WikiPage:
    rel_path = _slug.page_path(WikiKind.question.value, slug)
    full_path = Path(cfg.storage.root) / rel_path
    body = _render_stub_body(question_display=question_display, paper_count=evidence_count)
    body = _merge.protect_user_section(
        full_path.read_text(encoding="utf-8") if full_path.exists() else None,
        body,
    )
    now = datetime.now(UTC)
    meta = {
        "kind": WikiKind.question.value,
        "title": question_display,
        "slug": slug,
        "entity_key": f"question:{slug}",
        "compiled_at": now.isoformat(),
        "compiler_version": COMPILER_VERSION,
        "confidence": 0.0,
        "evidence_count": evidence_count,
        "source_paper_ids": [p.id for p in papers],
        "question_status": DEFAULT_QUESTION_STATUS,
        "evidence_hash": evidence_hash,
        "stub": True,
    }
    text = _frontmatter.dump(meta, body)
    _atomic_write(full_path, text)
    page = _reindex.upsert_page_from_disk(
        session, cfg, WikiKind.question.value, slug
    )
    if page is not None:
        page.stub = True
        page.evidence_count = evidence_count
        page.question_status = DEFAULT_QUESTION_STATUS
        session.add(page)
        session.commit()
    if page is None:
        raise QuestionError(f"failed to index written stub: {rel_path}")
    return page


# ---------------------------------------------------------------------------
# Staleness + batch
# ---------------------------------------------------------------------------


def select_stale_questions(session, *, limit: int = 20) -> list[str]:
    """Return question normalized strings that need (re)compilation.

    A question is stale when:
      * no WikiPage row exists, OR
      * the page is a stub AND the paper set is now at/above the
        threshold (promote to live), OR
      * ``latest_paper_update`` exceeds ``page.compiled_at``.

    Sorted by paper-count desc so the most-evidenced questions compile
    first.
    """
    candidates = aggregate(session)
    page_by_slug = {}
    for page in session.exec(
        select(WikiPage).where(
            WikiPage.kind == WikiKind.question.value,
            WikiPage.redirects_to.is_(None),
        )
    ).all():
        if page.slug:
            page_by_slug[page.slug] = page

    stale = []
    for c in candidates:
        slug = _slug.slugify(c.question_display)
        page = page_by_slug.get(slug)
        if page is None:
            stale.append((c.paper_count, c.question_normalized))
            continue
        if page.stub and c.paper_count >= EVIDENCE_THRESHOLD:
            # Stub promotion — threshold now met, recompile.
            stale.append((c.paper_count, c.question_normalized))
            continue
        if page.compiled_at is not None and c.latest_paper_update is not None:
            if c.latest_paper_update > page.compiled_at:
                stale.append((c.paper_count, c.question_normalized))
    stale.sort(key=lambda t: (-t[0], t[1]))
    return [qn for _count, qn in stale[:limit]]


def compile_questions_pending(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 20,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Compile stale question pages; returns counts."""
    # Reconcile the question catalog against the live aggregation.  Today
    # questions have no alias layer, so this only catches "page exists at
    # (kind, slug) but the aggregation no longer surfaces the question" —
    # we leave those alone for ops.  Cheap to run; failure is non-fatal.
    try:
        from carrel.pipeline.wiki._entities import reconcile_kind
        from carrel.pipeline.wiki._questions_agg import enumerate_entities
        reconcile_kind(
            session, kind="question", enumerate_fn=enumerate_entities
        )
    except Exception:
        logger.exception("question compile: reconcile failed (continuing)")

    questions = select_stale_questions(session, limit=limit)
    counts = {
        "candidates": len(questions),
        "compiled": 0,
        "stubbed": 0,
        "failed": 0,
    }
    total = len(questions)

    def _wrap(i, qn, title):
        def _cb(progress):
            if on_progress is not None:
                on_progress({**progress, "index": i, "total": total, "name": title})
        return _cb

    candidates = aggregate(session)
    for i, qn in enumerate(questions, start=1):
        cand = _candidate_for_question(candidates, qn)
        label = cand.question_display if cand else qn
        try:
            page = compile_question(
                session, cfg, qn, force=force, on_progress=_wrap(i, qn, label)
            )
            if page.stub:
                counts["stubbed"] += 1
            else:
                counts["compiled"] += 1
        except QuestionError as e:
            logger.info("question %s failed: %s", qn, e)
            counts["failed"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("question %s crashed: %s", qn, e)
            counts["failed"] += 1

    logger.info(
        "question wiki batch done: candidates=%d compiled=%d stubbed=%d failed=%d",
        counts["candidates"], counts["compiled"], counts["stubbed"], counts["failed"],
    )
    return counts
