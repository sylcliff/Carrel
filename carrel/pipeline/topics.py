"""LLM topic classification pipeline.

Classifies an in-library paper into 1-4 broad, human-readable research topics
(e.g. "LLM Agents", "Retrieval-Augmented Generation") using the paper's
metadata only (title, abstract, keywords, and source categories from arXiv /
OpenAlex / Semantic Scholar cached in ``raw_meta``). Unlike the summarize
step, this does NOT require a parsed PDF, so metadata-only library papers can
be classified.

The classifier is given the names of all existing topics and is told to reuse
one verbatim when it fits, inventing a new canonical name only when nothing
matches. Topics are many-to-many with papers (``Topic`` / ``PaperTopic``), so
they form a shared, browsable vocabulary that grows organically with the
library.

Design mirrors :mod:`carrel.pipeline.summarize`:
  * **Reuses** :func:`carrel.llm.chat_json` with the same model/config.
  * **Idempotent** — a paper that already has topics is skipped unless
    ``force=True``.
  * **Non-fatal** — a failure raises :class:`TopicsError` and never touches
    ``paper.status`` or ``paper.error``; embedding/search are unaffected.
  * **Synchronous** — one paper at a time (the LLM call is the bottleneck).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from carrel import llm, usage
from carrel.config import CarrelYAML
from carrel.models import Paper, PaperTopic, Topic

logger = logging.getLogger(__name__)


class TopicsError(Exception):
    """Topic classification failed (no key, bad LLM output, etc.)."""


ProgressCallback = Callable[[dict], None]

_MAX_TOPICS = 4
_MIN_TOPICS = 1


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "You are an expert research librarian. You classify an academic paper "
    "into BROAD, human-readable research topics based on its metadata.\n"
    "Rules:\n"
    "- Assign 1-4 topics. Prefer 1-2 when the paper is focused.\n"
    "- Topics are broad research THEMES (e.g. \"LLM Agents\", "
    "\"Retrieval-Augmented Generation\", \"Code Generation\", "
    "\"Reinforcement Learning\", \"Computer Vision\"), not narrow paper "
    "titles or free-floating keywords.\n"
    "- You will be given the list of EXISTING topic names. REUSE an existing "
    "topic name VERBATIM (exact spelling) whenever it fits. Only invent a new "
    "topic when none of the existing ones cover the paper's subject.\n"
    "- New topic names: short Title-Case noun phrases in English, no "
    "abbreviations unless universally known (e.g. \"LLM\"), no trailing "
    "punctuation.\n"
    '- For each topic include a one-sentence "description" (<= 100 chars) '
    "that could appear under the topic in a browse view.\n"
    "- Base every assignment ONLY on the metadata provided; do not invent "
    "topics unrelated to it. If metadata is sparse, still assign the most "
    "likely broad topic(s).\n"
    "- Respond with ONLY a JSON object, no prose or markdown fences, of the "
    'form: {"topics": [{"name": "...", "description": "..."}]}'
)


def _build_user_prompt(
    *,
    title: str,
    authors: str,
    venue_date: str,
    abstract: str,
    keywords: list[str],
    categories: list[str],
    existing_topics: list[str],
) -> str:
    parts = [f"Title: {title}", f"Authors: {authors or 'unknown'}"]
    if venue_date:
        parts.append(f"Venue/date: {venue_date}")
    if categories:
        parts.append(f"Source categories: {', '.join(categories)}")
    if keywords:
        parts.append(f"Keywords: {', '.join(keywords)}")
    if abstract:
        parts.append(f"Abstract:\n{abstract}")
    if existing_topics:
        parts.append(
            "Existing topic names (REUSE one verbatim if it fits):\n"
            + "\n".join(f"- {t}" for t in existing_topics)
        )
    else:
        parts.append("There are no existing topics yet; choose canonical names.")
    parts.append("\nReturn the JSON object now, with no commentary.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def _authors_string(paper: Paper) -> str:
    if not paper.authors:
        return ""
    names = [
        a.get("name", "")
        for a in paper.authors
        if isinstance(a, dict) and a.get("name")
    ]
    return ", ".join(names)


def _venue_date_string(paper: Paper) -> str:
    bits = [paper.venue or ""]
    if paper.publication_date:
        bits.append(str(paper.publication_date))
    return " · ".join(b for b in bits if b)


def _extract_source_categories(paper: Paper) -> list[str]:
    """Pull subject hints out of ``raw_meta`` across sources.

    * arXiv: ``raw_meta["categories"]`` (e.g. ``["cs.CL", "cs.AI"]``).
    * OpenAlex: ``primary_topic`` / ``topics[*]`` / ``concepts[*]``, which may
      live at the top level or under ``raw_meta["openalex"]``.
    * Semantic Scholar: ``fields_of_study`` / ``fieldsOfStudy``.
    """
    raw = paper.raw_meta
    if not raw:
        return []

    found: list[str] = []

    # arXiv categories (top-level key, possibly under an OA-enriched dict).
    for key in ("categories", "fields_of_study", "fieldsOfStudy"):
        val = raw.get(key)
        if isinstance(val, list):
            found.extend(str(v).strip() for v in val if v)

    # OpenAlex work may be nested under "openalex"; otherwise raw IS the work.
    oa = raw.get("openalex") if isinstance(raw.get("openalex"), dict) else raw
    for key in ("primary_topic", "topics", "concepts"):
        val = oa.get(key) if isinstance(oa, dict) else None
        if key == "primary_topic" and isinstance(val, dict):
            name = val.get("display_name")
            if name:
                found.append(str(name).strip())
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    name = item.get("display_name") or item.get("name")
                    if name:
                        found.append(str(name).strip())

    # Deduplicate while preserving order; drop very long junk strings.
    seen: set[str] = set()
    out: list[str] = []
    for s in found:
        key = s.lower()
        if key in seen or len(s) > 120:
            continue
        seen.add(key)
        out.append(s)
    return out


def _canonical_name(name: str) -> str | None:
    """Trim/squash whitespace; return None if empty."""
    return " ".join(name.strip().split()) or None


def _coerce_topics(data: dict[str, Any]) -> list[dict[str, str]]:
    """Validate the LLM payload into [{name, description}]."""
    raw = data.get("topics")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            name = _canonical_name(item)
            desc = ""
        elif isinstance(item, dict):
            name = _canonical_name(str(item.get("name", "")))
            desc = str(item.get("description", "")).strip()
        else:
            continue
        if not name:
            continue
        if len(name) > 100:
            name = name[:100].rstrip()
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "description": desc[:200]})
    return out


# ---------------------------------------------------------------------------
# Topic get-or-create
# ---------------------------------------------------------------------------


def _get_or_create_topic(
    session: Session, name: str, description: str
) -> Topic | None:
    """Find a topic by name (case-insensitive) or create it.

    The unique constraint is on the exact name; an ilike lookup keeps "LLM
    Agents" and "llm agents" from diverging in practice, and the
    IntegrityError fallback handles the rare single-user race.
    """
    existing = session.exec(select(Topic).where(Topic.name.ilike(name))).first()
    if existing is not None:
        return existing
    topic = Topic(name=name)
    if description:
        topic.description = description
    session.add(topic)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.exec(
            select(Topic).where(Topic.name.ilike(name))
        ).first()
        return existing
    session.refresh(topic)
    return topic


def _existing_topic_names(session: Session) -> list[str]:
    rows = session.exec(select(Topic.name).order_by(Topic.name)).all()
    return list(rows)


def _has_topics(session: Session, paper_id: str) -> bool:
    return session.exec(
        select(func.count(PaperTopic.topic_id)).where(
            PaperTopic.paper_id == paper_id
        )
    ).one() > 0


# ---------------------------------------------------------------------------
# Per-paper classification
# ---------------------------------------------------------------------------


def topics_paper(
    session: Session,
    cfg: CarrelYAML,
    paper_id: str,
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> Paper:
    """Assign 1-4 topics to one in-library paper; idempotent and non-fatal.

    Existing topics are reused when they fit; new topics are created. The
    paper's ``status`` is never changed.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise TopicsError(f"paper not found: {paper_id}")

    def _emit(**progress: Any) -> None:
        if on_progress is not None:
            on_progress({
                "paper_id": paper.id,
                "paper_title": paper.title,
                "stage": "topics",
                **progress,
            })

    if not force and _has_topics(session, paper.id):
        _emit(detail="Already classified")
        return paper

    # Fast no-key check: avoid a noisy stack trace when chaining after parse.
    if not (
        llm.has_key_for(cfg.llm.summarize_model)
        or llm.has_key_for(cfg.llm.fallback_model)
    ):
        raise TopicsError(
            "no LLM API key configured (set DEEPSEEK_API_KEY or VOLCANO_API_KEY)"
        )

    categories = _extract_source_categories(paper)
    existing = _existing_topic_names(session)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _build_user_prompt(
                title=paper.title,
                authors=_authors_string(paper),
                venue_date=_venue_date_string(paper),
                abstract=paper.abstract or "",
                keywords=list(paper.keywords or []),
                categories=categories,
                existing_topics=existing,
            ),
        },
    ]

    _emit(detail="Classifying topics…")
    try:
        data = llm.chat_json(
            messages,
            model=cfg.llm.summarize_model,
            fallback_model=cfg.llm.fallback_model,
            temperature=cfg.llm.temperature,
            timeout=cfg.llm.request_timeout_seconds,
            feature="topics",
            on_usage=usage.make_usage_callback(
                session, feature="topics", paper_id=paper.id,
            ),
        )
    except llm.LLMError as e:
        raise TopicsError(str(e)) from e

    assignments = _coerce_topics(data)
    if not assignments:
        raise TopicsError("LLM returned no usable topics")

    # Clamp to the max after we've deduped; keep the first (most relevant).
    assignments = assignments[:_MAX_TOPICS]
    if len(assignments) < _MIN_TOPICS:  # pragma: no cover - _MIN_TOPICS == 1
        raise TopicsError("LLM returned no usable topics")

    if force:
        for link in session.exec(
            select(PaperTopic).where(PaperTopic.paper_id == paper.id)
        ).all():
            session.delete(link)
        session.flush()

    assigned: list[str] = []
    for a in assignments:
        topic = _get_or_create_topic(session, a["name"], a["description"])
        if topic is None or topic.id is None:
            continue
        if session.get(PaperTopic, (paper.id, topic.id)) is None:
            session.add(PaperTopic(paper_id=paper.id, topic_id=topic.id))
        assigned.append(topic.name)
    session.commit()

    paper.updated_at = datetime.now(UTC)
    session.add(paper)
    session.commit()
    session.refresh(paper)
    _emit(detail=f"Topics: {', '.join(assigned)}")
    logger.info("classified %s into topics: %s", paper.id, assigned)
    return paper


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def select_pending_topics(session: Session, limit: int = 20) -> list[Paper]:
    """In-library papers with no topics (metadata-only is fine)."""
    pt_subq = (
        select(PaperTopic.paper_id)
        .where(PaperTopic.paper_id == Paper.id)
        .exists()
    )
    stmt = (
        select(Paper)
        .where(Paper.in_library.is_(True), ~pt_subq)
        .order_by(Paper.created_at.desc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def topics_pending(
    session: Session,
    cfg: CarrelYAML,
    *,
    limit: int = 20,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Classify a batch of eligible papers; returns counts."""
    papers = select_pending_topics(session, limit=limit)
    counts = {"candidates": len(papers), "classified": 0, "failed": 0, "skipped": 0}
    total = len(papers)

    def _wrap(i: int, title: str):
        def _cb(progress: dict) -> None:
            if on_progress is not None:
                on_progress({**progress, "index": i, "total": total, "title": title})
        return _cb

    for i, paper in enumerate(papers, start=1):
        try:
            topics_paper(
                session, cfg, paper.id, force=force, on_progress=_wrap(i, paper.title)
            )
            counts["classified"] += 1
        except TopicsError as e:
            logger.info("topics %s failed: %s", paper.id, e)
            counts["failed"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("topics %s crashed: %s", paper.id, e)
            counts["failed"] += 1

    logger.info(
        "topics batch done: candidates=%d classified=%d failed=%d",
        counts["candidates"], counts["classified"], counts["failed"],
    )
    return counts
