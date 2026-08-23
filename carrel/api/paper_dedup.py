"""Paper dedup endpoints — detect and resolve duplicate papers.

Paper dedup complements the scholar-level dedup: it catches cases where the
same paper has multiple rows in the library because of cross-id collisions
(DOI vs arXiv vs s2 paperId vs journal-doi bridge). The pipeline in
:mod:`carrel.pipeline.paper_dedup` scores in-library pairs and auto-merges
high-confidence matches; the remaining borderline pairs surface here as
*suggestions* for the user to Accept or Reject.

Merges are an indirection layer (``paper_aliases.alias_paper_id ->
canonical_paper_id``). On merge the loser's user_state (notes / favorite /
tags / topics / chat / chunks / wiki_sources / citation lists) is migrated to
the canonical; a :class:`PaperMergeEvent` row snapshots the loser's
pre-migration state for audit. Undo is best-effort: deleting the alias row
un-flags the loser's status but does not restore the migrated user state.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from carrel.db import get_app_engine, get_session_dep
from carrel.models import Job, JobKind, JobStatus, Paper, PaperAlias
from carrel.pipeline import paper_dedup as dedup
from carrel.pipeline import paper_dedup_judge as judge
from carrel.pipeline import paper_dedup_ops as ops
from carrel.schemas import JobOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/paper-dedup", tags=["paper-dedup"])

# In-process cache of the last scoring pass. Scoring is purely deterministic
# by default — no network calls — so the panel can refresh synchronously. We
# invalidate the cache whenever the underlying data changes (merge, reject,
# undo) so the panel always reflects the latest action without a rescore.
_suggestion_lock = threading.Lock()
_cached_suggestions: list[dict[str, Any]] = []
_cached_components: list[dict[str, Any]] = []
_cached_at: datetime | None = None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PaperDedupRunRequest(BaseModel):
    auto_apply: bool = True
    background: bool = True


class PaperMergeRequest(BaseModel):
    alias_paper_id: str
    canonical_paper_id: str
    display_label: str | None = None


class PaperRejectRequest(BaseModel):
    a: str
    b: str
    display_label: str | None = None


class PaperJudgeRequest(BaseModel):
    a: str
    b: str


class PaperJudgeResponse(BaseModel):
    a: str
    b: str
    verdict: str
    confidence: float
    reasons: list[str]
    model: str | None
    prompt_version: int | None
    cached: bool


class PaperPairSuggestion(BaseModel):
    a: str
    b: str
    score: float
    title: float
    authors: float
    strong_anchors: list[str]
    reasons: list[str]
    llm_verdict: dict[str, Any] | None
    title_a: str | None
    title_b: str | None
    year_a: int | None
    year_b: int | None
    doi_a: str | None
    doi_b: str | None
    arxiv_id_a: str | None
    arxiv_id_b: str | None
    s2_paper_id_a: str | None
    s2_paper_id_b: str | None


class PaperDedupComponent(BaseModel):
    canonical_id: str
    alias_ids: list[str]
    display_label: str | None
    reasons: list[str]
    avg_score: float
    sources: list[str]


class PaperDedupAlias(BaseModel):
    alias_paper_id: str
    canonical_paper_id: str
    display_label: str | None
    source: str
    confidence: float
    reasons: list[str]


class PaperDedupSnapshot(BaseModel):
    suggestions: list[PaperPairSuggestion]
    applied: list[PaperDedupAlias]
    rejected: list[PaperDedupAlias]
    components: list[PaperDedupComponent] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_alias_row(r: PaperAlias) -> PaperDedupAlias:
    return PaperDedupAlias(
        alias_paper_id=r.alias_paper_id,
        canonical_paper_id=r.canonical_paper_id,
        display_label=r.display_label,
        source=r.source,
        confidence=r.confidence,
        reasons=list(r.reasons or []),
    )


def _drop_cached_pair(a: str, b: str) -> None:
    pair = tuple(sorted((a, b)))
    with _suggestion_lock:
        globals()["_cached_suggestions"] = [
            s for s in _cached_suggestions
            if tuple(sorted((s["a"], s["b"]))) != pair
        ]


def _rescore(session: Session) -> list[dict[str, Any]]:
    """Run scoring (read-only) and cache suggestions + components."""
    result = dedup.run_dedup(session, auto_apply=False)
    with _suggestion_lock:
        globals()["_cached_suggestions"] = list(result.suggestions)
        globals()["_cached_components"] = list(result.components)
        globals()["_cached_at"] = datetime.now(UTC)
    return list(result.suggestions)


def _gather_snapshot(
    session: Session, *, rescore: bool = False
) -> PaperDedupSnapshot:
    """Build the panel payload.

    ``GET /paper-dedup/suggestions`` must be instant. Scoring is purely
    deterministic (no network), but can still touch the DB for every
    candidate pair, so we cache the result of the last scoring pass. The
    first GET before any scan returns an empty list; the first POST /run
    primes the cache, after which every GET serves cached data and merge /
    reject / undo update the cache in place.
    """
    applied = [
        _to_alias_row(r)
        for r in session.exec(
            select(PaperAlias).where(PaperAlias.source != "reject")
        ).all()
    ]
    rejected = [
        _to_alias_row(r)
        for r in session.exec(
            select(PaperAlias).where(PaperAlias.source == "reject")
        ).all()
    ]

    if rescore:
        raw = _rescore(session)
        components = _cached_components
    else:
        with _suggestion_lock:
            raw = list(_cached_suggestions)
            components = list(_cached_components)

    # Drop any pair that's been merged or rejected so the panel never shows
    # an action already taken.
    applied_pairs = {
        tuple(sorted((row.alias_paper_id, row.canonical_paper_id)))
        for row in applied
    }
    rejected_pairs = {
        tuple(sorted((row.alias_paper_id, row.canonical_paper_id)))
        for row in rejected
    }

    def _is_open(s: dict[str, Any]) -> bool:
        pair = tuple(sorted((s["a"], s["b"])))
        return pair not in applied_pairs and pair not in rejected_pairs

    suggestions = [PaperPairSuggestion(**s) for s in raw if _is_open(s)]
    return PaperDedupSnapshot(
        suggestions=suggestions,
        applied=applied,
        rejected=rejected,
        components=[PaperDedupComponent(**c) for c in components],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/suggestions", response_model=PaperDedupSnapshot)
def list_suggestions(
    session: Session = Depends(get_session_dep),
) -> PaperDedupSnapshot:
    return _gather_snapshot(session)


@router.post("/run", response_model=JobOut)
def run_dedup_job(
    body: PaperDedupRunRequest,
    bg: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> JobOut:
    """Run the paper dedup scoring pass as a Job.

    With ``auto_apply=True`` (default) any high-confidence pair is merged
    immediately; the response only contains a summary. The full snapshot
    (suggestions, applied, rejected) is read via
    ``GET /paper-dedup/suggestions`` after the job completes.
    """
    now = datetime.now(UTC)
    job = Job(
        kind=JobKind.paper_dedup.value,
        status=JobStatus.queued.value,
        message="Queued — paper dedup",
        stats={"stage": "queued", "auto_apply": body.auto_apply},
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

            # M10.6 — build the composite judge (deterministic + LLM) so
            # borderline pairs that lack a strong anchor get a real verdict
            # instead of staying in the suggestion queue. The LLM call is
            # budgeted per run via cfg.llm.paper_dedup_judge_max_calls_per_run
            # so a large borderline queue can't run the meter.
            from carrel.config import load_settings  # local import — heavy

            cfg, _ = load_settings()
            j_judge = judge.build_judge(sess, cfg.llm)

            result = dedup.run_dedup(
                sess,
                auto_apply=body.auto_apply,
                on_progress=_progress,
                judge=j_judge,
            )
            # Refresh the suggestion cache from the just-scored pass so the
            # panel opens instantly and merge / reject actions below mutate
            # this snapshot rather than paying for another rescore.
            with _suggestion_lock:
                globals()["_cached_suggestions"] = list(result.suggestions)
                globals()["_cached_components"] = list(result.components)
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
            logger.exception("paper dedup job %d crashed", job_id)
            jj = sess.get(Job, job_id)
            if jj is not None:
                jj.status = JobStatus.failed.value
                jj.finished_at = datetime.now(UTC)
                jj.message = f"{type(e).__name__}: {e}"[:200]
                sess.add(jj)
                sess.commit()

    def _run_bg() -> None:
        from sqlmodel import Session as SqlSession

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


@router.post("/merge", response_model=PaperDedupAlias)
def merge_pair(
    body: PaperMergeRequest,
    session: Session = Depends(get_session_dep),
) -> PaperDedupAlias:
    """User accepts a suggestion: merge the alias paper into the canonical.

    Migrates the loser's user_state (favorite / notes / tldr / keywords /
    tags / topics / chat / chunks / wiki_sources / citation lists) to the
    canonical. Loser is marked status=merged; ``resolve_paper_id`` will
    route the alias id back to the canonical.
    """
    if not body.alias_paper_id or not body.canonical_paper_id:
        raise HTTPException(
            status_code=422, detail="alias_paper_id and canonical_paper_id required"
        )
    if body.alias_paper_id == body.canonical_paper_id:
        raise HTTPException(
            status_code=422, detail="cannot merge a paper into itself"
        )
    try:
        row = ops.apply_merge(
            session,
            alias_paper_id=body.alias_paper_id,
            canonical_paper_id=body.canonical_paper_id,
            source="user",
            confidence=1.0,
            display_label=body.display_label,
        )
    except ops.PaperMergeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    session.commit()
    _drop_cached_pair(body.alias_paper_id, body.canonical_paper_id)
    # Drop the entire cache: the next rescore should recompute from the
    # post-merge data so the panel sees current components.
    with _suggestion_lock:
        globals()["_cached_suggestions"] = []
        globals()["_cached_components"] = []
        globals()["_cached_at"] = None
    return _to_alias_row(row)


@router.post("/reject", response_model=PaperDedupAlias)
def reject_pair(
    body: PaperRejectRequest,
    session: Session = Depends(get_session_dep),
) -> PaperDedupAlias:
    """User marks two papers as different — suppresses future auto-merges.

    Removes any prior auto / user / llm alias between the pair so the reject
    wins. The reject is invisible to :func:`resolve_paper_id` (it suppresses
    rather than redirects), so the two papers stay independent.
    """
    if not body.a or not body.b or body.a == body.b:
        raise HTTPException(status_code=422, detail="two distinct paper ids required")
    try:
        row = ops.apply_reject(
            session,
            a=body.a,
            b=body.b,
            display_label=body.display_label,
        )
    except ops.PaperMergeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    session.commit()
    _drop_cached_pair(body.a, body.b)
    return _to_alias_row(row)


@router.delete("/aliases/{alias_paper_id}/{canonical_paper_id}", response_model=dict)
def delete_alias(
    alias_paper_id: str,
    canonical_paper_id: str,
    session: Session = Depends(get_session_dep),
) -> dict:
    """Remove an alias (or rejection) so the pair becomes independent again.

    Loser status is un-flagged (status=ready) so it becomes a normal paper
    again. The user_state that was migrated to the canonical is *not* put
    back; ``PaperMergeEvent`` carries the pre-migration snapshot for offline
    recovery.
    """
    deleted = ops.undo_alias(
        session,
        alias_paper_id=alias_paper_id,
        canonical_paper_id=canonical_paper_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="alias not found")
    session.commit()
    with _suggestion_lock:
        globals()["_cached_suggestions"] = []
        globals()["_cached_components"] = []
        globals()["_cached_at"] = None
    return {"deleted": True}


@router.post("/judge", response_model=PaperJudgeResponse)
def judge_pair(
    body: PaperJudgeRequest,
    session: Session = Depends(get_session_dep),
) -> PaperJudgeResponse:
    """On-demand LLM judge for a single paper pair.

    Uses the configured composite judge (deterministic + LLM with cache).
    For a strong-anchor pair the LLM is bypassed entirely and the response
    is instant. For borderline pairs this calls the LLM; the verdict is
    cached in ``paper_dedup_verdicts`` keyed on (sorted pair + content hash)
    so a follow-up call with the same papers + same prompt_version is free.
    """
    if not body.a or not body.b or body.a == body.b:
        raise HTTPException(status_code=422, detail="two distinct paper ids required")
    a = session.get(Paper, body.a)
    b = session.get(Paper, body.b)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="paper not found")

    from carrel.config import load_settings  # local import — heavy
    from sqlmodel import select as _sel

    cfg, _ = load_settings()
    j = judge.build_judge(session, cfg.llm)

    # Detect a cache hit cheaply so the UI can show "(cached)" in the result
    # banner. The lookup uses the same (sorted a/b, model, prompt_version)
    # key the LLMJudge uses internally.
    from carrel.models import PaperDedupVerdict

    a_key, b_key = sorted((a.id, b.id))
    model_id = (
        cfg.llm.paper_dedup_judge_model
        or cfg.llm.chat_model
        or cfg.llm.summarize_model
    )
    pre = session.exec(
        _sel(PaperDedupVerdict).where(
            PaperDedupVerdict.paper_a_id == a_key,
            PaperDedupVerdict.paper_b_id == b_key,
            PaperDedupVerdict.model == model_id,
            PaperDedupVerdict.prompt_version == cfg.llm.paper_dedup_judge_prompt_version,
        )
    ).first()
    cached = pre is not None

    v = j.judge(a, b)
    return PaperJudgeResponse(
        a=a.id,
        b=b.id,
        verdict=v.verdict,
        confidence=v.confidence,
        reasons=list(v.reasons),
        model=v.model,
        prompt_version=v.prompt_version,
        cached=cached,
    )
