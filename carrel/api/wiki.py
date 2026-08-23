"""LLM-compiled wiki endpoints (M8).

The wiki is a set of interlinked Markdown files compiled from the library's
papers. The ``wiki_pages`` table is a rebuildable index; these routes read the
index for lists and read disk for a page's body. Compilation is a single Job
per batch (mirroring :mod:`carrel.api.topics` but one Job for the whole run),
which the frontend polls via the existing jobs API.

M8a ships scholar pages only; concept/question/chat endpoints arrive in later
milestones but the routing/kind plumbing is already here.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlmodel import Session, select

from carrel.config import CarrelYAML
from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus, Paper, WikiKind, WikiPage, WikiSource
from carrel.pipeline.paper_extract import (
    PaperExtractError,
    extract_papers_pending,
)
from carrel.pipeline.wiki import _frontmatter, _links, _reindex, _scholars_agg
from carrel.pipeline.wiki.concept_compile import (
    ConceptError,
    compile_concept,
    compile_concepts_pending,
)
from carrel.pipeline.wiki.question_compile import (
    QuestionError,
    compile_question,
    compile_questions_pending,
)
from carrel.pipeline.wiki.scholar_compile import (
    ScholarError,
    compile_scholar,
    compile_scholars_pending,
)
from carrel.schemas import (
    JobOut,
    WikiBacklink,
    WikiCompileRequest,
    WikiPageDetail,
    WikiPageSummary,
    WikiSourceOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wiki", tags=["wiki"])


# ---------------------------------------------------------------------------
# Row -> schema
# ---------------------------------------------------------------------------


def _to_summary(row: WikiPage) -> WikiPageSummary:
    return WikiPageSummary(
        id=row.id or 0,
        kind=row.kind,
        slug=row.slug,
        title=row.title,
        summary=row.summary,
        tags=list(row.tags or []),
        links_in_count=row.links_in_count,
        confidence=row.confidence,
        evidence_count=row.evidence_count,
        scholar_aid=row.scholar_aid,
        question_status=row.question_status,
        stub=row.stub,
        entity_key=row.entity_key,
        redirects_to=row.redirects_to,
        compiled_at=row.compiled_at,
        updated_at=row.updated_at,
    )


def _read_body(cfg: CarrelYAML, row: WikiPage) -> tuple[dict[str, Any], str]:
    """Read a page's file, returning (frontmatter, body-without-frontmatter)."""
    full = Path(cfg.storage.root) / row.path
    if not full.exists():
        return {}, ""
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        return {}, ""
    return _frontmatter.parse(text)


def _sources_for(session: Session, page_id: int) -> list[WikiSourceOut]:
    rows = session.exec(
        select(WikiSource).where(WikiSource.wiki_page_id == page_id)
    ).all()
    paper_ids = list({r.paper_id for r in rows})
    papers = {
        p.id: p
        for p in session.exec(select(Paper).where(Paper.id.in_(paper_ids))).all()
    }
    out: list[WikiSourceOut] = []
    for r in rows:
        p = papers.get(r.paper_id)
        out.append(
            WikiSourceOut(
                paper_id=r.paper_id,
                paper_title=p.title if p else None,
                year=p.publication_date.year if p and p.publication_date else None,
                heading=r.heading,
                quote=r.quote,
                role=r.role,
            )
        )
    return out


def _backlinks_for(session: Session, row: WikiPage) -> list[WikiBacklink]:
    """Pages whose links_out resolve to this (kind, slug) — including via redirects.

    A page that links to an old slug (now a redirect shell) still counts as
    a backlink to the canonical page it points at.  Using
    :func:`_links.resolve_target` (rather than the literal ``(kind, slug)``
    match) preserves those backlinks across renames.
    """
    pages = session.exec(select(WikiPage)).all()
    out: list[WikiBacklink] = []
    seen: set[int] = set()
    for p in pages:
        if p.id == row.id or p.redirects_to is not None or not p.links_out:
            continue
        for href in p.links_out:
            target = _links.resolve_target(session, p.path, href)
            if target is not None and target.id == row.id and target.id not in seen:
                seen.add(target.id)
                out.append(
                    WikiBacklink(
                        id=p.id or 0, kind=p.kind, slug=p.slug, title=p.title
                    )
                )
                break
    out.sort(key=lambda b: b.title.lower())
    return out


def _page_detail(
    session: Session, row: WikiPage, *, cfg: CarrelYAML | None = None
) -> WikiPageDetail:
    """Build the full detail schema for a WikiPage row (reads disk + joins)."""
    # Lazy global config to avoid import cycles during app startup.
    if cfg is None:
        from carrel.main import app_config as cfg  # noqa: PLC0415

    meta, body = _read_body(cfg, row)
    page_id = row.id or 0
    return WikiPageDetail(
        **_to_summary(row).model_dump(),
        path=row.path,
        frontmatter=meta,
        body=body,
        sources=_sources_for(session, page_id) if page_id else [],
        backlinks=_backlinks_for(session, row),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/pages", response_model=list[WikiPageSummary])
def list_pages(
    kind: str | None = Query(None, description="Filter by concept|scholar|question"),
    q: str | None = Query(None, description="Substring match on title/summary"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_redirects: bool = Query(
        False,
        description="Include redirect shells (default: hide them).",
    ),
    session: Session = Depends(get_session_dep),
) -> list[WikiPageSummary]:
    stmt = select(WikiPage)
    if kind:
        if kind not in {k.value for k in WikiKind}:
            raise HTTPException(status_code=422, detail=f"unknown kind: {kind}")
        stmt = stmt.where(WikiPage.kind == kind)
    if not include_redirects:
        stmt = stmt.where(WikiPage.redirects_to.is_(None))
    rows = list(session.exec(stmt).all())
    if q:
        needle = q.strip().lower()
        if needle:
            rows = [
                r
                for r in rows
                if needle in (r.title or "").lower()
                or needle in (r.summary or "").lower()
            ]
    rows.sort(key=lambda r: (r.kind, -(r.evidence_count or 0), r.title.lower()))
    return [_to_summary(r) for r in rows[offset : offset + limit]]


def _get_page_row(session: Session, page_id: int) -> WikiPage:
    row = session.get(WikiPage, page_id)
    if row is None:
        raise HTTPException(status_code=404, detail="wiki page not found")
    return row


@router.get("/pages/{page_id}", response_model=WikiPageDetail)
def get_page(
    page_id: int,
    session: Session = Depends(get_session_dep),
) -> WikiPageDetail:
    return _page_detail(session, _get_page_row(session, page_id))


@router.get("/pages/by-kind-slug/{kind}/{slug}", response_model=WikiPageDetail)
def get_page_by_kind_slug(
    kind: str,
    slug: str,
    session: Session = Depends(get_session_dep),
) -> WikiPageDetail:
    if kind not in {k.value for k in WikiKind}:
        raise HTTPException(status_code=422, detail=f"unknown kind: {kind}")
    row = session.exec(
        select(WikiPage).where(WikiPage.kind == kind, WikiPage.slug == slug)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="wiki page not found")
    # If the row is a redirect shell, follow it to the canonical.  The
    # response includes a ``redirected_from`` summary so the frontend can
    # show a "this page moved" notice without making a second round-trip.
    redirected_from: WikiPageSummary | None = None
    if row.redirects_to is not None:
        redirected_from = _to_summary(row)
        target = _links.resolve_target(session, row.path, f"../{kind}s/{slug}.md")
        if target is None or target.id == row.id:
            # Broken redirect (e.g. target entity was deleted).  Surface the
            # shell so the user sees the redirect notice rather than a 404.
            return _page_detail_with_redirected_from(
                session, row, redirected_from=redirected_from
            )
        row = target
    return _page_detail_with_redirected_from(
        session, row, redirected_from=redirected_from
    )


def _page_detail_with_redirected_from(
    session: Session,
    row: WikiPage,
    *,
    redirected_from: WikiPageSummary | None,
) -> WikiPageDetail:
    """Build a WikiPageDetail, optionally tagging it with the slug the user
    originally requested when that slug is now a redirect shell."""
    detail = _page_detail(session, row)
    return detail.model_copy(update={"redirected_from": redirected_from})


# ---------------------------------------------------------------------------
# Compilation (one Job per batch)
# ---------------------------------------------------------------------------

# Stage identifiers for the multi-phase driver.  Each maps to a batch
# function below; the order here is the default execution order.
_STAGE_PAPER_EXTRACT = "paper_extract"
_STAGE_SCHOLAR = "scholar_compile"
_STAGE_CONCEPT = "concept_compile"
_STAGE_QUESTION = "question_compile"
_ALL_STAGES = (
    _STAGE_PAPER_EXTRACT,
    _STAGE_SCHOLAR,
    _STAGE_CONCEPT,
    _STAGE_QUESTION,
)
_VALID_STAGES = frozenset(_ALL_STAGES)


def _stage_did_work(stage: str, counts: dict) -> bool:
    """True if a stage produced changes worth cascading into the next stage.

    A stage is "no-op" when no live compile happened AND no failures
    occurred — i.e. there is nothing to follow up on.  Stubbed pages count
    as no-op (they did work but no LLM call, so they don't change the
    staleness of downstream stages).
    """
    if not counts:
        return False
    if counts.get("failed", 0):
        return True
    if stage == _STAGE_PAPER_EXTRACT:
        return counts.get("extracted", 0) > 0
    return counts.get("compiled", 0) > 0


def _make_progress_cb(session: Session, job_id: int):
    def _cb(progress: dict) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        stats = {**(job.stats or {})}
        # Stage-specific sub-counters live under a per-stage key; preserve
        # any prior stage results so a later stage's events don't clobber
        # the earlier ones.
        stage = progress.get("stage", "wiki_compile")
        if stage and stage != "wiki_compile":
            sub = {**(stats.get(stage) or {})}
            sub.update({k: v for k, v in progress.items()
                        if k not in {"stage", "index", "total", "name", "detail"}})
            stats[stage] = sub
        # Top-level "stage" reports the most-recent phase for the UI badge.
        stats["stage"] = stage
        detail = progress.get("detail", "")
        idx = progress.get("index")
        total = progress.get("total")
        name = progress.get("name", "")
        if detail:
            stats["detail"] = detail
        if idx and total:
            job.message = f"[{idx}/{total}] {name} — {detail}" if detail else f"[{idx}/{total}] {name}"
        elif detail:
            job.message = detail
        job.stats = stats
        session.add(job)
        session.commit()

    return _cb


def _run_batch(
    session: Session,
    job_id: int,
    limit: int,
    force: bool,
    stages: list[str] | None = None,
) -> None:
    """Run the multi-phase compile job.

    Stages run in the order: paper_extract → scholar_compile →
    concept_compile → question_compile.  Each stage is wrapped in its own
    try/except so a concept-stage crash does not roll back scholar work.
    Per-stage counts land under ``Job.stats[<stage>]``; the top-level
    ``stage`` field reports the most-recent phase for the UI.

    When a stage is a no-op (no LLM work done and no failures), the next
    stage is skipped under the assumption that the input didn't change
    and downstream staleness is unchanged.  The skip is reflected in the
    job message so an operator can see why a stage did not run.
    """
    from carrel.main import app_config  # noqa: PLC0415

    job = session.get(Job, job_id)
    progress = _make_progress_cb(session, job_id)
    selected = list(stages) if stages else list(_ALL_STAGES)
    unknown = [s for s in selected if s not in _VALID_STAGES]
    if unknown:
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"unknown stages: {unknown!r}"
            session.add(job)
            session.commit()
        return

    try:
        if job is not None:
            job.status = JobStatus.running.value
            job.started_at = datetime.now(UTC)
            session.add(job)
            session.commit()

        prev_noop = False
        per_stage: dict[str, dict] = {}

        for stage in selected:
            if prev_noop:
                progress({"stage": stage, "detail": f"Skipping {stage} (previous stage no-op)"})
                per_stage[stage] = {"skipped": True, "reason": "prev_noop"}
                continue
            try:
                if stage == _STAGE_PAPER_EXTRACT:
                    counts = extract_papers_pending(
                        session, app_config, limit=limit, force=force,
                        on_progress=progress,
                    )
                elif stage == _STAGE_SCHOLAR:
                    counts = compile_scholars_pending(
                        session, app_config, limit=limit, force=force,
                        on_progress=progress,
                    )
                elif stage == _STAGE_CONCEPT:
                    counts = compile_concepts_pending(
                        session, app_config, limit=limit, force=force,
                        on_progress=progress,
                    )
                else:  # _STAGE_QUESTION
                    counts = compile_questions_pending(
                        session, app_config, limit=limit, force=force,
                        on_progress=progress,
                    )
            except Exception as e:  # noqa: BLE001
                # Failure isolation: log + record, do not abort the job.
                logger.exception("wiki compile stage %s crashed", stage)
                per_stage[stage] = {"error": f"{type(e).__name__}: {e}"[:200]}
                # A crashed stage is not a "no-op" — keep going so the
                # user gets diagnostic data for every stage.
                prev_noop = False
                continue

            per_stage[stage] = counts
            prev_noop = not _stage_did_work(stage, counts)

        # Final pass: prune dead links on auto-generated concept/question
        # pages, then recompute backlinks so the UI shows fresh counts.
        try:
            pruned = _reindex.prune_dead_links(session)
            recomputed = _reindex.recompute_backlinks(session)
        except Exception:
            logger.exception("wiki compile: final reindex pass failed")
            pruned = None
            recomputed = None

        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            final_stats = {**(job.stats or {})}
            for stage, counts in per_stage.items():
                final_stats[stage] = counts
            final_stats["stage"] = "done"
            final_stats["detail"] = "Done."
            if pruned is not None:
                final_stats["pruned_pages"] = pruned
            if recomputed is not None:
                final_stats["recomputed_backlinks"] = recomputed
            job.stats = final_stats
            job.message = "Done."
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("wiki compile job %d crashed", job_id)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"[:200]
            session.add(job)
            session.commit()


def _run_batch_background(
    job_id: int, limit: int, force: bool, stages: list[str] | None
) -> None:
    from sqlmodel import Session as SqlSession  # noqa: PLC0415

    from carrel.db import get_app_engine  # noqa: PLC0415

    with SqlSession(get_app_engine()) as session:
        _run_batch(session, job_id, limit, force, stages=stages)


@router.post("/compile", response_model=JobOut)
def compile_wiki(
    body: WikiCompileRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> JobOut:
    """Compile the wiki as a single multi-stage batch Job.

    See :class:`carrel.schemas.WikiCompileRequest` for the stage list.
    """
    now = datetime.now(UTC)
    job = Job(
        kind=JobKind.wiki_compile.value,
        status=JobStatus.queued.value,
        message="Queued — wiki compile",
        stats={
            "stage": "queued",
            "detail": "Queued…",
            "limit": body.limit,
            "force": body.force,
            "stages": list(body.stages) if body.stages else list(_ALL_STAGES),
        },
        created_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    job_id = job.id
    assert job_id is not None

    if body.background:
        background.add_task(_run_batch_background, job_id, body.limit, body.force, body.stages)
    else:
        _run_batch(session, job_id, body.limit, body.force, stages=body.stages)
        session.refresh(job)
    return JobOut(
        id=job.id or 0,
        kind=job.kind,
        status=job.status,
        message=job.message,
        stats=job.stats,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
    )


@router.post("/pages/{page_id}/recompile", response_model=JobOut)
def recompile_page(
    page_id: int,
    bg_tasks: BackgroundTasks,
    background: bool = True,
    session: Session = Depends(get_session_dep),
) -> JobOut:
    """Force-recompile one wiki page of any kind (scholar, concept, question).

    By default runs in a background task (the frontend polls the Job); pass
    ``background=false`` to run inline within the request.
    """
    row = _get_page_row(session, page_id)
    # Resolve the per-kind compile key from the page row.  Concept and
    # question compilers need the *normalized* term / question string, not
    # the slug (the slug is a presentation form and may have been
    # truncated relative to what the aggregation stored).  We look the
    # candidate up by slug so the recompile hits the same candidate the
    # forward compile would.
    if row.kind == WikiKind.scholar.value:
        key = row.scholar_aid or f"{_scholars_agg.NAME_KEY_PREFIX}{row.title}"
        compile_fn = _recompile_scholar
    elif row.kind == WikiKind.concept.value:
        key = _resolve_concept_key(session, row)
        if key is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"concept '{row.slug}' has no in-library papers; "
                    "cannot recompile"
                ),
            )
        compile_fn = _recompile_concept
    elif row.kind == WikiKind.question.value:
        key = _resolve_question_key(session, row)
        if key is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"question '{row.slug}' has no in-library papers; "
                    "cannot recompile"
                ),
            )
        compile_fn = _recompile_question
    else:
        raise HTTPException(
            status_code=422, detail=f"unknown wiki kind: {row.kind!r}"
        )

    now = datetime.now(UTC)
    job = Job(
        kind=JobKind.wiki_recompile.value,
        status=JobStatus.queued.value,
        message=f"Queued — recompile {row.title}",
        stats={
            "stage": "queued",
            "detail": "Queued…",
            "wiki_page_id": page_id,
            "wiki_kind": row.kind,
            "compile_key": key,
        },
        created_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    job_id = job.id
    assert job_id is not None

    def _run_one(sess: Session, jid: int) -> None:
        from carrel.main import app_config  # noqa: PLC0415

        j = sess.get(Job, jid)
        try:
            if j is not None:
                j.status = JobStatus.running.value
                j.started_at = datetime.now(UTC)
                sess.add(j)
                sess.commit()
            compile_fn(sess, app_config, key)
            if j is not None:
                j.status = JobStatus.done.value
                j.finished_at = datetime.now(UTC)
                j.message = f"Recompiled {row.title}"
                j.stats = {**(j.stats or {}), "stage": "done"}
                sess.add(j)
                sess.commit()
        except (
            ScholarError, ConceptError, QuestionError, PaperExtractError
        ) as e:
            if j is not None:
                j.status = JobStatus.failed.value
                j.finished_at = datetime.now(UTC)
                j.message = str(e)[:200]
                sess.add(j)
                sess.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception("wiki recompile job %d crashed", jid)
            if j is not None:
                j.status = JobStatus.failed.value
                j.finished_at = datetime.now(UTC)
                j.message = f"{type(e).__name__}: {e}"[:200]
                sess.add(j)
                sess.commit()

    def _run_one_bg() -> None:
        from sqlmodel import Session as SqlSession  # noqa: PLC0415

        from carrel.db import get_app_engine  # noqa: PLC0415

        with SqlSession(get_app_engine()) as s:
            _run_one(s, job_id)

    if background:
        bg_tasks.add_task(_run_one_bg)
    else:
        _run_one(session, job_id)
        session.refresh(job)

    return JobOut(
        id=job.id or 0,
        kind=job.kind,
        status=job.status,
        message=job.message,
        stats=job.stats,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
    )


# Per-kind recompile shims.  Each takes (session, cfg, key, force=True) so
# the dispatcher above can wire them through the same background-task path.
def _recompile_scholar(session, cfg, key):
    return compile_scholar(session, cfg, key, force=True)


def _recompile_concept(session, cfg, key):
    return compile_concept(session, cfg, key, force=True)


def _recompile_question(session, cfg, key):
    return compile_question(session, cfg, key, force=True)


def _resolve_concept_key(session: Session, row: WikiPage) -> str | None:
    """Find the term_normalized for ``row.slug`` (concept page)."""
    from carrel.pipeline.wiki._concepts_agg import aggregate
    from carrel.pipeline.wiki._slug import slugify

    for cand in aggregate(session):
        if slugify(cand.term_display) == row.slug:
            return cand.term_normalized
    return None


def _resolve_question_key(session: Session, row: WikiPage) -> str | None:
    """Find the question_normalized for ``row.slug`` (question page)."""
    from carrel.pipeline.wiki._questions_agg import aggregate
    from carrel.pipeline.wiki._slug import slugify

    for cand in aggregate(session):
        if slugify(cand.question_display) == row.slug:
            return cand.question_normalized
    return None
