"""Shared LLM-extraction orchestration for paper_card / paper_extract / summarize.

The three per-paper LLM extraction pipelines repeat the same boilerplate:

- paper lookup, md_path existence, staleness gate
- min-body check
- read markdown from disk
- slice via :func:`carrel.pipeline._section_picker.prepare_picker_input`
- build ``[system, user]`` messages
- call :func:`carrel.llm.chat_json` with the project-wide model + timeout
- catch :class:`carrel.llm.LLMError` and re-raise as the feature's error
- emit progress and record LLM usage

The only feature-specific bits are the staleness predicate, the message
builder, the budget, the error class, the usage-recorder factory, and the
final commit.  This module hosts the shared driver so a bug fix (e.g.
force-rerun when the markdown path changes) only needs to land in one
place and so budget caps cannot drift between the three pipelines.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from carrel import llm
from carrel.config import CarrelYAML
from carrel.models import Paper
from carrel.pipeline._llm_recorder import make_record_usage_callback
from carrel.pipeline._paper_meta import LLM_EXTRACT_PER_SECTION_CAP
from carrel.pipeline._section_picker import prepare_picker_input

ProgressCallback = Callable[[dict], None]

# Per-section char cap shared by every extraction pipeline (summarize /
# paper_extract / paper_card). Source of truth is
# :data:`carrel.pipeline._paper_meta.LLM_EXTRACT_PER_SECTION_CAP`;
# imported here so the driver and the constant live next to each other
# and the picker call site below shows the value. Wiki compiles don't
# go through this — they pass a different (larger) cap.
_PER_SECTION_CAP = LLM_EXTRACT_PER_SECTION_CAP


# Staleness predicate: ``is_stale(session, paper) -> bool``. Takes the
# session because paper_extract needs to query PaperConcept / PaperQuestion
# rows; paper_card / summarize ignore it.
StalenessFn = Callable[[Session, Paper], bool]

# Usage recorder factory: returns an ``on_usage`` callback suitable for
# :func:`carrel.llm.chat_json`. The default ``make_record_usage_callback``
# also stamps tokens onto the ambient AgentStep; pass a different factory
# (e.g. :func:`carrel.usage.make_usage_callback`) when no paper_id is
# available or the ambient recorder is not in play.
UsageFactory = Callable[..., Callable[[str, str, Any], None]]


def _default_usage_factory(session: Session, *, paper_id: str, feature: str):
    return make_record_usage_callback(
        session, paper_id=paper_id, feature=feature
    )


def drive_paper_llm_extraction(
    session: Session,
    cfg: CarrelYAML,
    paper_id: str,
    *,
    feature: str,
    progress_stage: str,
    error_class: type[Exception],
    is_stale: StalenessFn,
    build_messages: Callable[[Paper, str], list[dict]],
    budget_chars: int,
    force: bool = False,
    min_body_chars: int | None = None,
    too_short_returns: bool = False,
    on_progress: ProgressCallback | None = None,
    usage_factory: UsageFactory | None = None,
) -> tuple[Paper, dict | None, str]:
    """Run the shared LLM-call orchestrator for a per-paper feature.

    Returns ``(paper, data, body)`` where ``data is None`` when the LLM
    was not called (either not stale, or — if ``too_short_returns=True`` —
    the raw body is shorter than ``min_body_chars``).  The caller does
    the feature-specific commit (coerce → write → status advance).

    Raises ``error_class`` on missing paper, missing md_path, missing
    LLM key, body-too-short (unless ``too_short_returns=True``), and
    :class:`carrel.llm.LLMError` from the LLM call.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise error_class(f"paper not found: {paper_id}")

    def _emit(**progress: Any) -> None:
        if on_progress is not None:
            on_progress({
                "paper_id": paper.id,
                "paper_title": paper.title,
                "stage": progress_stage,
                **progress,
            })

    if not paper.md_path:
        raise error_class("paper has no md_path; parse it first")
    md_path = Path(cfg.storage.root) / paper.md_path
    if not md_path.exists():
        raise error_class(f"parsed markdown missing on disk: {md_path}")

    if not force and not is_stale(session, paper):
        _emit(detail="Already up to date")
        return paper, None, ""

    if not llm.has_summarize_key(cfg):
        raise error_class(
            "no LLM API key configured (set DEEPSEEK_API_KEY or VOLCANO_API_KEY)"
        )

    md = md_path.read_text(encoding="utf-8", errors="replace")
    if min_body_chars is not None and len(md.strip()) < min_body_chars:
        if too_short_returns:
            _emit(detail="Body too short; skipping")
            return paper, None, ""
        raise error_class("paper body too short to extract")

    body = prepare_picker_input(
        md,
        budget_chars=budget_chars,
        per_section_cap=_PER_SECTION_CAP,
    )
    messages = build_messages(paper, body)

    _emit(detail=f"Calling LLM for {progress_stage}…")
    factory = usage_factory or _default_usage_factory
    try:
        data = llm.chat_json(
            messages,
            model=cfg.llm.summarize_model,
            fallback_model=cfg.llm.fallback_model,
            temperature=cfg.llm.temperature,
            timeout=cfg.llm.request_timeout_seconds,
            feature=feature,
            on_usage=factory(session, paper_id=paper.id, feature=feature),
        )
    except llm.LLMError as e:
        raise error_class(str(e)) from e

    return paper, data, body


def touch_paper_after_llm(paper: Paper, session: Session) -> None:
    """Bump ``paper.updated_at`` and commit. Used by features whose LLM
    result lives on the paper row (paper_card, summarize) so the row's
    timestamp drives downstream L1 ETag invalidation."""
    paper.updated_at = datetime.now(UTC)
    session.add(paper)
    session.commit()
    session.refresh(paper)
