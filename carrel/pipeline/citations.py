"""Citation enrichment pipeline (Semantic Scholar + OpenAlex).

For each paper we fetch the citation count, influential/reference counts, and a
capped list of citing papers from S2, then merge in any additional citing
papers OpenAlex knows about that S2 missed. The merged list is deduped by
DOI / arXiv id / S2 paper id / OpenAlex id / normalized title. This module is
synchronous and mirrors :mod:`carrel.pipeline.process`:

  - :func:`enrich_paper` enriches one paper and reports progress via a callback
    shaped like process.py's (a dict with ``stage`` / ``detail``).
  - :func:`enrich_papers` walks a list; request pacing is handled globally by
    the S2 client's rate limiter.
  - :func:`select_stale` picks papers never enriched (for a first backfill).

Failures are soft: a network/rate-limit error is logged and re-raised so the
per-paper Job can be marked failed, but callers that batch many papers (sync)
catch and continue so one bad lookup never aborts a sync run.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import String, cast
from sqlmodel import Session, or_, select

from carrel.config import CarrelYAML
from carrel.models import Paper
from carrel.sources import openalex_client as oa
from carrel.sources import semanticscholar_client as s2

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]

# OpenAlex paginates at 200/page; a single cites: query is bounded by that.
_OPENALEX_CITES_LIMIT = 200


def _norm_title(t: str | None) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def _openalex_to_citing(work: dict) -> dict:
    """Normalize an OpenAlex Work dict to the {title,year,doi,arxiv_id,...} shape
    stored on Paper.citing_papers."""
    return {
        "title": (work.get("title") or work.get("display_name") or "").strip() or None,
        "year": work.get("publication_year"),
        "venue": oa.work_venue(work),
        "doi": oa.work_doi(work),
        "arxiv_id": oa.work_arxiv_id(work),
        "s2_paper_id": None,
        "openalex_id": oa.work_id(work) or None,
    }


def _merge_citing(s2_list: list[dict], oa_list: list[dict]) -> list[dict]:
    """Merge S2 and OpenAlex citing-paper dicts, dedup by (doi, arxiv_id, s2_id,
    openalex_id, normalized title). S2 entries win on conflict (richer fields);
    when an S2 entry is missing venue but the OA match has one, backfill it."""
    out: list[dict] = []
    seen_doi: set[str] = set()
    seen_arxiv: set[str] = set()
    seen_s2: set[str] = set()
    seen_oa: set[str] = set()
    seen_title: set[str] = set()

    def _dup(d: dict) -> bool:
        doi = (d.get("doi") or "").lower() or None
        arxiv = d.get("arxiv_id") or None
        s2id = d.get("s2_paper_id") or None
        oaid = d.get("openalex_id") or None
        title = _norm_title(d.get("title"))
        if (doi and doi in seen_doi) or (arxiv and arxiv in seen_arxiv) or \
           (s2id and s2id in seen_s2) or (oaid and oaid in seen_oa) or \
           (title and title in seen_title):
            return True
        if doi: seen_doi.add(doi)
        if arxiv: seen_arxiv.add(arxiv)
        if s2id: seen_s2.add(s2id)
        if oaid: seen_oa.add(oaid)
        if title: seen_title.add(title)
        return False

    def _find_existing(d: dict) -> dict | None:
        doi = (d.get("doi") or "").lower() or None
        arxiv = d.get("arxiv_id") or None
        oaid = d.get("openalex_id") or None
        title = _norm_title(d.get("title"))
        for existing in out:
            if (doi and (existing.get("doi") or "").lower() == doi) or \
               (arxiv and existing.get("arxiv_id") == arxiv) or \
               (oaid and existing.get("openalex_id") == oaid) or \
               (title and _norm_title(existing.get("title")) == title):
                return existing
        return None

    for d in s2_list:
        if not _dup(d):
            out.append(dict(d))
    for d in oa_list:
        match = _find_existing(d)
        if match is not None:
            if not match.get("venue") and d.get("venue"):
                match["venue"] = d["venue"]
        elif not _dup(d):
            out.append(dict(d))
    return out


def _dedup_simple(items: list[dict]) -> list[dict]:
    """Drop duplicate reference rows by doi / arxiv / s2 id / normalized title."""
    out: list[dict] = []
    seen_doi: set[str] = set()
    seen_arxiv: set[str] = set()
    seen_s2: set[str] = set()
    seen_title: set[str] = set()
    for d in items:
        doi = (d.get("doi") or "").lower() or None
        arxiv = d.get("arxiv_id") or None
        s2id = d.get("s2_paper_id") or None
        title = _norm_title(d.get("title"))
        if doi and doi in seen_doi:
            continue
        if arxiv and arxiv in seen_arxiv:
            continue
        if s2id and s2id in seen_s2:
            continue
        if title and title in seen_title:
            continue
        if doi:
            seen_doi.add(doi)
        if arxiv:
            seen_arxiv.add(arxiv)
        if s2id:
            seen_s2.add(s2id)
        if title:
            seen_title.add(title)
        out.append(d)
    return out


def _openalex_identifier(paper: Paper) -> str | None:
    """Pick the best identifier for the OpenAlex `cites:` filter.

    OpenAlex prefers W-ids; for arxiv-only papers we fall back to the arXiv id
    since OpenAlex's `cites` filter accepts external ids.
    """
    if paper.id_kind == "openalex" or paper.id.startswith("W"):
        return paper.id
    if paper.doi:
        return oa.work_doi({"doi": paper.doi}) or paper.doi
    if paper.arxiv_id:
        return paper.arxiv_id
    return None


def enrich_paper(
    session: Session,
    cfg: CarrelYAML,
    paper_id: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> bool:
    """Look up one paper on S2 and persist its citation data.

    Returns ``True`` when citation data was written (found or explicitly
    empty), ``False`` when the paper has no resolvable identifier. A paper S2
    cannot find still gets its ``citations_updated_at`` stamped so we don't
    retry it on every sync.
    """
    paper = session.get(Paper, paper_id)
    if paper is None:
        return False

    def _progress(detail: str, **extra: object) -> None:
        if on_progress is not None:
            on_progress({"stage": "citations", "detail": detail, **extra})

    limit = cfg.semantic_scholar.citations_limit

    if not (paper.s2_paper_id or paper.doi or paper.arxiv_id):
        logger.info("paper %s has no DOI/arXiv/S2 id; skipping citations", paper.id)
        _progress("No identifier to look up", paper_id=paper.id)
        paper.citations_updated_at = datetime.now(UTC)
        session.add(paper)
        session.commit()
        return False

    _progress("Querying Semantic Scholar…", paper_id=paper.id)
    result = s2.fetch_citations(
        doi=paper.doi,
        arxiv_id=paper.arxiv_id,
        s2_id=paper.s2_paper_id,
        limit=limit,
    )

    # OpenAlex merge — best-effort, never blocks the S2 result from being saved.
    oa_works: list[dict] = []
    oa_id = _openalex_identifier(paper)
    if oa_id:
        _progress("Merging OpenAlex citing works…", paper_id=paper.id)
        try:
            oa_works = oa.fetch_citing_works(oa_id, limit=_OPENALEX_CITES_LIMIT)
        except Exception as e:  # noqa: BLE001
            logger.warning("openalex citing fetch failed for %s: %s", paper.id, e)
    oa_citing = [_openalex_to_citing(w) for w in oa_works]

    now = datetime.now(UTC)
    if result is None and not oa_citing:
        # Neither source has a record; stamp so we don't keep retrying.
        paper.citations_updated_at = now
        session.add(paper)
        session.commit()
        _progress("Not found on Semantic Scholar or OpenAlex", paper_id=paper.id)
        return False

    s2_list = result.citing_papers if result is not None else []
    merged = _merge_citing(s2_list, oa_citing)

    if result is not None:
        paper.s2_paper_id = result.s2_paper_id or paper.s2_paper_id
        # S2 count is authoritative when present; bump to OA page-count if higher.
        paper.citation_count = max(result.citation_count or 0, len(oa_citing)) or result.citation_count
        paper.influential_citation_count = result.influential_count
        paper.reference_count = result.reference_count
        # References only come from S2; no OpenAlex equivalent to merge.
        paper.references = _dedup_simple(result.referenced_papers)
    else:
        # No S2 result — fall back to OA page count as a lower bound.
        paper.citation_count = len(oa_citing)
    paper.citing_papers = merged
    paper.citations_updated_at = now
    session.add(paper)
    session.commit()

    total = paper.citation_count if paper.citation_count is not None else len(merged)
    _progress(f"{total} citations ({len(merged)} listed)", paper_id=paper.id)
    return True


def enrich_papers(
    session: Session,
    cfg: CarrelYAML,
    paper_ids: list[str],
    *,
    on_progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Enrich many papers, one at a time.

    Request pacing is handled globally by the S2 client's rate limiter (which
    spaces the three calls within each paper and between papers), so no extra
    delay is needed here. Returns counters ``{enriched, failed, skipped}``. A
    failure on one paper is logged and counted but does not stop the batch.
    """
    enriched = failed = skipped = 0
    total = len(paper_ids)

    for idx, pid in enumerate(paper_ids):
        if on_progress is not None:
            on_progress({
                "stage": "citations",
                "detail": f"Looking up citations ({idx + 1}/{total})…",
                "paper_id": pid,
            })
        try:
            if enrich_paper(session, cfg, pid, on_progress=on_progress):
                enriched += 1
            else:
                skipped += 1
        except Exception as e:  # noqa: BLE001 - batch must continue
            logger.warning("citation enrichment failed for %s: %s", pid, e)
            failed += 1

    return {"enriched": enriched, "failed": failed, "skipped": skipped}


def select_stale(session: Session, limit: int = 50) -> list[Paper]:
    """Return papers that have never had citations fetched (oldest first)."""
    stmt = (
        select(Paper)
        .where(
            Paper.in_library.is_(True),
            Paper.citations_updated_at.is_(None),
        )
        .order_by(Paper.created_at.asc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())


def select_missing_references(session: Session, limit: int = 50) -> list[Paper]:
    """Return enriched papers whose reference list was never backfilled.

    These are papers enriched before the references-list feature shipped: they
    carry a ``reference_count`` but ``references`` is still NULL (distinct from
    an empty list ``[]``, which means "fetched and genuinely none"). Only papers
    with a resolvable identifier are returned, matching :func:`enrich_paper`.
    """
    # "Never backfilled" = the column holds SQL NULL (Postgres) or the JSON
    # literal null (SQLite, whose JSON type serializes None to the text 'null').
    # An empty list '[]' means "fetched, genuinely none" and must NOT match.
    refs_null = or_(
        Paper.references.is_(None),
        cast(Paper.references, String) == "null",
    )
    stmt = (
        select(Paper)
        .where(
            Paper.in_library.is_(True),
            Paper.reference_count.is_not(None),
            Paper.reference_count > 0,
            refs_null,
            or_(
                Paper.s2_paper_id.is_not(None),
                Paper.doi.is_not(None),
                Paper.arxiv_id.is_not(None),
            ),
        )
        .order_by(Paper.citations_updated_at.asc())
        .limit(limit)
    )
    return list(session.exec(stmt).all())
