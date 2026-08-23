"""Scholar dedup endpoints — detect and resolve duplicate OpenAlex A-IDs.

OpenAlex frequently splits one real researcher across multiple Author IDs. The
pipeline in :mod:`carrel.pipeline.scholar_dedup` scores same-named A-ID clusters
and auto-merges high-confidence pairs into ``scholar_aliases`` rows; the
remaining pairs surface here as *suggestions* for the user to Accept or Reject.

Aliases are an indirection layer (alias_aid -> canonical_aid) resolved by the
scholar aggregator; ``Paper.authors`` is never rewritten, so merges are
reversible by deleting the alias row.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus, ScholarAlias
from carrel.pipeline import scholar_dedup as dedup
from carrel.pipeline.wiki import _scholars_agg
from carrel.schemas import JobOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scholar-dedup", tags=["scholar-dedup"])

# Last scoring pass result (in-process). Scoring fans out one OpenAlex Authors
# call per A-ID (~10s for 70 A-IDs), so it only runs when the user clicks Scan
# duplicates (POST /run); GET /suggestions reads this cache. Merge/reject also
# drop the affected pair from the cached suggestions without a full rescore.
_suggestion_lock = threading.Lock()
_cached_suggestions: list[dict[str, Any]] = []
_cached_at: datetime | None = None  # informational only (last rescore time)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class DedupRunRequest(BaseModel):
    auto_apply: bool = True
    background: bool = True


class MergeRequest(BaseModel):
    alias_aid: str
    canonical_aid: str
    display_name: str | None = None


class RejectRequest(BaseModel):
    a: str
    b: str
    display_name: str | None = None


class SuggestionOut(BaseModel):
    a: str
    b: str
    display_name: str | None
    score: float
    coauthor: float
    affiliation: float
    topic: float
    reasons: list[str]
    paper_counts: dict[str, int]
    affiliations: dict[str, str | None]


class AppliedAliasOut(BaseModel):
    alias_aid: str
    canonical_aid: str
    display_name: str | None
    source: str
    confidence: float
    reasons: list[str]


class DedupSnapshot(BaseModel):
    suggestions: list[SuggestionOut]
    applied: list[AppliedAliasOut]
    rejected: list[AppliedAliasOut]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_out_row(r: ScholarAlias) -> AppliedAliasOut:
    return AppliedAliasOut(
        alias_aid=r.alias_aid,
        canonical_aid=r.canonical_aid,
        display_name=r.display_name,
        source=r.source,
        confidence=r.confidence,
        reasons=list(r.reasons or []),
    )


def _drop_cached_pair(a: str, b: str) -> None:
    """Remove a pair from the cached suggestions after a user action (no rescore)."""
    pair = tuple(sorted((a, b)))
    with _suggestion_lock:
        surviving = [
            s for s in _cached_suggestions
            if tuple(sorted((s["a"], s["b"]))) != pair
        ]
        globals()["_cached_suggestions"] = surviving


def _rescore(session: Session) -> list[dict[str, Any]]:
    """Run scoring (read-only) and cache suggestions."""
    global _cached_suggestions, _cached_at
    result = dedup.run_dedup(session, auto_apply=False)
    with _suggestion_lock:
        _cached_suggestions = result.suggestions
        _cached_at = datetime.now(UTC)
    return result.suggestions


def _gather_snapshot(session: Session, *, rescore: bool = False) -> DedupSnapshot:
    """Build the panel payload.

    ``GET /suggestions`` must never block on OpenAlex — opening the panel should
    be instant even before any scan has run. Scoring (one Authors call per A-ID)
    is expensive, so it only happens when the user clicks **Scan duplicates**
    (``POST /run``), which primes this cache. On GET we serve whatever is cached,
    falling back to an empty suggestion list until a scan runs.
    """
    applied = [
        _to_out_row(r)
        for r in session.exec(
            select(ScholarAlias).where(ScholarAlias.source.in_(["auto", "user"]))
        ).all()
    ]
    rejected = [
        _to_out_row(r)
        for r in session.exec(
            select(ScholarAlias).where(ScholarAlias.source == "reject")
        ).all()
    ]

    # POST /run passes rescore=True to run the scoring pass and refresh the
    # cache; GET serves the cache only (possibly empty).
    if rescore:
        raw = _rescore(session)
    else:
        with _suggestion_lock:
            raw = list(_cached_suggestions)

    # Drop any pair that's since been merged or rejected so the panel never
    # shows an action that's already taken — no need to rescore for this.
    applied_pairs = {(row.alias_aid, row.canonical_aid) for row in applied}
    rejected_pairs = {(row.alias_aid, row.canonical_aid) for row in rejected}

    def _is_open(s: dict[str, Any]) -> bool:
        pair = tuple(sorted((s["a"], s["b"])))
        for ap in applied_pairs:
            if tuple(sorted(ap)) == pair:
                return False
        for rp in rejected_pairs:
            if tuple(sorted(rp)) == pair:
                return False
        return True

    suggestions = [
        SuggestionOut(
            a=s["a"], b=s["b"],
            display_name=s.get("display_name"),
            score=float(s["score"]),
            coauthor=float(s["coauthor"]),
            affiliation=float(s["affiliation"]),
            topic=float(s["topic"]),
            reasons=list(s["reasons"]),
            paper_counts={k: int(v) for k, v in s["paper_counts"].items()},
            affiliations={k: v for k, v in s["affiliations"].items()},
        )
        for s in raw
        if _is_open(s)
    ]
    return DedupSnapshot(suggestions=suggestions, applied=applied, rejected=rejected)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/suggestions", response_model=DedupSnapshot)
def list_suggestions(session: Session = Depends(get_session_dep)) -> DedupSnapshot:
    return _gather_snapshot(session)


@router.post("/run", response_model=JobOut)
def run_dedup(
    body: DedupRunRequest,
    bg: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> JobOut:
    """Run the dedup scoring pass as a Job."""
    now = datetime.now(UTC)
    job = Job(
        kind=JobKind.scholar_dedup.value,
        status=JobStatus.queued.value,
        message="Queued — scholar dedup",
        stats={
            "stage": "queued",
            "auto_apply": body.auto_apply,
        },
        created_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    job_id = job.id
    assert job_id is not None

    def _run(sess: Session) -> None:
        j = sess.get(Job, job_id)
        try:
            if j is not None:
                j.status = JobStatus.running.value
                j.started_at = datetime.now(UTC)
                sess.add(j)
                sess.commit()

            def _progress(p: dict[str, Any]) -> None:
                jj = sess.get(Job, job_id)
                if jj is None:
                    return
                jj.stats = {**(jj.stats or {}), **p}
                jj.message = p.get("detail") or jj.message
                sess.add(jj)
                sess.commit()

            result = dedup.run_dedup(
                sess,
                auto_apply=body.auto_apply,
                on_progress=_progress,
            )
            _scholars_agg.invalidate_alias_cache()
            # If the run auto-merged any pairs, the wiki catalog now points
            # at pages whose entity_key no longer matches a live author.
            # Reconcile so the alias's page becomes a redirect shell to the
            # canonical.  Best-effort — a reconcile failure must not lose
            # the dedup result the user is waiting on.
            if result.auto_merged > 0:
                try:
                    from carrel.pipeline.wiki._entities import reconcile_scholars
                    reconcile_scholars(sess)
                except Exception:
                    logger.exception("dedup.run: scholar reconcile failed")
            # Rebuild the suggestion cache from the just-scored pass so the
            # panel opens instantly and the merge/reject actions below mutate
            # this snapshot rather than paying for another rescore.
            with _suggestion_lock:
                globals()["_cached_suggestions"] = list(result.suggestions)
                globals()["_cached_at"] = datetime.now(UTC)
            if j is not None:
                j.status = JobStatus.done.value
                j.finished_at = datetime.now(UTC)
                j.stats = {
                    **(j.stats or {}),
                    "stage": "done",
                    "candidates": result.candidates,
                    "auto_merged": result.auto_merged,
                    "suggested": result.suggested,
                    "skipped_rejected": result.skipped_rejected,
                }
                j.message = (
                    f"merged={result.auto_merged} suggested={result.suggested} "
                    f"rejected_skipped={result.skipped_rejected}"
                )
                sess.add(j)
                sess.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception("scholar dedup job %d crashed", job_id)
            jj = sess.get(Job, job_id)
            if jj is not None:
                jj.status = JobStatus.failed.value
                jj.finished_at = datetime.now(UTC)
                jj.message = f"{type(e).__name__}: {e}"[:200]
                sess.add(jj)
                sess.commit()

    def _run_bg() -> None:
        from sqlmodel import Session as SqlSession  # noqa: PLC0415

        from carrel.db import get_app_engine  # noqa: PLC0415

        with SqlSession(get_app_engine()) as s:
            _run(s)

    if body.background:
        bg.add_task(_run_bg)
    else:
        _run(session)
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


@router.post("/merge", response_model=AppliedAliasOut)
def merge_pair(
    body: MergeRequest,
    session: Session = Depends(get_session_dep),
) -> AppliedAliasOut:
    if not body.alias_aid or not body.canonical_aid:
        raise HTTPException(status_code=422, detail="alias_aid and canonical_aid required")
    if body.alias_aid == body.canonical_aid:
        raise HTTPException(status_code=422, detail="cannot merge an A-ID with itself")
    row = dedup.apply_user_merge(
        session,
        alias_aid=body.alias_aid,
        canonical_aid=body.canonical_aid,
        display_name=body.display_name,
    )
    _scholars_agg.invalidate_alias_cache()
    _drop_cached_pair(body.alias_aid, body.canonical_aid)
    # Reconcile the wiki catalog so the alias's existing page is converted
    # to a redirect shell pointing at the canonical.  Non-fatal: a wiki
    # reconcile failure must not undo the merge or surface as 500.
    try:
        from carrel.pipeline.wiki._entities import reconcile_scholars
        reconcile_scholars(session)
    except Exception:
        logger.exception("dedup.merge: scholar reconcile failed")
    return _to_out_row(row)


@router.post("/reject", response_model=AppliedAliasOut)
def reject_pair(
    body: RejectRequest,
    session: Session = Depends(get_session_dep),
) -> AppliedAliasOut:
    if not body.a or not body.b or body.a == body.b:
        raise HTTPException(status_code=422, detail="two distinct A-IDs required")
    row = dedup.apply_user_reject(
        session, a=body.a, b=body.b, display_name=body.display_name
    )
    _scholars_agg.invalidate_alias_cache()
    _drop_cached_pair(body.a, body.b)
    # A reject *un*merges any auto-merge the two A-IDs previously had.
    # The previously-aliased page may now be its own author again — run
    # reconcile so the catalog reflects the corrected state.
    try:
        from carrel.pipeline.wiki._entities import reconcile_scholars
        reconcile_scholars(session)
    except Exception:
        logger.exception("dedup.reject: scholar reconcile failed")
    return _to_out_row(row)


@router.delete("/aliases/{alias_aid}/{canonical_aid}", response_model=dict)
def delete_alias(
    alias_aid: str,
    canonical_aid: str,
    session: Session = Depends(get_session_dep),
) -> dict:
    """Remove an alias (or rejection) so the pair becomes independent again."""
    row = session.exec(
        select(ScholarAlias).where(
            ScholarAlias.alias_aid == alias_aid,
            ScholarAlias.canonical_aid == canonical_aid,
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="alias not found")
    session.delete(row)
    session.commit()
    _scholars_agg.invalidate_alias_cache()
    # Undo may re-open a pair the cache had already filtered out; force a
    # rescore on next GET rather than trying to surgically restore it.
    with _suggestion_lock:
        globals()["_cached_at"] = None
    return {"deleted": True}
