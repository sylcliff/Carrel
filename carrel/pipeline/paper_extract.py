"""Per-paper LLM extraction of concepts + open questions (M8b foundation).

Drives a parsed, summarized paper through an LLM that pulls out technical
concepts and open questions grounded in the paper's body.  The output is
two flat tables — :class:`PaperConcept` and :class:`PaperQuestion` — that
the concept/question wiki compilers aggregate and synthesize into pages.

The extraction is intentionally narrow: 2+2 sections by default, 5+5 when
``deep=True``.  The full body is not sent — the LLM call is the bottleneck
and the gain from extra context is small once the abstract, introduction,
and conclusion are included.

Design mirrors :mod:`carrel.pipeline.summarize` and :mod:`carrel.pipeline.topics`:
  * **Reuses** :func:`carrel.llm.chat_json` / same model config.
  * **Idempotent** — a paper with existing extractions is skipped unless
    ``force=True``.  Staleness is ``paper.updated_at > max(paper_concepts
    ∪ paper_questions).created_at``; no per-paper queue column.
  * **Quote verification** — every concept/question must carry a verbatim
    span from the supplied body.  Hallucinated mentions (quotes that
    don't appear in the text) are dropped silently, leaving a partial
    result rather than blocking the rest of the paper.
  * **Non-fatal** — failures raise :class:`PaperExtractError` and are
    caught per paper by the batch driver; the paper's ``status`` is never
    touched and the existing extraction rows (if any) are preserved.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, func, select

from carrel import chunking, llm, usage
from carrel.config import CarrelYAML
from carrel.models import Paper, PaperConcept, PaperQuestion, PaperStatus

logger = logging.getLogger(__name__)


class PaperExtractError(Exception):
    """Per-paper extraction failed (no key, bad LLM output, IO error, etc.)."""


ProgressCallback = Callable[[dict], None]

# Default section pick: first 2 + last 2.  MinerU preserves ATX headings
# (see :func:`carrel.chunking.split_by_heading`); the abstract+intro+method
#+conclusion sweep we want.
_DEFAULT_HEAD = 2
_DEFAULT_TAIL = 2
_DEEP_HEAD = 5
_DEEP_TAIL = 5
# Fallback when the body has no headings: first/last chars windows.
_FALLBACK_HEAD_CHARS = 1500
_FALLBACK_TAIL_CHARS = 1500
# Cap on LLM input (rough chars; not a token counter).
_MAX_INPUT_CHARS = 8_000
_MIN_BODY_CHARS = 200


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are an expert research librarian. You read an academic paper's body "
    "and identify the technical concepts that a domain researcher in this "
    "field would want to look up later. Use the five categories below to "
    "decide what counts.\n"
    "\n"
    "Categories — pick one per concept:\n"
    '  METHOD     a concrete technique, algorithm, or model '
    '(e.g. "graph neural network", "variational quantum eigensolver", '
    '"convolutional neural network")\n'
    '  THEORY     a theoretical framework, equation, or formal model '
    '(e.g. "Kohn-Sham DFT", "Schrödinger equation", "Bayesian inference")\n'
    '  DATASET    a named corpus, benchmark, or database '
    '(e.g. "Materials Project", "QM9", "ImageNet")\n'
    '  DOMAIN     a research area or subfield (more specific than the '
    "paper's own field) "
    '(e.g. "first-principles phonon calculations", "equivariant machine '
    'learning for molecules" — NOT "materials science" or "machine learning" '
    "which are too broad)\n"
    '  PHENOMENON a specific physical effect or observed result '
    '(e.g. "twist-tunable flat bands", "Anderson localization", '
    '"magic-angle superconductivity" — NOT "superconductivity" which is '
    "too broad)\n"
    "\n"
    "Rules:\n"
    "- Extract 3-12 concepts per paper; 0-5 open questions.\n"
    '- Use the full written form as `term`. If the paper uses an '
    'abbreviation (e.g. "DFT"), put the full form in `term` and any '
    'abbreviation/synonym in `aliases` (e.g. {"term": "density functional '
    'theory", "aliases": ["DFT"]}). `aliases` may be empty or omitted.\n'
    "- DOMAIN concepts should be more specific than the paper's own field "
    '— pick a sub-area, not the umbrella discipline.\n'
    "- PHENOMENON concepts name a specific effect/result, not a general "
    "class.\n"
    '- "machine learning" is too broad to extract on its own; name the '
    "specific method (e.g. \"graph neural network\").\n"
    "- For EVERY concept AND question you include, supply a verbatim "
    '"quote" of 20-200 characters copied exactly from the supplied body. '
    "Quotes must be a contiguous substring of the body (case + whitespace "
    "+ punctuation preserved). If you cannot ground an item in the body, "
    "drop it.\n"
    "- Questions are short statements of unresolved problems the paper "
    'itself raises (e.g. "How can retrieval stay current with fast-moving '
    "knowledge bases?\"). Do NOT propose new research ideas; quote what "
    "the paper says.\n"
    "- If the body is too short, corrupted, or not in English, still do "
    "your best from what is there.\n"
    "- Respond with ONLY a JSON object, no prose or markdown fences, of "
    'the form: {"concepts": [{"term": "...", "category": "METHOD|THEORY|'
    'DATASET|DOMAIN|PHENOMENON", "aliases": ["..."], "quote": "..."}], '
    '"questions": [{"question": "...", "quote": "..."}]}'
)


# Valid concept categories (used to validate LLM output).
CONCEPT_CATEGORIES = frozenset({"METHOD", "THEORY", "DATASET", "DOMAIN", "PHENOMENON"})


# ---------------------------------------------------------------------------
# Body preparation
# ---------------------------------------------------------------------------


def _strip_image_lines(md: str) -> str:
    """Drop lines that start with ``!`` (MinerU image markup)."""
    kept: list[str] = []
    for line in md.splitlines():
        if line.lstrip().startswith("!"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _pick_sections(md: str, *, head: int, tail: int) -> str:
    """Pick ``head`` leading + ``tail`` trailing sections from a parsed paper.

    Uses :func:`carrel.chunking.split_by_heading`; falls back to first/last
    char windows when the body has no ATX headings. Each picked section
    is rendered with its ``## heading`` prefix so the LLM can see the
    structure (e.g. ``## Introduction`` before the introduction body).
    Headings are always preserved — even when the body is short enough to
    be returned whole — so the LLM knows which section each block came
    from.
    """
    sections = chunking.split_by_heading(md)
    if not sections:
        return ""
    if len(sections) <= head + tail:
        picked = sections
    else:
        picked = sections[:head] + sections[-tail:]
    parts: list[str] = []
    for heading, body in picked:
        if heading:
            parts.append(f"## {heading}")
        parts.append(body)
    return "\n\n".join(parts).strip()


def _pick_fallback(md: str) -> str:
    """No headings at all: take first + last char windows of the body."""
    cleaned = md.strip()
    if len(cleaned) <= _FALLBACK_HEAD_CHARS + _FALLBACK_TAIL_CHARS:
        return cleaned
    head = cleaned[:_FALLBACK_HEAD_CHARS].rstrip()
    tail = cleaned[-_FALLBACK_TAIL_CHARS:].lstrip()
    return f"{head}\n\n…\n\n{tail}"


def _prepare_body(md: str, *, head: int, tail: int, max_chars: int) -> str:
    """Assemble the LLM input body.  Truncates with a marker."""
    if not md or not md.strip():
        return ""
    md = _strip_image_lines(md)
    has_headings = bool(chunking.split_by_heading(md)) and bool(
        chunking.split_by_heading(md)[0][0]
    )
    if has_headings:
        body = _pick_sections(md, head=head, tail=tail)
    else:
        body = _pick_fallback(md)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n…[truncated]"
    return body


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


_TRAILING_PUNCT = re.compile(r"[\s\.,;:!\?\-—]+$")
_INNER_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip trailing punctuation.

    Used as the compound primary key for PaperConcept / PaperQuestion so
    "RAG", "rag", and "RAG." map to the same row.  Trimming trailing
    punctuation is safe here because the display form is preserved.
    """
    s = (text or "").strip()
    s = _INNER_WS.sub(" ", s)
    s = _TRAILING_PUNCT.sub("", s)
    return s.lower()


def _display(text: str) -> str:
    """Surface form: collapse whitespace, keep punctuation, trim."""
    s = _INNER_WS.sub(" ", (text or "").strip())
    return s


# ---------------------------------------------------------------------------
# LLM response coercion + verification
# ---------------------------------------------------------------------------


def _coerce_items(raw: Any, kind: str) -> list[dict[str, str]]:
    """Validate the LLM payload into ``[{key, quote, category?}]``.

    ``kind`` is "concept" or "question"; the key is ``term`` or ``question``.
    Items missing a key or a quote (or whose quote is empty) are dropped.
    For concepts, a ``category`` field is captured when it's one of the five
    known categories (METHOD/THEORY/DATASET/DOMAIN/PHENOMENON); anything else
    is dropped so the DB never sees a junk enum.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        if kind == "concept":
            key = str(item.get("term", "")).strip()
        else:
            key = str(item.get("question", "")).strip()
        quote = str(item.get("quote", "")).strip()
        if not key or not quote:
            continue
        norm = _normalize(key)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        row: dict[str, str] = {"key": key, "quote": quote}
        if kind == "concept":
            cat = str(item.get("category", "")).strip().upper()
            if cat in CONCEPT_CATEGORIES:
                row["category"] = cat
        out.append(row)
    return out


def _summarize_extract(
    concepts: list[dict[str, str]], questions: list[dict[str, str]]
) -> str:
    """Compact, human-readable digest of an extraction result for the stepper."""
    lines: list[str] = []
    for c in concepts[:8]:
        term = (c.get("term") or c.get("display") or "").strip()
        if term:
            lines.append(f"concept: {term}")
    for q in questions[:8]:
        text = (q.get("question") or q.get("display") or "").strip()
        if text:
            lines.append(f"question: {text}")
    if not lines:
        return "(no items extracted)"
    return "\n".join(lines)


def _verify_quotes(items: list[dict[str, str]], body: str) -> list[dict[str, str]]:
    """Keep only items whose ``quote`` is a substring of ``body``.

    This is the "no hallucinated concept" guard.  We do a literal substring
    check; if the LLM paraphrases even slightly, the quote won't match and
    we drop the item.  Empty body ⇒ no items.
    """
    if not body:
        return []
    out: list[dict[str, str]] = []
    for it in items:
        q = it["quote"]
        if q in body:
            out.append(it)
        else:
            logger.info("paper extract: dropping unverified %r (quote not in body)", it["key"][:60])
    return out


# ---------------------------------------------------------------------------
# Per-paper extraction
# ---------------------------------------------------------------------------


def _has_extraction(session: Session, paper_id: str) -> bool:
    """True if the paper already has any PaperConcept or PaperQuestion rows."""
    has_c = session.exec(
        select(func.count(PaperConcept.paper_id)).where(PaperConcept.paper_id == paper_id)
    ).one()
    if has_c:
        return True
    has_q = session.exec(
        select(func.count(PaperQuestion.paper_id)).where(PaperQuestion.paper_id == paper_id)
    ).one()
    return bool(has_q)


def _is_stale(session: Session, paper: Paper) -> bool:
    """A paper is stale if it has no extraction, or its updated_at is newer."""
    if not _has_extraction(session, paper.id):
        return True
    max_c = session.exec(
        select(func.max(PaperConcept.created_at)).where(PaperConcept.paper_id == paper.id)
    ).one()
    max_q = session.exec(
        select(func.max(PaperQuestion.created_at)).where(PaperQuestion.paper_id == paper.id)
    ).one()
    candidates = [t for t in (max_c, max_q) if t is not None]
    latest = max(candidates) if candidates else None
    if latest is None:
        return True
    if paper.updated_at is None:
        return False
    return paper.updated_at > latest


def _write_rows(
    session: Session,
    *,
    paper_id: str,
    concepts: list[dict[str, str]],
    questions: list[dict[str, str]],
) -> tuple[int, int]:
    """Replace the paper's PaperConcept / PaperQuestion rows.  Returns counts written."""
    # Wipe-and-replace.  Idempotency for ``force=False`` is handled above
    # (the caller skips before reaching here).
    for old in session.exec(
        select(PaperConcept).where(PaperConcept.paper_id == paper_id)
    ).all():
        session.delete(old)
    for old in session.exec(
        select(PaperQuestion).where(PaperQuestion.paper_id == paper_id)
    ).all():
        session.delete(old)
    session.flush()

    now = datetime.now(UTC)
    c_written = 0
    for it in concepts:
        norm = _normalize(it["key"])
        if not norm:
            continue
        session.add(PaperConcept(
            paper_id=paper_id,
            term_normalized=norm[:200],
            term_display=_display(it["key"])[:300],
            evidence_quote=it["quote"][:2000] or None,
            category=(it.get("category") or None) or None,
            created_at=now,
        ))
        c_written += 1
    q_written = 0
    for it in questions:
        norm = _normalize(it["key"])
        if not norm:
            continue
        session.add(PaperQuestion(
            paper_id=paper_id,
            question_normalized=norm[:400],
            question_display=_display(it["key"])[:600],
            evidence_quote=it["quote"][:2000] or None,
            created_at=now,
        ))
        q_written += 1
    session.commit()
    return c_written, q_written


def extract_paper(
    session: Session,
    cfg: CarrelYAML,
    paper_id: str,
    *,
    deep: bool = False,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> Paper:
    """Extract concepts + questions from one paper.  Idempotent, non-fatal.

    Raises :class:`PaperExtractError` on missing markdown, no LLM key, or
    a malformed response.  Existing rows are preserved on failure.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise PaperExtractError(f"paper not found: {paper_id}")

    def _emit(**progress: Any) -> None:
        if on_progress is not None:
            on_progress({
                "paper_id": paper.id,
                "paper_title": paper.title,
                "stage": "paper_extract",
                **progress,
            })

    if not paper.md_path:
        raise PaperExtractError("paper has no md_path; parse it first")
    md_path = Path(cfg.storage.root) / paper.md_path
    if not md_path.exists():
        raise PaperExtractError(f"parsed markdown missing on disk: {md_path}")

    if not force and not _is_stale(session, paper):
        _emit(detail="Already extracted")
        return paper

    if not (
        llm.has_key_for(cfg.llm.summarize_model)
        or llm.has_key_for(cfg.llm.fallback_model)
    ):
        raise PaperExtractError(
            "no LLM API key configured (set DEEPSEEK_API_KEY or VOLCANO_API_KEY)"
        )

    md = md_path.read_text(encoding="utf-8", errors="replace")
    if len(md.strip()) < _MIN_BODY_CHARS:
        # Don't waste an LLM call on near-empty bodies.  Treat as a no-op
        # so the batch driver can move on.  The paper is still marked
        # "up to date" because the next extract would produce the same
        # emptiness, and a future re-parse will flip it stale again.
        _emit(detail="Body too short; skipping")
        return paper

    head = _DEEP_HEAD if deep else _DEFAULT_HEAD
    tail = _DEEP_TAIL if deep else _DEFAULT_TAIL
    body = _prepare_body(md, head=head, tail=tail, max_chars=_MAX_INPUT_CHARS)

    _emit(detail=f"Extracting concepts + questions (head={head}, tail={tail})…")
    try:
        data = llm.chat_json(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": body},
            ],
            model=cfg.llm.summarize_model,
            fallback_model=cfg.llm.fallback_model,
            temperature=cfg.llm.temperature,
            timeout=cfg.llm.request_timeout_seconds,
            feature="extract",
            on_usage=usage.make_usage_callback(
                session, feature="extract", paper_id=paper.id,
            ),
        )
    except llm.LLMError as e:
        raise PaperExtractError(str(e)) from e

    concepts = _verify_quotes(_coerce_items(data.get("concepts"), "concept"), body)
    questions = _verify_quotes(_coerce_items(data.get("questions"), "question"), body)

    if not concepts and not questions:
        # We still want to record the attempt so we don't re-burn tokens on
        # the same useless input.  Wipe the rows to record "we tried".
        c_written, q_written = _write_rows(
            session, paper_id=paper.id, concepts=[], questions=[]
        )
        _emit(detail="No grounded items found")
        return paper

    c_written, q_written = _write_rows(
        session, paper_id=paper.id, concepts=concepts, questions=questions
    )
    # Surface a short input/output snippet pair so the wiki stepper can show
    # *what* the model saw and produced for this paper — not just counts.
    _emit(
        detail=f"Extracted {c_written} concepts, {q_written} questions",
        io={
            "input": body,
            "output": _summarize_extract(concepts, questions),
        },
    )
    logger.info("paper extract %s: %d concepts, %d questions", paper.id, c_written, q_written)
    return paper


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def select_stale_extract(session: Session, *, limit: int = 20) -> list[Paper]:
    """In-library, parsed/summarized/ready papers needing extraction.

    A paper is eligible when ``md_path`` is set AND
    ``status in {parsed, summarized, ready}`` AND the paper is stale
    (no extraction, or ``updated_at`` newer than the max created_at of
    its PaperConcept/PaperQuestion rows).  Ordering is newest-first by
    ``created_at`` so the most-recently-added papers are processed first.
    """
    stmt = (
        select(Paper)
        .where(
            Paper.in_library.is_(True),
            Paper.md_path.is_not(None),
            Paper.status.in_([
                PaperStatus.parsed.value,
                PaperStatus.summarized.value,
                PaperStatus.ready.value,
            ]),
        )
        .order_by(Paper.created_at.desc())
        .limit(limit * 4)  # over-fetch then filter; cheap because the OR is sub-linear
    )
    candidates = list(session.exec(stmt).all())
    stale = [p for p in candidates if _is_stale(session, p)]
    return stale[:limit]


def extract_papers_pending(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 20,
    deep: bool = False,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Extract concepts + questions for a batch of eligible papers.  Returns counts."""
    papers = select_stale_extract(session, limit=limit)
    if force and not papers:
        # Force-mode without any obviously-stale papers: walk the in-library
        # set directly so the caller can wipe and re-extract.
        stmt = (
            select(Paper)
            .where(
                Paper.in_library.is_(True),
                Paper.md_path.is_not(None),
                Paper.status.in_([
                    PaperStatus.parsed.value,
                    PaperStatus.summarized.value,
                    PaperStatus.ready.value,
                ]),
            )
            .order_by(Paper.created_at.desc())
            .limit(limit)
        )
        papers = list(session.exec(stmt).all())
    counts = {
        "candidates": len(papers),
        "extracted": 0,
        "skipped": 0,
        "failed": 0,
    }
    total = len(papers)

    def _wrap(i: int, title: str):
        def _cb(progress: dict) -> None:
            if on_progress is not None:
                on_progress({**progress, "index": i, "total": total, "name": title})
        return _cb

    for i, paper in enumerate(papers, start=1):
        # Capture "was stale" before the extract so we can correctly
        # attribute a skip (already up-to-date) vs. an extract (we just
        # did work).  We mirror the gate inside extract_paper, but
        # measure it here so the batch counter reflects what *we* did.
        was_stale = force or _is_stale(session, paper)
        try:
            extract_paper(
                session, cfg, paper.id, deep=deep, force=force,
                on_progress=_wrap(i, paper.title),
            )
            if was_stale:
                counts["extracted"] += 1
            else:
                counts["skipped"] += 1
        except PaperExtractError as e:
            logger.info("paper extract %s failed: %s", paper.id, e)
            counts["failed"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("paper extract %s crashed: %s", paper.id, e)
            counts["failed"] += 1

    logger.info(
        "paper extract batch done: candidates=%d extracted=%d skipped=%d failed=%d",
        counts["candidates"], counts["extracted"], counts["skipped"], counts["failed"],
    )
    return counts
