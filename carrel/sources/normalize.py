"""Normalize source-specific shapes into a uniform PaperRecord.

The pipeline receives records from two sources:
  - arXiv Atom entries (from sources.arxiv.fetch_recent)
  - OpenAlex Work dicts (from sources.openalex_client.fetch_recent_*)

Each is reduced to a PaperRecord (the in-process shape the rest of the
pipeline understands), then upserted into the papers table.

Identity is anchored on the OpenAlex Work ID when available; we fall back
to `arxiv:<id>` so the same paper is never inserted twice.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlmodel import Session

from carrel.sources import openalex_client as oa
from carrel.sources.arxiv import ArxivEntry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PaperRecord:
    """A single paper ready to upsert into the DB."""

    id: str  # OpenAlex Work ID (W...) or "arxiv:<id>"
    id_kind: str  # "openalex" | "arxiv"
    title: str
    abstract: str | None
    publication_date: date | None
    venue: str | None
    authors: list[dict[str, Any]]
    doi: str | None
    arxiv_id: str | None
    pdf_url: str | None
    oa_status: str  # "oa" | "closed" | "none"
    source: str  # "arxiv" | "openalex" | "both"
    raw_meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ArxivEntry -> PaperRecord (best-effort, no OpenAlex)
# ---------------------------------------------------------------------------


def from_arxiv(entry: ArxivEntry) -> PaperRecord:
    pub_date: date | None = None
    try:
        from datetime import datetime

        pub_date = datetime.fromisoformat(entry.updated.replace("Z", "+00:00")).date()
    except ValueError:
        pub_date = None

    # Strip version suffix (v1, v2, ...) from the ID for stable identity.
    bare_id = _strip_arxiv_version(entry.arxiv_id)

    return PaperRecord(
        id=f"arxiv:{bare_id}",
        id_kind="arxiv",
        title=entry.title,
        abstract=entry.summary or None,
        publication_date=pub_date,
        venue=None,
        authors=[{"name": a, "openalex_author_id": "", "affiliation": None} for a in entry.authors],
        doi=None,
        arxiv_id=bare_id,
        pdf_url=entry.pdf_url,
        oa_status="oa" if entry.pdf_url else "none",
        source="arxiv",
        raw_meta={
            "abs_url": entry.abs_url,
            "categories": entry.categories,
            "updated": entry.updated,
        },
    )


# ---------------------------------------------------------------------------
# OpenAlex Work -> PaperRecord
# ---------------------------------------------------------------------------


def from_openalex(work: dict[str, Any]) -> PaperRecord | None:
    """Normalize an OpenAlex Work, or return None if it should be skipped.

    Zenodo deposits are filtered out: they're software/dataset deposits, not
    papers, and their concept/version DOI pair used to create duplicate rows.
    """
    doi = oa.work_doi(work)
    venue = (oa.work_venue(work) or "").strip().lower()
    if is_zenodo(doi, venue):
        logger.debug(
            "skipping Zenodo work %s (doi=%s venue=%s)",
            oa.work_id(work), doi, oa.work_venue(work),
        )
        return None

    wid = oa.work_id(work)
    raw_arxiv = oa.work_arxiv_id(work)
    arxiv_id = _strip_arxiv_version(raw_arxiv) if raw_arxiv else None
    pdf_url, oa_status = oa.work_pdf_url(work)
    if wid:
        rec_id, id_kind = wid, "openalex"
    elif arxiv_id:
        rec_id, id_kind = f"arxiv:{arxiv_id}", "arxiv"
    else:
        rec_id, id_kind = "", "openalex"
    return PaperRecord(
        id=rec_id,
        id_kind=id_kind,
        title=oa.work_title(work),
        abstract=oa.work_abstract(work),
        publication_date=oa.work_publication_date(work),
        venue=oa.work_venue(work),
        authors=oa.work_authors(work),
        doi=doi,
        arxiv_id=arxiv_id,
        pdf_url=pdf_url,
        oa_status=oa_status,
        source="openalex",
        raw_meta=work,
    )


def is_zenodo(doi: str | None, venue: str | None) -> bool:
    """Return True for Zenodo deposits (software/datasets, not papers).

    Matches on the venue name ("Zenodo" or "Zenodo (...)") or a Zenodo DOI
    prefix (10.5281/zenodo.).
    """
    venue_lower = (venue or "").strip().lower()

    # OpenAlex returns "Zenodo" or "Zenodo (CERN European Organization for
    # Nuclear Research)" depending on the record; match either.
    if venue_lower == "zenodo" or venue_lower.startswith("zenodo "):
        return True
    if not doi:
        return False
    d = doi.strip().lower()
    # DOIs arrive as full URLs (https://doi.org/10.5281/zenodo.12345); also
    # tolerate the bare form.
    return "10.5281/zenodo." in d


# ---------------------------------------------------------------------------
# Promote: try to attach an OpenAlex Work to a bare arXiv record
# ---------------------------------------------------------------------------


def enrich_with_openalex(rec: PaperRecord, session: Session | None = None) -> PaperRecord:
    """If the record came from arXiv, try to attach an OpenAlex Work so the
    rest of the system has a canonical ID, authors with OA IDs, and a
    canonical PDF. ``work_pdf_url`` prefers repository/arXiv copies, since
    publisher ``pdf_url`` values sometimes serve HTML landing pages.

    When a SQLAlchemy ``session`` is provided the OpenAlex lookup goes
    through the persistent cache (see
    :func:`carrel.cache.openalex_works.lookup_work_by_arxiv_id`): the
    first miss hits OpenAlex and writes the result back to the
    ``work_by_arxiv_id`` table so the next caller — sync, the import
    path, or the publication check — short-circuits. Without a session
    the legacy direct call is used (kept for unit tests and any caller
    that hasn't been threaded through a session yet).
    """
    if rec.id_kind != "arxiv" or not rec.arxiv_id:
        return rec

    if session is not None:
        from carrel.cache import openalex_works as cache

        work = cache.lookup_work_by_arxiv_id(
            session, rec.arxiv_id, title_hint=rec.title
        )
    else:
        work = oa.lookup_by_arxiv_id(rec.arxiv_id)
    if not work:
        return rec

    # Prefer OpenAlex values when present; fall back to arXiv data.
    wid = oa.work_id(work)
    arxiv_id = _strip_arxiv_version(oa.work_arxiv_id(work) or rec.arxiv_id)
    pdf_url, oa_status = oa.work_pdf_url(work)
    # If OpenAlex has no direct PDF (closed or HTML-only) but arXiv does,
    # keep the arXiv PDF and mark the record oa — we have something to parse.
    if not pdf_url and rec.pdf_url:
        pdf_url = rec.pdf_url
        oa_status = "oa"

    return PaperRecord(
        id=wid or f"arxiv:{arxiv_id}",
        id_kind="openalex" if wid else "arxiv",
        title=oa.work_title(work) or rec.title,
        abstract=oa.work_abstract(work) or rec.abstract,
        publication_date=oa.work_publication_date(work) or rec.publication_date,
        venue=oa.work_venue(work) or rec.venue,
        authors=oa.work_authors(work) or rec.authors,
        doi=oa.work_doi(work) or rec.doi,
        arxiv_id=arxiv_id,
        pdf_url=pdf_url,
        oa_status=oa_status,
        source="both" if rec.source == "arxiv" else rec.source,
        raw_meta={**rec.raw_meta, "openalex": work},
    )


def _strip_arxiv_version(arxiv_id: str) -> str:
    """Strip trailing version (v1, v2) so identity is stable across revisions."""
    return re.sub(r"v\d+$", "", arxiv_id)


def format_journal_citation(raw_meta: dict | None) -> str | None:
    """Return a short bibliographic locator from a Work's ``biblio`` block.

    OpenAlex exposes volume / issue / first_page / last_page under
    ``work["biblio"]``; Semantic Scholar does not. We render a compact
    locator that fits next to a venue name without duplicating it:

      * ``"11(1), 147-174"``     — volume, issue, page range
      * ``"11(1)"``              — volume and issue only
      * ``"11, 147-174"``        — volume and page range
      * ``"11"``                 — volume only
      * ``"147-174"``            — page range only
      * ``None``                 — no biblio data (arXiv preprints, S2 papers)

    Empty strings and ``None`` fields are skipped; a single-page article
    collapses to ``"147"`` rather than ``"147-147"``.
    """
    if not raw_meta:
        return None
    biblio = raw_meta.get("biblio") or {}
    if not isinstance(biblio, dict):
        return None
    volume = biblio.get("volume") or None
    issue = biblio.get("issue") or None
    first = biblio.get("first_page") or None
    last = biblio.get("last_page") or None

    parts: list[str] = []
    if volume and issue:
        parts.append(f"{volume}({issue})")
    elif volume:
        parts.append(str(volume))

    if first and last and first != last:
        pages = f"{first}-{last}"
    elif first:
        pages = str(first)
    elif last:
        pages = str(last)
    else:
        pages = None
    if pages:
        parts.append(pages)

    return ", ".join(parts) or None



