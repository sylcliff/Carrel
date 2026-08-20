"""Citation endpoints: list stored citations and refresh them on demand.

``GET  /papers/{paper_id}/citations`` returns the stored citing-paper list with
each item's library-membership resolved (so the UI can link to papers already
in Carrel).

``POST /papers/{paper_id}/refresh-citations`` kicks off a Semantic Scholar
lookup as one Job (kind ``citations``), mirroring the one-job-per-paper design
of :mod:`carrel.api.process`. The frontend polls the job the same way it polls
a parse job.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlmodel import Session, or_, select

from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus, Paper
from carrel.pipeline.citations import enrich_paper
from carrel.schemas import (
    CitationItem,
    CitationListOut,
    CitationRefreshRequest,
    JobOut,
    ReferenceListOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/papers", tags=["citations"])


def _to_out(job: Job) -> JobOut:
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


def _resolve_library(
    session: Session, citing: list[dict]
) -> dict[str, str]:
    """Build identifier -> Carrel Paper.id lookups by DOI / arXiv / S2 / OpenAlex id.

    One batched query keeps this cheap even for long citation lists. DOIs are
    stored as full URLs and compared case-insensitively against bare DOIs.
    """
    dois = {c["doi"].lower() for c in citing if c.get("doi")}
    arxiv_ids = {c["arxiv_id"] for c in citing if c.get("arxiv_id")}
    s2_ids = {c["s2_paper_id"] for c in citing if c.get("s2_paper_id")}
    oa_ids = {c["openalex_id"] for c in citing if c.get("openalex_id")}

    by_id: dict[str, str] = {}
    if not (dois or arxiv_ids or s2_ids or oa_ids):
        return by_id

    clauses = []
    if dois:
        doi_variants = list(dois) + [f"https://doi.org/{d}" for d in dois]
        clauses.append(Paper.doi.in_(doi_variants))
    if arxiv_ids:
        clauses.append(Paper.arxiv_id.in_(list(arxiv_ids)))
    if s2_ids:
        clauses.append(Paper.s2_paper_id.in_(list(s2_ids)))
    if oa_ids:
        # Carrel Paper.id is stored as the bare "W…" form; OpenAlex sometimes
        # hands us the full URL — match both.
        oa_variants = list(oa_ids) + [
            f"https://openalex.org/{oid}" for oid in oa_ids
        ]
        clauses.append(Paper.id.in_(oa_variants))

    for p in session.exec(select(Paper).where(or_(*clauses))).all():
        if p.doi:
            bare = p.doi.lower().removeprefix("https://doi.org/").removeprefix("http://doi.org/")
            by_id[f"doi:{bare}"] = p.id
        if p.arxiv_id:
            by_id[f"arxiv:{p.arxiv_id}"] = p.id
        if p.s2_paper_id:
            by_id[f"s2:{p.s2_paper_id}"] = p.id
        # Carrel paper ids always start with W; prefix with both styles so
        # the lookup works whether the citing entry carried the URL or the
        # bare id.
        if p.id.startswith("W"):
            by_id[f"oa:{p.id}"] = p.id
            by_id[f"oa:https://openalex.org/{p.id}"] = p.id
    return by_id


def _find_in_library(lib_map: dict[str, str], item: dict) -> str | None:
    if item.get("doi"):
        pid = lib_map.get(f"doi:{item['doi'].lower()}")
        if pid:
            return pid
    if item.get("arxiv_id"):
        pid = lib_map.get(f"arxiv:{item['arxiv_id']}")
        if pid:
            return pid
    if item.get("s2_paper_id"):
        pid = lib_map.get(f"s2:{item['s2_paper_id']}")
        if pid:
            return pid
    oa = item.get("openalex_id")
    if oa:
        bare = oa.rsplit("/", 1)[-1]  # strip https://openalex.org/ prefix
        pid = lib_map.get(f"oa:{bare}")
        if pid:
            return pid
    return None


@router.get("/{paper_id}/citations", response_model=CitationListOut)
def list_citations(
    paper_id: str,
    sort: str | None = None,
    offset: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session_dep),
) -> CitationListOut:
    """List citing papers with optional sort + pagination.

    - `sort` ∈ {`year_asc`, `year_desc`, `cited_desc`}; default = original
      merged order from the last enrichment.
    - `offset` / `limit` control pagination. The first page comes from the
      cached `paper.citing_papers` when the sort has a year key; once the
      user scrolls past the cache we fan out to OpenAlex live and the
      response carries `next_offset` for the caller to keep loading.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")

    if sort not in (None, "", "year_asc", "year_desc", "cited_desc"):
        raise HTTPException(status_code=400, detail=f"unknown sort: {sort}")
    sort = sort or ""
    limit = max(1, min(limit, 200))

    citing_raw = list(paper.citing_papers or [])
    cached_count = len(citing_raw)
    next_offset: int | None = None
    source = "cache"

    # Sort the cached list. `cited_desc` has no field on cached items, so
    # we fall back to year_desc — the user can keep scrolling to get the
    # real "most cited" ranking from OpenAlex.
    if sort == "year_asc":
        citing_raw.sort(key=lambda c: (c.get("year") or 9999, _norm_title(c.get("title"))))
    elif sort == "year_desc":
        citing_raw.sort(key=lambda c: -(c.get("year") or -9999))
    elif sort == "cited_desc":
        # Cache has no cited_by_count; sort the cached slice by year desc
        # as a best-effort preview, then live-fetch from OpenAlex from p2.
        citing_raw.sort(key=lambda c: -(c.get("year") or -9999))

    page: list[dict] = []
    if offset < cached_count:
        page = citing_raw[offset : offset + limit]
        if offset + limit < cached_count:
            next_offset = offset + limit
    # Beyond the cache: fetch from OpenAlex (live), respecting `sort`.
    if offset >= cached_count or (not page and offset > 0):
        page, source = _fetch_openalex_page(paper, sort=sort, offset=offset, limit=limit)
        if page:
            # `offset` here is an absolute index into the conceptual stream
            # (cache + OpenAlex); we report `next_offset` as the next absolute
            # position. The client just appends + scrolls.
            next_offset = offset + len(page)
        else:
            next_offset = None

    lib_map = _resolve_library(session, page)
    items: list[CitationItem] = []
    for c in page:
        pid = _find_in_library(lib_map, c)
        items.append(CitationItem(
            title=c.get("title"),
            year=c.get("year"),
            venue=c.get("venue"),
            doi=c.get("doi"),
            arxiv_id=c.get("arxiv_id"),
            s2_paper_id=c.get("s2_paper_id"),
            openalex_id=c.get("openalex_id"),
            in_library=pid is not None,
            paper_id=pid,
        ))

    from carrel.main import app_config

    return CitationListOut(
        paper_id=paper.id,
        citation_count=paper.citation_count,
        influential_citation_count=paper.influential_citation_count,
        reference_count=paper.reference_count,
        updated_at=paper.citations_updated_at,
        truncated=next_offset is not None,
        citing=items,
        next_offset=next_offset,
        source=source,
        cached_count=cached_count,
    )


def _norm_title(t: str | None) -> str:
    import re
    return re.sub(r"\s+", " ", (t or "").strip().lower())


@router.get("/{paper_id}/references", response_model=ReferenceListOut)
def list_references(
    paper_id: str,
    sort: str | None = None,
    session: Session = Depends(get_session_dep),
) -> ReferenceListOut:
    """List the papers this paper cites (its bibliography).

    Data comes from the cached `paper.references` populated by the citations
    refresh job. `sort` ∈ {`year_asc`, `year_desc`}; default preserves the
    Semantic Scholar order (roughly order of first appearance). Library
    membership is resolved per item so the UI can link / offer Import.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")

    if sort not in (None, "", "year_asc", "year_desc"):
        raise HTTPException(status_code=400, detail=f"unknown sort: {sort}")

    refs = list(paper.references or [])
    if sort == "year_asc":
        refs.sort(key=lambda c: (c.get("year") or 9999, _norm_title(c.get("title"))))
    elif sort == "year_desc":
        refs.sort(key=lambda c: -(c.get("year") or -9999))

    lib_map = _resolve_library(session, refs)
    items: list[CitationItem] = []
    for c in refs:
        pid = _find_in_library(lib_map, c)
        items.append(CitationItem(
            title=c.get("title"),
            year=c.get("year"),
            venue=c.get("venue"),
            doi=c.get("doi"),
            arxiv_id=c.get("arxiv_id"),
            s2_paper_id=c.get("s2_paper_id"),
            openalex_id=c.get("openalex_id"),
            in_library=pid is not None,
            paper_id=pid,
        ))

    return ReferenceListOut(
        paper_id=paper.id,
        reference_count=paper.reference_count,
        updated_at=paper.citations_updated_at,
        references=items,
    )


def _fetch_openalex_page(
    paper: Paper, *, sort: str, offset: int, limit: int,
) -> tuple[list[dict], str]:
    """Live-fetch a page of citing papers from OpenAlex with the given sort.

    `offset` here is the absolute position past the cached stream, so we map
    it to OpenAlex's 1-indexed page (capped at 200/page).
    """
    from carrel.sources import openalex_client as oa

    if sort not in ("year_asc", "year_desc", "cited_desc"):
        return [], "openalex"
    if paper.id_kind == "openalex" or paper.id.startswith("W"):
        oa_id = paper.id
    elif paper.doi:
        oa_id = oa.work_doi({"doi": paper.doi}) or paper.doi
    elif paper.arxiv_id:
        oa_id = paper.arxiv_id
    else:
        return [], "openalex"

    # Map absolute offset → OA page. Cache size isn't known here; we just step
    # in OA 200/page chunks starting at page 1 once `offset` exceeds cache.
    # Caller passes `offset >= cached_count`, so OA page = (offset - cache)/200 + 1.
    # We don't have cached_count here, so the caller (list_citations) only
    # invokes us with offsets past the cache. Page = (offset - cached_count) // 200 + 1.
    # The actual `offset - cached_count` value is approximated by treating the
    # difference as `offset` in the live stream — but we don't know cached
    # length. So we just compute from offset (caller's call site only fires
    # when offset > cached, so OA's effective page index is floor(offset/200)+1).
    page_num = (offset // 200) + 1
    sort_kwargs: dict = {}
    if sort == "year_asc":
        sort_kwargs["publication_date"] = "asc"
    elif sort == "year_desc":
        sort_kwargs["publication_date"] = "desc"
    elif sort == "cited_desc":
        sort_kwargs["cited_by_count"] = "desc"

    try:
        results = (
            oa.Works()
            .filter(cites=oa_id)
            .sort(**sort_kwargs)
            .get(per_page=min(limit, 200), page=page_num)
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("openalex cites page fetch failed: %s", e)
        return [], "openalex"

    out: list[dict] = []
    for w in results:
        out.append({
            "title": (w.get("title") or "").strip() or None,
            "year": w.get("publication_year"),
            "venue": oa.work_venue(w),
            "doi": oa.work_doi(w),
            "arxiv_id": oa.work_arxiv_id(w),
            "s2_paper_id": None,
            "openalex_id": oa.work_id(w) or None,
        })
    return out, "openalex"


@router.post("/{paper_id}/refresh-citations", response_model=JobOut)
def refresh_citations(
    paper_id: str,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
    body: CitationRefreshRequest = CitationRefreshRequest(),
) -> JobOut:
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="paper not found")

    now = datetime.now(UTC)
    job = Job(
        kind=JobKind.citations.value,
        status=JobStatus.queued.value,
        message=f"Queued — {paper.title[:80]}",
        stats={
            "paper_id": paper.id,
            "paper_title": paper.title,
            "stage": "queued",
            "detail": "Queued…",
        },
        created_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    assert job.id is not None

    if body.background and background is not None:
        background.add_task(_run_background, job.id, paper.id)
    else:
        _run(session, job.id, paper.id)
        session.refresh(job)

    return _to_out(job)


def _make_progress_cb(session: Session, job_id: int):
    def _cb(progress: dict) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        detail = progress.get("detail", "")
        title = progress.get("paper_title") or progress.get("title") or ""
        stats = {**(job.stats or {})}
        for key in ("stage", "detail", "paper_id", "paper_title"):
            if key in progress:
                stats[key] = progress[key]
        job.stats = stats
        job.message = f"{title} — {detail}" if (title and detail) else (detail or title or job.message)
        session.add(job)
        session.commit()
    return _cb


def _run(session: Session, job_id: int, paper_id: str) -> None:
    from carrel.main import app_config

    job = session.get(Job, job_id)
    paper = session.get(Paper, paper_id)
    base_cb = _make_progress_cb(session, job_id)
    title = paper.title if paper is not None else ""

    def progress(p: dict) -> None:
        base_cb({**p, "paper_id": paper_id, "paper_title": p.get("paper_title") or title})

    try:
        if job is not None:
            job.status = JobStatus.running.value
            job.started_at = datetime.now(UTC)
            session.add(job)
            session.commit()

        enrich_paper(session, app_config, paper_id, on_progress=progress)

        if job is not None:
            job.status = JobStatus.done.value
            job.finished_at = datetime.now(UTC)
            job.stats = {**(job.stats or {}), "stage": "done", "detail": "Done."}
            job.message = "Done"
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("citations job %d failed", job_id)
        if job is not None:
            job.status = JobStatus.failed.value
            job.finished_at = datetime.now(UTC)
            job.message = f"{type(e).__name__}: {e}"[:200]
            session.add(job)
            session.commit()


def _run_background(job_id: int, paper_id: str) -> None:
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine

    engine = get_app_engine()
    with SqlSession(engine) as session:
        _run(session, job_id, paper_id)
