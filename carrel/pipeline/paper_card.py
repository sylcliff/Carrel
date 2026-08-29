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

The shared LLM-call orchestration (paper lookup, md_path check,
staleness gate, body prep, LLM call, LLMError catch, progress + usage
recording) lives in :mod:`carrel.pipeline._llm_extract` so a bug fix
or budget change only lands in one place.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session

from carrel import llm  # re-exported for tests that monkey-patch pipe.llm
from carrel import prompts_runtime
from carrel.config import CarrelYAML
from carrel.models import Paper
from carrel.pipeline._llm_extract import (
    drive_paper_llm_extraction,
    touch_paper_after_llm,
)
from carrel.pipeline._paper_meta import USER_PROMPT_TEMPLATE, authors_string, venue_date_string
from carrel.prompts_language import language_directive
from carrel.schemas import PaperCardOut, PaperTypeEnum, ResultClaim

logger = logging.getLogger(__name__)


class PaperCardError(Exception):
    """Per-paper card extraction failed (no markdown, no LLM key, bad output)."""


ProgressCallback = Callable[[dict], None]

# Cap on LLM input.  The picker fills the budget with the highest-
# priority sections (method → results → conclusion → intro) and drops
# references / acknowledgments / supplementary / appendix / funding
# boilerplate, so 8k is plenty when sections are short and tight enough
# to fail gracefully on a paper with no recognisable headings.
_MAX_INPUT_CHARS = 8_000
# Don't waste an LLM call on near-empty bodies.
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
    "- The paper text below is sliced by section type and numbered "
    "(## [1] Methods, ## [2] Results, …). Use the section label to "
    "attribute each field to the right block: method_name / method_summary "
    "/ key_techniques from the Method block, main_results / metrics from "
    "the Results block, conclusion / limitations / future_work from the "
    "Conclusion block.\n"
    "- Respond with ONLY the JSON object, no prose or markdown fences."
)


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


def _is_stale(_session: Session, paper: Paper) -> bool:
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
    def _build_messages(paper: Paper, body: str) -> list[dict]:
        # ``cfg.llm.output_language`` is read fresh on every call so a
        # PATCH to /api/settings is live on the next extraction without
        # waiting for the prompts_runtime 60s TTL (which only caches
        # override rows, not cfg-derived directives).
        system = prompts_runtime.get_system("paper_card", _SYSTEM_PROMPT)
        system = f"{system}\n\n{language_directive(cfg.llm.output_language)}"
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": prompts_runtime.get_user_template("paper_card", USER_PROMPT_TEMPLATE).format(
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
        feature="paper_card",
        progress_stage="paper_card",
        error_class=PaperCardError,
        is_stale=_is_stale,
        build_messages=_build_messages,
        budget_chars=_MAX_INPUT_CHARS,
        force=force,
        min_body_chars=_MIN_BODY_CHARS,
        on_progress=on_progress,
    )
    if data is None:
        return paper  # not stale — driver already emitted the "up to date" signal

    card = _coerce_card(data)
    paper.paper_card = card.model_dump()
    paper.paper_card_extracted_at = datetime.now(UTC)
    touch_paper_after_llm(paper, session)
    logger.info(
        "paper card %s: type=%s confidence=%.2f",
        paper.id, card.paper_type.value, card.confidence,
    )
    return paper
