"""LLM summarization pipeline (M4).

Drives a parsed paper through the optional summarization step::

    parsed --(LLM TL;DR + abstract + keywords)--> summarized

Reads the parsed Markdown at ``Paper.md_path``, asks an LLM for a bilingual
one-line TL;DR, a 3-5 sentence Chinese abstract, and 5-8 keywords, then writes
them to the ``tldr_en``/``tldr_zh``/``summary_zh``/``keywords`` columns.

The body sent to the LLM is sliced by the section picker (see
:mod:`carrel.pipeline._section_picker`): noise sections like references,
acknowledgments, supplementary material, and appendices are dropped, and
the budget is filled in priority order (Method → Results → Conclusion →
Intro) so the summarizer has the actual method/result text in front of it,
not the front matter.  The slice is rendered as numbered blocks
(``## [1] Method``, ``## [2] Results``, …) so the model can attribute
each fact to the right section.

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

The shared LLM-call orchestration (paper lookup, md_path check, body
prep, LLM call, LLMError catch, progress + usage recording) lives in
:mod:`carrel.pipeline._llm_extract` so a bug fix or budget change only
lands in one place.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from carrel import llm  # re-exported for tests that monkey-patch pipe.llm
from carrel import prompts_runtime
from carrel.config import CarrelYAML
from carrel.models import Paper, PaperStatus
from carrel.pipeline._llm_extract import (
    drive_paper_llm_extraction,
    touch_paper_after_llm,
)
from carrel.pipeline._paper_meta import USER_PROMPT_TEMPLATE, authors_string, venue_date_string

# Summarize's output schema is bilingual (tldr_en / tldr_zh / summary_zh),
# so a single "respond in X" directive would leave the wrong field
# feeling primary. Build a per-language suffix that names which field is
# the primary one and which is the gloss. Keeping the schema stable
# (no migration) — see plan "summarize schema 怎么处理".
_SUMMARIZE_LANGUAGE_SUFFIX = {
    "zh": (
        "Output language: Simplified Chinese (简体中文). The 'tldr_zh' and "
        "'summary_zh' fields are the primary output; populate them with "
        "high-quality Simplified Chinese. If you also fill 'tldr_en', it "
        "is a brief English gloss, not the primary summary."
    ),
    "en": (
        "Output language: English. The 'tldr_en' field is the primary "
        "output; populate it with a high-quality English sentence. "
        "'tldr_zh' and 'summary_zh' (if filled) are translations / glosses, "
        "not the primary output."
    ),
}

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
    "- The paper text below is sliced by section type and numbered "
    "(## [1] Method, ## [2] Results, …).  Prioritize the Method and "
    "Results blocks when writing the summary; use the Conclusion block "
    "to confirm the main claim.\n"
    "- Respond with ONLY a JSON object, no prose or markdown fences, of the "
    "form: "
    '{"tldr_en": "...", "tldr_zh": "...", "summary_zh": "...", '
    '"keywords": ["..."]}'
)


# ---------------------------------------------------------------------------
# Per-paper summarization
# ---------------------------------------------------------------------------


def _all_fields_present(_session: Session, paper: Paper) -> bool:
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
    def _build_messages(paper: Paper, body: str) -> list[dict]:
        # Read cfg.llm.output_language fresh each call so a settings
        # PATCH is live on the next summarise (the 60s prompts_runtime
        # TTL caches override rows only, not cfg-derived directives).
        # Unknown values fall back to the English suffix to mirror
        # ``carrel.prompts_language.language_directive``'s safety net.
        suffix = _SUMMARIZE_LANGUAGE_SUFFIX.get(
            cfg.llm.output_language, _SUMMARIZE_LANGUAGE_SUFFIX["en"]
        )
        system = prompts_runtime.get_system("summarize", _SYSTEM_PROMPT)
        system = f"{system}\n\n{suffix}"
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": prompts_runtime.get_user_template("summarize", USER_PROMPT_TEMPLATE).format(
                    title=paper.title,
                    authors=authors_string(paper) or "unknown",
                    venue_date=venue_date_string(paper) or "unknown",
                    abstract=paper.abstract or "",
                    numbered_sections=body,
                ),
            },
        ]

    paper, data, _body = drive_paper_llm_extraction(
        session, cfg, paper_id,
        feature="summarize",
        progress_stage="summarize",
        error_class=SummarizeError,
        is_stale=lambda s, p: not _all_fields_present(s, p),
        build_messages=_build_messages,
        budget_chars=cfg.llm.max_input_chars,
        force=force,
        on_progress=on_progress,
    )
    if data is None:
        return paper  # not stale — driver already emitted the "up to date" signal

    _apply_fields(paper, data, force=force)

    # Advance parsed -> summarized, but never regress an already-ready paper.
    if paper.status == PaperStatus.parsed.value:
        paper.status = PaperStatus.summarized.value
    touch_paper_after_llm(paper, session)
    if on_progress is not None:
        on_progress({
            "paper_id": paper.id,
            "paper_title": paper.title,
            "stage": "summarize",
            "detail": "Summary generated",
        })
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
