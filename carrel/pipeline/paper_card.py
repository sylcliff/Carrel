"""LLM extraction of a structured paper card.

Drives a parsed paper through an LLM that fills a fixed schema
(:class:`carrel.schemas.PaperCardOut`) covering the paper's research
question, method, resources, results, and conclusions.  The result is
stored as JSON on ``papers.paper_card`` so the schema can evolve without
further migrations.

Design mirrors :mod:`carrel.pipeline.summarize` and
:mod:`carrel.pipeline.paper_extract`:
  * **Reuses** :func:`carrel.llm.chat_json` with the same model config.
  * **Idempotent** — a paper with an existing card is skipped unless
    ``force=True``.  Staleness is ``paper.updated_at >
    paper.paper_card_extracted_at``; no separate queue column.
  * **Non-fatal** — failures raise :class:`PaperCardError` and are caught
    by the API layer; the paper's ``status`` is never touched and any
    existing card JSON is preserved.
  * **Single-paper only** — a card requires ~5-15s of LLM time per paper.
    The endpoint runs inline (synchronous) rather than spawning a Job;
    re-running for many papers is a future concern (batch via Jobs).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from carrel import llm, prompts_runtime
from carrel.config import CarrelYAML
from carrel.models import Paper
from carrel.pipeline._llm_recorder import make_record_usage_callback
from carrel.schemas import PaperCardOut, PaperTypeEnum, ResultClaim

logger = logging.getLogger(__name__)


class PaperCardError(Exception):
    """Per-paper card extraction failed (no markdown, no LLM key, bad output)."""


ProgressCallback = Callable[[dict], None]

# Cap on LLM input.  We send the first ~2 + last ~2 sections (≈ abstract +
# intro + method + conclusion) — the same "head + tail" pick that
# paper_extract uses, since the card fields all live in those bands.
_MAX_INPUT_CHARS = 8_000
_MIN_BODY_CHARS = 200


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

# Field whitelist — every field in the schema, in the order the LLM sees it.
# Keeping it in one place means adding a new field only needs a schema +
# whitelist update, not a prompt rewrite.
_ALLOWED_LIST_FIELDS = (
    "key_techniques",
    "datasets",
    "baselines",
    "model_urls",
    "metrics",
    "limitations",
    "future_work",
)


_SYSTEM_PROMPT = (
    "You are an expert research assistant. You read an academic paper and "
    "extract its core facts into a fixed JSON schema. Rules:\n"
    "- Base every claim ONLY on the paper text provided; never invent "
    "results, numbers, datasets, or URLs.\n"
    "- If a field is not mentioned in the paper, return null (or [] for "
    "lists). Do NOT guess.\n"
    "- Be concise: text fields should be 1-2 sentences; lists should hold "
    "at most 5 items.\n"
    "- main_results entries: write a short claim string, and where the paper "
    "gives an explicit number, set ``value`` (float) and ``unit``. Include "
    "the dataset name when the paper ties a number to one.\n"
    "- code_url must be a real URL from the paper (GitHub repo, project "
    "page, etc.); null if none.\n"
    "- confidence is your own 0..1 self-rating of how grounded the output "
    "is (1.0 = every field directly stated; 0.3 = mostly inferred).\n"
    "- paper_type is one of: research | survey | benchmark | system | "
    "position | case_study | other. Default: research.\n"
    "- Respond with ONLY the JSON object, no prose or markdown fences."
)


_USER_TEMPLATE = (
    "Title: {title}\n"
    "Authors: {authors}\n"
    "Venue/date: {venue_date}\n\n"
    "Abstract:\n{abstract}\n\n"
    "Paper text (parsed from PDF; may contain OCR noise; truncated):\n\n"
    "{body}\n\n"
    "Return the JSON object now."
)


# ---------------------------------------------------------------------------
# Body preparation
# ---------------------------------------------------------------------------


def _strip_image_lines(md: str) -> str:
    """Drop lines that start with ``!`` (MinerU image markup)."""
    return "\n".join(
        line for line in md.splitlines() if not line.lstrip().startswith("!")
    )


def _pick_head_tail(md: str) -> str:
    """Pick a head + tail window of the body for the LLM.

    Mirrors :mod:`carrel.pipeline.paper_extract`: if the markdown has ATX
    headings, take the first 2 + last 2 sections (with their headings so
    the LLM sees the structure).  Otherwise fall back to first + last char
    windows.
    """
    from carrel.chunking import split_by_heading

    sections = split_by_heading(md)
    if sections:
        if len(sections) <= 4:
            picked = sections
        else:
            picked = sections[:2] + sections[-2:]
        parts: list[str] = []
        for heading, body in picked:
            if heading:
                parts.append(f"## {heading}")
            parts.append(body)
        return "\n\n".join(parts).strip()

    cleaned = md.strip()
    if len(cleaned) <= 3_000:
        return cleaned
    head = cleaned[:1_500].rstrip()
    tail = cleaned[-1_500:].lstrip()
    return f"{head}\n\n…\n\n{tail}"


def _prepare_body(md: str, max_chars: int) -> str:
    if not md or not md.strip():
        return ""
    md = _strip_image_lines(md)
    body = _pick_head_tail(md)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n…[truncated]"
    return body


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


# ---------------------------------------------------------------------------
# Response coercion
# ---------------------------------------------------------------------------


def _coerce_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _coerce_result_claims(value: Any) -> list[ResultClaim]:
    if not isinstance(value, list):
        return []
    out: list[ResultClaim] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        claim = _coerce_str(item.get("claim"))
        if not claim:
            continue
        try:
            value_num = item.get("value")
            baseline_num = item.get("baseline_value")
            out.append(
                ResultClaim(
                    claim=claim,
                    value=float(value_num) if isinstance(value_num, (int, float)) else None,
                    unit=_coerce_str(item.get("unit")),
                    dataset=_coerce_str(item.get("dataset")),
                    baseline_value=float(baseline_num) if isinstance(baseline_num, (int, float)) else None,
                    baseline_label=_coerce_str(item.get("baseline_label")),
                )
            )
        except (TypeError, ValueError):
            # Bad numeric coercion → keep just the claim string.
            out.append(ResultClaim(claim=claim))
    return out


def _coerce_paper_type(value: Any) -> PaperTypeEnum:
    if not isinstance(value, str):
        return PaperTypeEnum.research
    try:
        return PaperTypeEnum(value.strip().lower())
    except ValueError:
        return PaperTypeEnum.other


def _coerce_card(data: dict[str, Any]) -> PaperCardOut:
    """Validate the LLM payload into a :class:`PaperCardOut`.

    Anything that doesn't survive coercion is dropped (the rest of the
    card is still useful).  Drops never raise — a partially-filled card
    is far better than none.
    """
    cleaned: dict[str, Any] = {}
    # text fields
    for field in (
        "research_question",
        "motivation",
        "hypothesis",
        "method_name",
        "method_summary",
        "code_url",
        "conclusion",
    ):
        v = _coerce_str(data.get(field))
        if v is not None:
            cleaned[field] = v
    # list-of-str fields
    for field in _ALLOWED_LIST_FIELDS:
        v = _coerce_str_list(data.get(field))
        if v:
            cleaned[field] = v
    # main_results
    results = _coerce_result_claims(data.get("main_results"))
    if results:
        cleaned["main_results"] = results
    # paper_type
    cleaned["paper_type"] = _coerce_paper_type(data.get("paper_type"))
    # confidence: clamp into 0..1
    conf = data.get("confidence")
    if isinstance(conf, (int, float)):
        try:
            cleaned["confidence"] = max(0.0, min(1.0, float(conf)))
        except (TypeError, ValueError):
            pass
    return PaperCardOut(**cleaned)


# ---------------------------------------------------------------------------
# Per-paper extraction
# ---------------------------------------------------------------------------


def _is_stale(paper: Paper) -> bool:
    """A paper is stale if it has no card, or if it was updated after extract."""
    if paper.paper_card is None or paper.paper_card_extracted_at is None:
        return True
    if paper.updated_at is None:
        return False
    return paper.updated_at > paper.paper_card_extracted_at


def extract_paper_card(
    session: Session,
    cfg: CarrelYAML,
    paper_id: str,
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> Paper:
    """Extract a :class:`PaperCardOut` for one paper.  Idempotent, non-fatal.

    Raises :class:`PaperCardError` on missing markdown, no LLM key, or a
    malformed response.  The paper row is updated in place; no status flip.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise PaperCardError(f"paper not found: {paper_id}")

    def _emit(**progress: Any) -> None:
        if on_progress is not None:
            on_progress({
                "paper_id": paper.id,
                "paper_title": paper.title,
                "stage": "paper_card",
                **progress,
            })

    if not paper.md_path:
        raise PaperCardError("paper has no md_path; parse it first")
    md_path = Path(cfg.storage.root) / paper.md_path
    if not md_path.exists():
        raise PaperCardError(f"parsed markdown missing on disk: {md_path}")

    if not force and not _is_stale(paper):
        _emit(detail="Card already up to date")
        return paper

    if not (
        llm.has_key_for(cfg.llm.summarize_model)
        or llm.has_key_for(cfg.llm.fallback_model)
    ):
        raise PaperCardError(
            "no LLM API key configured (set DEEPSEEK_API_KEY or VOLCANO_API_KEY)"
        )

    md = md_path.read_text(encoding="utf-8", errors="replace")
    if len(md.strip()) < _MIN_BODY_CHARS:
        raise PaperCardError("paper body too short to extract a card")

    body = _prepare_body(md, _MAX_INPUT_CHARS)
    messages = [
        {"role": "system", "content": prompts_runtime.get_system("paper_card", _SYSTEM_PROMPT)},
        {
            "role": "user",
            "content": prompts_runtime.get_user_template("paper_card", _USER_TEMPLATE).format(
                title=paper.title,
                authors=_authors_string(paper) or "unknown",
                venue_date=_venue_date_string(paper) or "unknown",
                abstract=paper.abstract or "",
                body=body,
            ),
        },
    ]

    _emit(detail="Generating paper card…")
    try:
        data = llm.chat_json(
            messages,
            model=cfg.llm.summarize_model,
            fallback_model=cfg.llm.fallback_model,
            temperature=cfg.llm.temperature,
            timeout=cfg.llm.request_timeout_seconds,
            feature="paper_card",
            on_usage=make_record_usage_callback(
                session, paper_id=paper.id, feature="paper_card"
            ),
        )
    except llm.LLMError as e:
        raise PaperCardError(str(e)) from e

    card = _coerce_card(data)
    paper.paper_card = card.model_dump()
    paper.paper_card_extracted_at = datetime.now(UTC)
    paper.updated_at = datetime.now(UTC)
    session.add(paper)
    session.commit()
    session.refresh(paper)
    _emit(detail="Card generated")
    logger.info("paper card %s: type=%s confidence=%.2f", paper.id, card.paper_type.value, card.confidence)
    return paper
