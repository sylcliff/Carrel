"""Thin wrapper around pyalex (https://github.com/J535D165/pyalex, MIT).

Carrel's only reasons to use OpenAlex:
  1. Disambiguate authors and venues (canonical Author/Source IDs).
  2. Find an OA PDF for papers that have one (best_oa_location).
  3. Find new papers by author/venue/keyword in the last 24h.
  4. Look up an OpenAlex Work from a DOI or arXiv ID.
  5. Resolve an author / venue name to an OpenAlex ID (for the subscription
     editor UI later).

We deliberately do NOT pull full-text from OpenAlex — we read PDFs from the
canonical source (arXiv, publisher) and parse with MinerU. OpenAlex is the
metadata spine, not a content source.

Configuration: OpenAlex is free without an API key. Pass `mailto` to enter
the politeness pool (faster, more reliable). We read it from the YAML config.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import pyalex
from pyalex import Authors, Sources, Works

from carrel.config import CarrelYAML

logger = logging.getLogger(__name__)


def configure(cfg: CarrelYAML) -> None:
    """Idempotent. Set pyalex globals from our config; safe to call repeatedly."""
    pyalex.config.email = cfg.openalex.mailto or None
    pyalex.config.api_key = cfg.openalex.api_key or None
    pyalex.config.max_retries = cfg.openalex.max_retries
    pyalex.config.retry_backoff_factor = 0.5
    pyalex.config.retry_http_codes = [429, 500, 503]


# ---------------------------------------------------------------------------
# Work lookups
# ---------------------------------------------------------------------------


def lookup_by_arxiv_id(arxiv_id: str) -> dict[str, Any] | None:
    """Return the OpenAlex Work dict for an arXiv ID, or None.

    OpenAlex does not reliably index arXiv DOIs of the form 10.48550/arXiv.X.Y
    (returns 404 for many). The reliable approach is a multi-step search:
    try the DOI form first, then search by the arXiv ID as a token and pick
    the top hit that actually matches.
    """
    arxiv_id = arxiv_id.strip()
    if not arxiv_id:
        return None

    # 1. Direct DOI lookup
    doi = f"10.48550/arXiv.{arxiv_id}"
    try:
        w = Works()[doi]
        if w:
            return dict(w)
    except Exception as e:
        logger.debug("DOI lookup failed for %s: %s", arxiv_id, e)

    # 2. Search fallback — most arXiv papers are indexed by title string
    try:
        candidates = (
            Works()
            .search(arxiv_id)
            .filter(primary_location={"source": {"type": "repository"}})
            .get(per_page=10)
        )
        for w in candidates:
            d = dict(w)
            ids = d.get("ids") or {}
            arxiv_field = (ids.get("arxiv") or "").split("/")[-1]
            doi_field = (d.get("doi") or "").lower()
            if arxiv_field == arxiv_id or doi_field.endswith(arxiv_id.lower()):
                return d
        if candidates:
            return dict(candidates[0])
    except Exception as e:
        logger.debug("Search lookup failed for %s: %s", arxiv_id, e)

    return None


def lookup_by_doi(doi: str) -> dict[str, Any] | None:
    doi = (doi or "").strip()
    if not doi:
        return None
    try:
        w = Works()[doi]
    except Exception as e:
        logger.debug("OpenAlex lookup for DOI %s failed: %s", doi, e)
        return None
    return dict(w) if w else None


# ---------------------------------------------------------------------------
# Recent papers by author / venue / keyword
# ---------------------------------------------------------------------------


def fetch_recent_by_author(
    author_id: str,
    *,
    since: datetime,
    max_results: int = 50,
) -> list[dict[str, Any]]:
    """OpenAlex ID like 'A5013214678' (strip the URL prefix if present)."""
    author_id = _strip_prefix(author_id, "A")
    try:
        results = (
            Works()
            .filter(author={"id": author_id}, from_publication_date=since.date().isoformat())
            .sort(publication_date="desc")
            .get(per_page=min(max_results, 200))
        )
    except Exception as e:
        logger.warning("OpenAlex author fetch failed for %s: %s", author_id, e)
        return []
    return [dict(w) for w in results]


def fetch_recent_by_venue(
    source_id: str,
    *,
    since: datetime,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    source_id = _strip_prefix(source_id, "S")
    try:
        results = (
            Works()
            .filter(primary_location={"source": {"id": source_id}})
            .filter(from_publication_date=since.date().isoformat())
            .sort(publication_date="desc")
            .get(per_page=min(max_results, 200))
        )
    except Exception as e:
        logger.warning("OpenAlex venue fetch failed for %s: %s", source_id, e)
        return []
    return [dict(w) for w in results]


def fetch_recent_by_keyword(
    query: str,
    *,
    since: datetime,
    max_results: int = 50,
) -> list[dict[str, Any]]:
    try:
        results = (
            Works()
            .search(query)
            .filter(from_publication_date=since.date().isoformat())
            .sort(publication_date="desc")
            .get(per_page=min(max_results, 200))
        )
    except Exception as e:
        logger.warning("OpenAlex keyword fetch failed for %r: %s", query, e)
        return []
    return [dict(w) for w in results]


def fetch_recent_by_arxiv_category(
    category: str,
    *,
    since: datetime,
    max_results: int = 200,
) -> list[dict[str, Any]]:
    """`category` is e.g. 'cs.CL'. We use the OpenAlex topic/primary_location
    filter; OpenAlex does not have a 1:1 arXiv-category filter, so we search
    for the category string within concepts/primary_location and let OpenAlex
    do the relevance matching."""
    try:
        results = (
            Works()
            .filter(
                primary_location={"source": {"type": "repository"}},
                from_publication_date=since.date().isoformat(),
            )
            .search(category)
            .sort(publication_date="desc")
            .get(per_page=min(max_results, 200))
        )
    except Exception as e:
        logger.warning("OpenAlex arXiv-cat fetch failed for %r: %s", category, e)
        return []
    return [dict(w) for w in results]


# ---------------------------------------------------------------------------
# Autocomplete / search for the subscription editor UI
# ---------------------------------------------------------------------------


def search_authors(name: str, limit: int = 5) -> list[dict[str, Any]]:
    name = (name or "").strip()
    if not name:
        return []
    try:
        results = Authors().search(name).get(per_page=limit)
    except Exception as e:
        logger.warning("OpenAlex author search failed: %s", e)
        return []
    out = []
    for a in results:
        d = dict(a)
        out.append(
            {
                "id": _strip_id_prefix(d.get("id", "")),
                "name": d.get("display_name"),
                "works_count": d.get("works_count"),
                "last_known_institution": (
                    d.get("last_known_institutions") or [{}]
                )[0].get("display_name"),
            }
        )
    return out


def search_venues(name: str, limit: int = 5) -> list[dict[str, Any]]:
    name = (name or "").strip()
    if not name:
        return []
    try:
        results = Sources().search(name).get(per_page=limit)
    except Exception as e:
        logger.warning("OpenAlex venue search failed: %s", e)
        return []
    out = []
    for s in results:
        d = dict(s)
        out.append(
            {
                "id": _strip_id_prefix(d.get("id", "")),
                "name": d.get("display_name"),
                "issn_l": d.get("issn_l"),
                "works_count": d.get("works_count"),
                "type": d.get("type"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def work_id(work: dict[str, Any] | None) -> str:
    if not work:
        return ""
    return _strip_id_prefix(work.get("id") or "")


def work_doi(work: dict[str, Any] | None) -> str | None:
    if not work:
        return None
    return work.get("doi")


def work_title(work: dict[str, Any] | None) -> str:
    if not work:
        return "(untitled)"
    return (work.get("title") or "").strip() or "(untitled)"


def work_abstract(work: dict[str, Any] | None) -> str | None:
    """OpenAlex stores abstracts as an inverted index; pyalex restores plain text."""
    if not work:
        return None
    return work.get("abstract")


def work_publication_date(work: dict[str, Any] | None) -> date | None:
    if not work:
        return None
    raw = work.get("publication_date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def work_authors(work: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return [{name, openalex_author_id, affiliation}, ...]."""
    out: list[dict[str, Any]] = []
    if not work:
        return out
    for a in work.get("authorships") or []:
        author = a.get("author") or {}
        insts = a.get("institutions") or []
        out.append(
            {
                "name": author.get("display_name") or "",
                "openalex_author_id": _strip_id_prefix(author.get("id") or ""),
                "affiliation": (insts[0].get("display_name") if insts else None),
            }
        )
    return out


def work_venue(work: dict[str, Any] | None) -> str | None:
    if not work:
        return None
    primary = work.get("primary_location") or {}
    src = primary.get("source") or {}
    return src.get("display_name")


def work_arxiv_id(work: dict[str, Any] | None) -> str | None:
    """Best-effort: arXiv DOI is 10.48550/arXiv.XXXX.YYYYY."""
    if not work:
        return None
    doi = work_doi(work)
    if doi and doi.lower().startswith("10.48550/arxiv."):
        tail = doi.split("/")[-1]  # "arXiv.XXXX.YYYYY"
        if "." in tail:
            return tail.split(".", 1)[1]  # "XXXX.YYYYY"
        return None
    # Fallback: look in ids
    ids = work.get("ids") or {}
    for key in ("arxiv", "doi"):
        v = ids.get(key)
        if v and key == "arxiv":
            # sometimes looks like a URL; strip
            return v.split("/")[-1] if "/" in v else v
        if v and key == "doi" and v.lower().startswith("10.48550/arxiv."):
            return v.split(".", 1)[1] if "." in v.split("/")[-1] else None
    return None


def work_pdf_url(work: dict[str, Any] | None) -> tuple[str | None, str]:
    """Return (pdf_url, oa_status) where oa_status is 'oa' | 'closed' | 'none'."""
    if not work:
        return None, "none"
    if not (work.get("open_access") or {}).get("is_oa"):
        return None, "closed"
    best = work.get("best_oa_location") or {}
    url = best.get("pdf_url") or best.get("landing_page_url")
    if not url:
        return None, "none"
    return url, "oa"


# ---------------------------------------------------------------------------
# internal
# ---------------------------------------------------------------------------


def _strip_id_prefix(id_str: str) -> str:
    """OpenAlex IDs arrive as 'https://openalex.org/W12345' — we want 'W12345'."""
    if not id_str:
        return ""
    if "openalex.org/" in id_str:
        return id_str.split("openalex.org/")[-1].strip()
    return id_str.strip()


def _strip_prefix(value: str, prefix: str) -> str:
    v = (value or "").strip()
    if v.startswith("https://openalex.org/"):
        v = v.split("/")[-1]
    if v.startswith(prefix):
        return v
    return v
