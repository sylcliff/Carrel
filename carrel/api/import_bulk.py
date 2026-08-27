"""Bulk import endpoint (M-extension): upsert N papers in one call.

The single-paper ``POST /import`` is fine for one-off imports, but the
common flow is "I just searched and got 200 candidates — I want all of
them in the library now". Forcing the user to click 200 import buttons
(or fire 200 requests in parallel from the frontend) is the worst part
of the literature-acquisition UX.

``POST /import/bulk`` accepts a list of identifier dicts (same shape as
``ImportPaperIn``) and processes them one at a time through the existing
``_resolve_work_for_import`` → ``_import_one_paper`` chain. A single
:class:`Job` wraps the whole batch so the ``jobs`` table doesn't get
flooded (1000 paper imports = 1 job, not 1000).

Failure handling mirrors the ``/search`` "soft-fail" model: per-item
errors are captured in the response and the job's ``stats.errors`` list
(capped at 50 to keep the JSON column small); the batch never aborts
because one item couldn't resolve.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlmodel import Session

from carrel.api.search import _import_one_paper, _resolve_work_for_import
from carrel.db import get_session_dep
from carrel.models import Job, JobKind, JobStatus
from carrel.schemas import (
    BulkImportIn,
    BulkImportItem,
    BulkImportItemOut,
    BulkImportOut,
)
from carrel.sources import openalex_client as oa
from carrel.sources.normalize import is_zenodo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["import"])

# Cap the per-job error list in stats so a 1000-paper batch with all
# failures still produces a small JSON payload.
_MAX_ERRORS_IN_STATS = 50


def _new_stats(total: int) -> dict[str, Any]:
    return {
        "total": total,
        "succeeded": 0,
        "created": 0,
        "updated": 0,
        "failed": 0,
        "current": None,
        "errors": [],
    }


# Fast-path trigger: when these three are all present we have enough inline
# metadata to skip the OpenAlex / S2 round-trip and upsert straight to the
# DB. ``source`` drives the upsert path in ``_import_one_paper``; ``title``
# is mandatory on the Paper row; ``authors`` lets us populate the JSON column
# without an OA authorship fetch.
_FAST_PATH_KEYS = ("source", "title", "authors")


def _has_inline_metadata(item: BulkImportItem) -> bool:
    """True when ``item`` carries enough metadata to skip the resolver."""
    return all(
        getattr(item, k) is not None and getattr(item, k) != ""
        for k in _FAST_PATH_KEYS
    )


def _search_result_to_work(item: BulkImportItem) -> tuple[dict, str]:
    """Build an OpenAlex Work-shaped dict from inline metadata on a bulk item.

    Used by the fast path: the Search page already had to fetch the work
    once to render results, so re-fetching it from OpenAlex just to upsert
    the same data is wasted work. This synthesises a dict shaped exactly
    like the one :mod:`carrel.sources.openalex_client` returns, so the
    existing :func:`_import_one_paper` upsert logic runs untouched.

    Returns ``(work, source)`` where ``source`` is the value the caller
    should pass to ``_import_one_paper``. ``source="openalex"`` is the
    common case (search results from OA); ``"semantic_scholar"`` is used
    when the item has an S2 id but no OA id.

    Limitations vs. a real OpenAlex fetch:
    - No ``abstract_inverted_index`` — only a flat abstract if the
      search result carried one. S2's TLDR is the more common surrogate
      anyway.
    - Author affiliations / OpenAlex author ids are unknown. The author
      list is plain ``{name}`` dicts; the backfill-authors pipeline can
      enrich them later.
    - No PDF candidate ranking — we trust whatever ``pdf_url`` the
      search result gave us. The download-time fallback in
      :func:`carrel.api.process._pdf_candidates` heals mislabeled ones.
    """
    source = item.source or "openalex"
    authorships = [
        {
            "author": {"display_name": name, "id": None},
            "institutions": [],
        }
        for name in (item.authors or [])
        if isinstance(name, str) and name.strip()
    ]

    # Determine the canonical work id the upsert should use. For OA items
    # it's the W-id (or the full URL — ``oa.work_id`` strips the prefix).
    # For S2-only items the existing code stores ``"s2:" + paperId`` so
    # dedup-by-s2_id still works downstream.
    if source == "openalex":
        work_id = item.openalex_id or ""
    elif source == "semantic_scholar":
        work_id = f"s2:{item.s2}" if item.s2 else ""
    else:
        # arxiv-only or unknown — fall through to the resolver; we shouldn't
        # be here because ``_has_inline_metadata`` only triggers the fast
        # path for source ∈ {openalex, semantic_scholar}.
        work_id = item.openalex_id or (f"s2:{item.s2}" if item.s2 else "")

    work: dict[str, Any] = {
        "id": work_id,
        "title": item.title or "(untitled)",
        "doi": item.doi,
        "publication_date": item.publication_date,
        "publication_year": None,
        "cited_by_count": item.citation_count,
        "primary_location": (
            {"source": {"display_name": item.venue}} if item.venue else None
        ),
        "authorships": authorships,
        # S2's upsert path reads ``work["authors"]`` as ``[{"name": "..."}]``
        # rather than the OA-shaped ``authorships`` list. Populate it so
        # the S2 fast path produces a non-empty authors column. For OA
        # items the OA upsert path uses ``authorships`` and ignores this.
        "authors": [{"name": name} for name in (item.authors or []) if name],
        # Search results carry the flat abstract when OA / S2 had one; the
        # import path accepts both shapes. Don't set abstract_inverted_index
        # — we'd have to fabricate it.
        "abstract": item.abstract,
        "open_access": {"is_oa": bool(item.pdf_url)},
        "ids": {"arxiv": item.arxiv_id} if item.arxiv_id else {},
        # Mirror the arxiv id at the top level too. The S2 import branch in
        # ``_import_from_s2`` only reads ``work["arxiv_id"]``; without this
        # mirror, S2-tagged fast-path imports lose the arxiv id from the
        # column even though it survives in ``raw_meta.ids.arxiv``.
        "arxiv_id": item.arxiv_id,
        # Marker the S2 path uses to branch; harmless when source=openalex.
        "s2_paper_id": item.s2 if source == "semantic_scholar" else None,
    }
    return work, source


def _process_one(
    session: Session, item: BulkImportItem
) -> BulkImportItemOut:
    """Resolve + upsert one paper, returning a per-item outcome.

    Fast path: when the item carries inline metadata (``source`` + ``title``
    + ``authors``), build a Work-shaped dict directly and skip the per-item
    OpenAlex / S2 round-trip. This is the common case for the Search page,
    which already fetched the data to render results.

    Slow path: when only identifiers are present, fall back to
    :func:`_resolve_work_for_import` — same chain ``POST /import`` uses.
    This is the path CLI / curl callers take.

    All exceptions on either path are caught and surfaced as
    ``status="error"`` so the batch never aborts on a single bad item
    (matches the soft-fail model of ``POST /search``'s ``warnings``).
    """
    try:
        if _has_inline_metadata(item):
            # Fast path — no HTTP. The frontend already paid for the
            # metadata once during search; re-fetching is wasted work.
            work, source = _search_result_to_work(item)
            display_title = work["title"]
        else:
            resolved = _resolve_work_for_import(
                oa_id=item.openalex_id,
                doi=item.doi,
                arxiv_id=item.arxiv_id,
                s2_id=item.s2,
                title=item.title,
                session=session,
            )
            if not resolved:
                return BulkImportItemOut(
                    id=None,
                    title=item.title,
                    created=False,
                    status="error",
                    error="not found on OpenAlex or Semantic Scholar",
                )
            work, source = resolved
            display_title = oa.work_title(work) or item.title

        if is_zenodo(oa.work_doi(work), oa.work_venue(work)):
            return BulkImportItemOut(
                id=None,
                title=display_title,
                created=False,
                status="error",
                error="Zenodo deposits cannot be imported",
            )
        out = _import_one_paper(session, work, source)
        return BulkImportItemOut(
            id=out.id,
            title=display_title,
            created=out.created,
            status="ok",
        )
    except Exception as e:  # noqa: BLE001
        # Network blip, OA 502, anything. Log + return per-item error so the
        # batch keeps moving.
        logger.warning(
            "bulk import item failed (oa=%s doi=%s arxiv=%s s2=%s): %s",
            item.openalex_id,
            item.doi,
            item.arxiv_id,
            item.s2,
            e,
        )
        return BulkImportItemOut(
            id=None,
            title=item.title,
            created=False,
            status="error",
            error=f"{type(e).__name__}: {e}"[:200],
        )


def _record_item(job: Job, item_out: BulkImportItemOut, index: int) -> None:
    """Update job.stats with one item's outcome. Caller commits."""
    stats = {**(job.stats or {})}
    if item_out.status == "ok":
        stats["succeeded"] = stats.get("succeeded", 0) + 1
        if item_out.created:
            stats["created"] = stats.get("created", 0) + 1
        else:
            stats["updated"] = stats.get("updated", 0) + 1
    else:
        stats["failed"] = stats.get("failed", 0) + 1
        errors = list(stats.get("errors", []))
        if len(errors) < _MAX_ERRORS_IN_STATS:
            errors.append({"index": index, "error": item_out.error})
        stats["errors"] = errors
    stats["current"] = item_out.title or item_out.id
    job.stats = stats


def _drive_batch(
    session: Session, job_id: int, items: list[BulkImportItem]
) -> list[BulkImportItemOut]:
    """Run the batch serially against ``session``; mutate job.stats as we go.

    Used by both the inline path and the background path. Per-item
    commits keep ``job.stats`` live (so a frontend poll sees real-time
    progress) and bound the transaction size.

    The job's ``status`` moves ``queued → running → done/failed`` exactly
    like :mod:`carrel.api.sync`.
    """
    job = session.get(Job, job_id)
    if job is None:
        return []
    if job.status == JobStatus.queued.value:
        job.status = JobStatus.running.value
        job.started_at = datetime.now(UTC)
        session.add(job)
        session.commit()

    results: list[BulkImportItemOut] = []
    try:
        for i, item in enumerate(items):
            item_out = _process_one(session, item)
            results.append(item_out)
            _record_item(job, item_out, i)
            session.add(job)
            session.commit()
    except Exception as e:  # noqa: BLE001
        # Catastrophic batch failure (DB down, etc). Mark job failed; partial
        # results so far are already committed and visible in stats.
        logger.exception("bulk import job %d crashed at item %d", job_id, len(results))
        job.status = JobStatus.failed.value
        job.finished_at = datetime.now(UTC)
        job.message = f"{type(e).__name__}: {e}"[:200]
        session.add(job)
        session.commit()
        return results

    job.status = JobStatus.done.value
    job.finished_at = datetime.now(UTC)
    succeeded = (job.stats or {}).get("succeeded", 0)
    failed = (job.stats or {}).get("failed", 0)
    job.message = f"Imported {succeeded}/{len(items)}" + (
        f" ({failed} failed)" if failed else ""
    )
    session.add(job)
    session.commit()
    return results


@router.post("/import/bulk", response_model=BulkImportOut)
def import_bulk(
    body: BulkImportIn,
    background: BackgroundTasks,
    session: Session = Depends(get_session_dep),
) -> BulkImportOut:
    """Upsert N papers in one call. See :class:`BulkImportIn` for semantics.

    Always creates one :class:`Job` (kind ``import_bulk``) wrapping the
    whole batch. ``background=true`` (default) returns immediately with
    just ``job_id``; the worker runs in a ``BackgroundTask``. ``background=
    false`` blocks until done and returns the per-item results inline
    (handy for 1-20 items selected from a search).
    """
    now = datetime.now(UTC)
    job = Job(
        kind=JobKind.import_bulk.value,
        status=JobStatus.queued.value,
        message=f"Queued — importing {len(body.items)} papers",
        stats=_new_stats(len(body.items)),
        created_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    assert job.id is not None

    if body.background:
        # Items get serialised into BackgroundTasks by JSON; the worker
        # re-hydrates through ``ImportPaperIn.model_validate`` to be safe.
        items_payload = [it.model_dump() for it in body.items]
        background.add_task(_run_bulk_background, job.id, items_payload)
        return BulkImportOut(job_id=job.id, items=None)

    results = _drive_batch(session, job.id, body.items)
    return BulkImportOut(job_id=job.id, items=results)


def _run_bulk_background(job_id: int, items_payload: list[dict[str, Any]]) -> None:
    """BackgroundTasks entrypoint: open a fresh session, drive the batch."""
    from sqlmodel import Session as SqlSession

    from carrel.db import get_app_engine

    engine = get_app_engine()
    items = [BulkImportItem.model_validate(it) for it in items_payload]
    with SqlSession(engine) as session:
        _drive_batch(session, job_id, items)
