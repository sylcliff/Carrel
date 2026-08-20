"""LLM summarization pipeline (M4).

Drives a parsed paper through the optional summarization step::

    parsed --(LLM TL;DR + abstract + keywords)--> summarized

Reads the parsed Markdown at ``Paper.md_path``, asks an LLM for a bilingual
one-line TL;DR, a 3-5 sentence Chinese abstract, and 5-8 keywords, then writes
them to the ``tldr_en``/``tldr_zh``/``summary_zh``/``keywords`` columns.

Design choices (see plan):
  * **Chained after parse** — :func:`carrel.pipeline.process.process_paper`
    calls :func:`summarize_paper` best-effort once MinerU succeeds.
  * **Fill-missing** — a value already present (e.g. ``tldr_en`` sourced from
    Semantic Scholar) is preserved unless ``force=True``; we only fill the
    fields the model returns that are currently unset.
  * **Non-fatal** — a summarization failure does NOT flip the paper to
    ``failed``; the paper stays ``parsed`` and embedding can still proceed
    (embed accepts both ``parsed`` and ``summarized``). Failures surface via
    the wrapping Job and logs, leaving ``paper.error`` untouched so a
    successful parse is not obscured.

Like :mod:`carrel.pipeline.embed`, this module is synchronous (single-user box;
the LLM call is the bottleneck and runs one paper at a time).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from carrel import llm
from carrel.config import CarrelYAML
from carrel.models import Paper, PaperStatus

logger = logging.getLogger(__name__)


class SummarizeError(Exception):
    """Summarization failed for a paper (missing markdown, no key, bad LLM output)."""


# Mirrors process/embed ProgressCallback shape.
ProgressCallback = Callable[[dict], None]

# Fields this step is responsible for, in the order we apply them.
_OUTPUT_FIELDS = ("tldr_en", "tldr_zh", "summary_zh", "keywords")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are an expert research assistant. You read an academic paper and "
    "produce a concise, accurate summary. Rules:\n"
    "- Base every claim ONLY on the paper text provided; never invent results, "
    "numbers, or conclusions.\n"
    "- If the text is too short, corrupted, or not in English, still do your "
    "best from what is there; do not refuse.\n"
    "- tldr_en: ONE sentence in English, <= 40 words, stating the core "
    "contribution/finding.\n"
    "- tldr_zh: ONE sentence in Simplified Chinese, <= 40 Chinese characters, "
    "same meaning as tldr_en.\n"
    "- summary_zh: 3-5 sentences in Simplified Chinese covering method, main "
    "contribution, and conclusion.\n"
    "- keywords: 5-8 English technical keywords/phrases as a JSON array of "
    "strings, lowercase unless a proper noun.\n"
    "- Respond with ONLY a JSON object, no prose or markdown fences, of the "
    "form: "
    '{"tldr_en": "...", "tldr_zh": "...", "summary_zh": "...", '
    '"keywords": ["..."]}'
)


def _build_user_prompt(
    *,
    title: str,
    authors: str,
    venue_date: str,
    abstract: str,
    body: str,
) -> str:
    parts = [
        f"Title: {title}",
        f"Authors: {authors or 'unknown'}",
        f"Venue/date: {venue_date or 'unknown'}",
    ]
    if abstract:
        parts.append(f"Abstract:\n{abstract}")
    parts.append(
        "Paper text (parsed from PDF; may contain OCR noise; "
        f"truncated):\n\n{body}"
    )
    parts.append(
        "\nReturn the JSON object now, with no commentary."
    )
    return "\n\n".join(parts)


def _authors_string(paper: Paper) -> str:
    if not paper.authors:
        return ""
    names = [a.get("name", "") for a in paper.authors if isinstance(a, dict) and a.get("name")]
    return ", ".join(names)


def _venue_date_string(paper: Paper) -> str:
    bits = [paper.venue or ""]
    if paper.publication_date:
        bits.append(str(paper.publication_date))
    return " · ".join(b for b in bits if b)


def _prepare_body(md: str, max_chars: int) -> str:
    """Collapse image tags and trim the parsed Markdown before sending it.

    MinerU output can contain many ``![]()`` image lines that waste tokens and
    add no signal. We drop them, then take the first ``max_chars`` characters.
    """
    kept: list[str] = []
    for line in md.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("!"):
            continue  # image markup
        kept.append(line)
    text = "\n".join(kept).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…[truncated]"
    return text


# ---------------------------------------------------------------------------
# Per-paper summarization
# ---------------------------------------------------------------------------


def _all_fields_present(paper: Paper) -> bool:
    return all(getattr(paper, f) for f in _OUTPUT_FIELDS)


def summarize_paper(
    session: Session,
    cfg: CarrelYAML,
    paper_id: str,
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> Paper:
    """Generate LLM summary fields for one paper; advance ``parsed`` -> ``summarized``.

    Idempotent: when all four fields exist and ``force`` is False, the LLM is
    not called. Existing values are preserved (fill-missing); ``force=True``
    overwrites all of them.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise SummarizeError(f"paper not found: {paper_id}")

    def _emit(**progress: Any) -> None:
        if on_progress is not None:
            on_progress({
                "paper_id": paper.id,
                "paper_title": paper.title,
                "stage": "summarize",
                **progress,
            })

    if not paper.md_path:
        raise SummarizeError("paper has no md_path; parse it first")

    md_path = Path(cfg.storage.root) / paper.md_path
    if not md_path.exists():
        raise SummarizeError(f"parsed markdown missing on disk: {md_path}")

    if _all_fields_present(paper) and not force:
        _emit(detail="Already summarized")
        return paper

    # Fast no-key check: avoid a noisy stack trace when chaining after parse.
    if not (
        llm.has_key_for(cfg.llm.summarize_model)
        or llm.has_key_for(cfg.llm.fallback_model)
    ):
        raise SummarizeError(
            "no LLM API key configured (set DEEPSEEK_API_KEY or VOLCANO_API_KEY)"
        )

    md = md_path.read_text(encoding="utf-8", errors="replace")
    body = _prepare_body(md, cfg.llm.max_input_chars)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_user_prompt(
                title=paper.title,
                authors=_authors_string(paper),
                venue_date=_venue_date_string(paper),
                abstract=paper.abstract or "",
                body=body,
            ),
        },
    ]

    _emit(detail="Generating summary…")
    try:
        data = llm.chat_json(
            messages,
            model=cfg.llm.summarize_model,
            fallback_model=cfg.llm.fallback_model,
            temperature=cfg.llm.temperature,
            timeout=cfg.llm.request_timeout_seconds,
        )
    except llm.LLMError as e:
        raise SummarizeError(str(e)) from e

    _apply_fields(paper, data, force=force)

    # Advance parsed -> summarized, but never regress an already-ready paper.
    if paper.status == PaperStatus.parsed.value:
        paper.status = PaperStatus.summarized.value
    paper.updated_at = datetime.now(UTC)
    session.add(paper)
    session.commit()
    session.refresh(paper)
    _emit(detail="Summary generated")
    logger.info("summarized %s", paper.id)
    return paper


def _apply_fields(paper: Paper, data: dict[str, Any], *, force: bool) -> int:
    """Write model output onto the paper (fill-missing unless force).

    Returns the number of fields actually changed.
    """
    changed = 0
    for field in _OUTPUT_FIELDS:
        value = data.get(field)
        if value is None:
            continue
        if field == "keywords":
            value = _clean_keywords(value)
            if not value:
                continue
        elif isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        else:
            continue

        if force or not getattr(paper, field):
            setattr(paper, field, value)
            changed += 1
    return changed


def _clean_keywords(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        kw = item.strip().strip(".,;")
        if not kw:
            continue
        key = kw.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(kw)
    return out


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def select_pending_summarize(session: Session, limit: int = 20) -> list[Paper]:
    """Papers that are parsed (or failed-with-markdown) and missing a summary.

    A paper is eligible when it has markdown AND at least one of the four
    summary fields is NULL. ``ready`` papers are also eligible if they were
    embedded before summaries existed — we want the backfill to reach them too
    without affecting search.
    """
    missing = (
        (Paper.tldr_en.is_(None))
        | (Paper.tldr_zh.is_(None))
        | (Paper.summary_zh.is_(None))
        # JSON NULL on SQLite can be either SQL NULL or the text 'null';
        # keywords comes back from MinerU-less papers as NULL in both engines.
        | (Paper.keywords.is_(None))
    )
    stmt = (
        select(Paper)
        .where(
            Paper.in_library.is_(True),
            Paper.md_path.is_not(None),
            Paper.status.in_([
                PaperStatus.parsed.value,
                PaperStatus.summarized.value,  # partially filled (e.g. S2 tldr only)
                PaperStatus.ready.value,
                PaperStatus.failed.value,
            ]),
            missing,
        )
        .order_by(Paper.created_at.desc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def summarize_pending(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 20,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Summarize a batch of eligible papers; returns counts."""
    papers = select_pending_summarize(session, limit=limit)
    counts = {"candidates": len(papers), "summarized": 0, "failed": 0, "skipped": 0}
    total = len(papers)

    def _wrap(i: int, title: str):
        def _cb(progress: dict) -> None:
            if on_progress is not None:
                on_progress({**progress, "index": i, "total": total, "title": title})
        return _cb

    for i, paper in enumerate(papers, start=1):
        try:
            summarize_paper(
                session, cfg, paper.id, force=force, on_progress=_wrap(i, paper.title)
            )
            counts["summarized"] += 1
        except SummarizeError as e:
            logger.info("summarize %s failed: %s", paper.id, e)
            counts["failed"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("summarize %s crashed: %s", paper.id, e)
            counts["failed"] += 1

    logger.info(
        "summarize batch done: candidates=%d summarized=%d failed=%d",
        counts["candidates"], counts["summarized"], counts["failed"],
    )
    return counts
