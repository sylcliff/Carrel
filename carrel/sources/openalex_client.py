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
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

import pyalex
import pyalex.api as _pyalex_api
import requests
from pyalex import Authors, Sources, Works, invert_abstract
from urllib3.util import Retry

from carrel.config import CarrelYAML

logger = logging.getLogger(__name__)

# OpenAlex's budget-exhausted 429 carries a Retry-After measured in minutes
# (e.g. "Insufficient budget ... Resets at midnight UTC", retryAfter=253).
# urllib3 honours Retry-After verbatim, so a single best-effort cites: lookup
# could block the citations job for ~10 minutes. Cap it so OA failures fail
# fast — fetch_citing_works already treats exceptions as "no extras".
_MAX_RETRY_AFTER_SECONDS = 5.0


class _CappedRetry(Retry):
    """urllib3 Retry that clamps a long Retry-After to a few seconds."""

    def get_retry_after(self, response: Any) -> float | None:  # type: ignore[override]
        val = super().get_retry_after(response)
        if val is None:
            return None
        return min(float(val), _MAX_RETRY_AFTER_SECONDS)


# Saved once so repeated configure() calls don't re-wrap an already wrapped
# session factory.
_ORIG_SESSION_FACTORY = _pyalex_api._get_requests_session


def configure(cfg: CarrelYAML) -> None:
    """Idempotent. Set pyalex globals from our config; safe to call repeatedly."""
    pyalex.config.email = cfg.openalex.mailto or None
    pyalex.config.api_key = cfg.openalex.api_key or None
    pyalex.config.max_retries = cfg.openalex.max_retries
    pyalex.config.retry_backoff_factor = 0.5
    pyalex.config.retry_http_codes = [429, 500, 503]

    # pyalex's requests session has no timeout, so a hung connection blocks
    # forever (and retries multiply that into tens of minutes). The YAML defines
    # request_timeout_seconds but pyalex never reads it; inject it by wrapping
    # the per-request session factory. Use a (connect, read) tuple so a dead
    # proxy fails fast on connect while slow-but-live responses get the full
    # read budget.
    timeout = cfg.openalex.request_timeout_seconds
    connect_timeout = min(10, timeout)

    def _timed_session() -> Any:
        session = _ORIG_SESSION_FACTORY()
        # Replace pyalex's default adapter so a budget-exhausted 429 doesn't
        # block for minutes on its Retry-After header (see _CappedRetry).
        retries = _CappedRetry(
            total=cfg.openalex.max_retries,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 503],
            allowed_methods={"GET", "POST"},
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        original_get = session.get

        def get_with_timeout(url: str, **kwargs: Any) -> Any:
            kwargs.setdefault("timeout", (connect_timeout, timeout))
            return original_get(url, **kwargs)

        session.get = get_with_timeout
        return session

    _pyalex_api._get_requests_session = _timed_session


# ---------------------------------------------------------------------------
# Work lookups
# ---------------------------------------------------------------------------


def _title_similarity(a: str | None, b: str | None) -> float:
    """Token Jaccard overlap in [0, 1] for loose title matching."""
    import re as _re

    toks_a = {t for t in _re.findall(r"[a-z0-9]+", (a or "").lower()) if len(t) > 1}
    toks_b = {t for t in _re.findall(r"[a-z0-9]+", (b or "").lower()) if len(t) > 1}
    if not toks_a or not toks_b:
        return 0.0
    return len(toks_a & toks_b) / max(len(toks_a | toks_b), 1)


_ARXIV_TITLE_MATCH_THRESHOLD = 0.85


def lookup_by_arxiv_id(
    arxiv_id: str,
    *,
    title_hint: str | None = None,
) -> dict[str, Any] | None:
    """Return the OpenAlex Work dict for an arXiv ID, or None.

    OpenAlex does not reliably index arXiv DOIs of the form 10.48550/arXiv.X.Y
    (returns 404 for many). The reliable approach is a multi-step search:
    try the DOI form first, then search by the arXiv ID as a token and pick
    the top hit that actually matches.

    When ``title_hint`` is supplied and no id-field match is found, fall back to
    a title search and accept a top hit whose normalized title has high token
    overlap (≥ 0.85). This rescues preprints OpenAlex indexed under a title but
    without a back-linked arXiv id.
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

    # 2. Search fallback — find a work whose ids actually reference this arXiv ID.
    #    We do NOT return a top search hit on a weak id match: attaching the wrong
    #    canonical Work ID would corrupt authorship/venue data for a paper.
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
            arxiv_field = (ids.get("arxiv") or "").split("/")[-1].lower()
            # strip a possible version suffix on both sides for the comparison
            arxiv_field = re.sub(r"v\d+$", "", arxiv_field)
            target = re.sub(r"v\d+$", "", arxiv_id.lower())
            doi_field = (d.get("doi") or "").lower()
            if arxiv_field == target or doi_field.endswith(f"arxiv.{target}"):
                return d
    except Exception as e:
        logger.debug("Search lookup failed for %s: %s", arxiv_id, e)

    # 3. Title-hint fallback — OpenAlex sometimes has the work but never linked
    #    its arXiv id. Accept only a strong title match to avoid wrong imports.
    if title_hint:
        try:
            for cand in search_work(title_hint, limit=5):
                cand_title = cand.get("title") or cand.get("display_name") or ""
                if _title_similarity(title_hint, cand_title) >= _ARXIV_TITLE_MATCH_THRESHOLD:
                    return cand
        except Exception as e:  # noqa: BLE001
            logger.debug("Title-hint lookup failed for %s: %s", arxiv_id, e)

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


def fetch_work_authors(
    doi: str | None,
    arxiv_id: str | None,
    *,
    title_hint: str | None = None,
) -> list[dict[str, Any]] | None:
    """Resolve a paper's canonical authorship from OpenAlex.

    Returns ``[{name, openalex_author_id, affiliation}, ...]`` or None when no
    Work could be found. Tries, in order: the DOI via the low-budget ``doi:``
    form, the arXiv DOI form (``10.48550/arXiv.<id>``), then the multi-step
    arXiv search in :func:`lookup_by_arxiv_id`.

    The ``doi:`` URL form is required — the ``/works/https://doi.org/...`` form
    costs substantially more budget and quickly returns 429.
    """

    def _by_doi(ident: str) -> dict[str, Any] | None:
        ident = (ident or "").strip()
        if not ident:
            return None
        try:
            w = Works()[f"doi:{ident}"]
        except Exception as e:  # noqa: BLE001
            logger.debug("OpenAlex doi: lookup for %s failed: %s", ident, e)
            return None
        return dict(w) if w else None

    work: dict[str, Any] | None = None
    if doi:
        work = _by_doi(doi)
    if work is None and arxiv_id:
        work = _by_doi(f"10.48550/arXiv.{arxiv_id.strip()}")
    if work is None and arxiv_id:
        work = lookup_by_arxiv_id(arxiv_id, title_hint=title_hint)

    if not work:
        return None
    return work_authors(work)


def fetch_citing_works(
    identifier: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return works that cite `identifier` (OpenAlex W-id, DOI, or arXiv id).

    The OpenAlex `cites` filter accepts any of those forms. Returned dicts are
    raw Work records (same shape as ``fetch_recent_by_*``); callers normalize
    to the schema they need.
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return []
    try:
        results = Works().filter(cites=identifier).get(per_page=min(limit, 200))
    except Exception as e:
        logger.warning("OpenAlex cites=%s fetch failed: %s", identifier, e)
        return []
    return [dict(w) for w in results]


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


def fetch_author_works(
    author_id: str,
    *,
    cursor: str | None = None,
    limit: int = 25,
) -> tuple[list[dict[str, Any]], str | None, int | None]:
    """Page one OpenAlex author's published works (newest first).

    Returns ``(items, next_cursor, total)``. ``next_cursor`` is ``None`` when
    OpenAlex signals there are no more pages; pass it back as ``cursor`` to
    load the next page. ``limit`` is clamped to OpenAlex's per-page range of
    [1, 200]. ``total`` is OpenAlex's reported work count for this author
    (from response ``meta.count``); it is the same on every page so callers
    can use the first page's value to render "Showing X of Y".

    Unlike :func:`fetch_recent_by_author` this is not time-windowed — the
    author page is a "show me everything" list, not a "since X" sync feed.

    Pyalex gotcha: ``Works().get(per_page=N)`` defaults to *page* pagination,
    which returns ``meta["page"]`` rather than ``meta["next_cursor"]``. To get
    cursor tokens back we must pass ``cursor="*"`` (pyalex's
    ``Paginator.VALUE_CURSOR_START``) on the first call. Subsequent calls
    pass the returned token; an empty/null token means "no more pages".
    """
    a_id = _strip_prefix(author_id, "A")
    if not a_id:
        return [], None, None
    per_page = min(max(limit, 1), 50)
    page_cursor = cursor if cursor else "*"
    try:
        results = (
            Works()
            .filter(author={"id": a_id})
            .sort(publication_date="desc", cited_by_count="desc")
            .get(per_page=per_page, cursor=page_cursor)
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("OpenAlex author-works fetch failed for %s: %s", a_id, e)
        return [], None, None
    items = [dict(w) for w in results]
    next_cursor = None
    total = None
    try:
        meta = results.meta or {}
        next_cursor = meta.get("next_cursor") or None
        raw = meta.get("count")
        total = int(raw) if raw is not None else None
    except AttributeError:
        pass
    except (TypeError, ValueError):
        pass
    return items, next_cursor, total


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


def search_work(
    query: str,
    *,
    limit: int = 20,
    year_from: int | None = None,
    year_to: int | None = None,
    min_citations: int | None = None,
    open_access_only: bool = False,
    sort: str = "relevance",
) -> list[dict[str, Any]]:
    """Search OpenAlex Works with faceted filters.

    Assembles one AND-joined filter string:
      ``from_publication_date`` / ``to_publication_date`` (YYYY-MM-DD),
      ``is_oa:true``, ``cited_by_count:>N``.
    ``sort`` maps to ``relevance_score:desc`` / ``cited_by_count:desc`` /
    ``publication_date:desc``. Returns raw Work dicts; callers extract fields
    via the ``work_*`` helpers.
    """
    query = (query or "").strip()
    if not query:
        return []

    filters: dict[str, Any] = {}
    if year_from is not None:
        filters["from_publication_date"] = f"{year_from}-01-01"
    if year_to is not None:
        filters["to_publication_date"] = f"{year_to}-12-31"
    if open_access_only:
        filters["is_oa"] = "true"
    if min_citations is not None and min_citations > 0:
        filters["cited_by_count"] = f">{min_citations}"

    sort_map = {
        "relevance": {"relevance_score": "desc"},
        "citations": {"cited_by_count": "desc"},
        "date": {"publication_date": "desc"},
    }
    sort_kwargs = sort_map.get(sort, {"relevance_score": "desc"})

    try:
        q = Works().search(query)
        if filters:
            q = q.filter(**filters)
        results = q.sort(**sort_kwargs).get(per_page=min(max(limit, 1), 50))
    except Exception as e:
        logger.warning("OpenAlex search failed for %r: %s", query, e)
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


def fetch_author(author_id: str) -> dict[str, Any] | None:
    """Fetch one OpenAlex Author by bare ID (e.g. 'A5013214678').

    Returns a compact profile dict for the Scholar detail page, or None on
    any failure (network, rate limit, missing record). Callers should treat
    None as "profile unavailable" rather than an error.
    """
    a_id = _strip_prefix(author_id, "A")
    if not a_id:
        return None
    try:
        d = dict(Authors()[a_id])
    except Exception as e:  # noqa: BLE001
        logger.warning("OpenAlex author fetch failed for %s: %s", a_id, e)
        return None
    insts = d.get("last_known_institutions") or []
    inst = (insts[0] or {}).get("display_name") if insts else None
    summary = d.get("summary_stats") or {}
    topics = []
    for t in d.get("topics") or []:
        if isinstance(t, dict) and t.get("id"):
            topics.append({"id": _strip_id_prefix(t.get("id", "")), "name": t.get("display_name")})
    return {
        "id": _strip_id_prefix(d.get("id", "")),
        "name": d.get("display_name"),
        "affiliation": inst,
        "works_count": d.get("works_count"),
        "cited_by_count": d.get("cited_by_count"),
        "h_index": summary.get("h_index"),
        "orcid": d.get("orcid"),
        "alternate_names": d.get("display_name_alternatives") or [],
        "topics": topics,
    }


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
    """Restore the abstract from OpenAlex's inverted index.

    pyalex (>=0.15) leaves abstracts as `abstract_inverted_index`; the flat
    `abstract` field is empty. We invert it here into readable text.
    """
    if not work:
        return None
    restored = invert_abstract(work.get("abstract_inverted_index"))
    if restored:
        return restored
    flat = work.get("abstract")
    return flat or None


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
    """Extract a bare arXiv ID (e.g. '2401.00001') from an OpenAlex work.

    OpenAlex stores DOIs as full URLs ('https://doi.org/10.48550/arxiv.X.Y'),
    and sometimes exposes an `ids.arxiv` URL. We check both.
    """
    if not work:
        return None

    def _from_doi(doi: str | None) -> str | None:
        if not doi:
            return None
        tail = doi.rsplit("/", 1)[-1]  # 'arxiv.2401.00001'
        if tail.lower().startswith("arxiv."):
            candidate = tail.split(".", 1)[1]
            return candidate or None
        return None

    found = _from_doi(work_doi(work))
    if found:
        return found

    ids = work.get("ids") or {}
    arxiv_field = ids.get("arxiv")
    if arxiv_field:
        return arxiv_field.rstrip("/").rsplit("/", 1)[-1]
    return _from_doi(ids.get("doi"))


def work_pdf_url(work: dict[str, Any] | None) -> tuple[str | None, str]:
    """Return (best_pdf_url, oa_status).

    The chosen URL is the highest-ranked of the work's candidate OA PDF URLs
    (see :func:`work_pdf_candidates`). A landing-page HTML URL is never
    returned directly; however OpenAlex sometimes mislabels a publisher HTML
    page as a ``pdf_url`` (e.g. an IOP ``/pdf`` route that serves text/html),
    so the ranker prefers repository/arXiv copies — which are reliably real
    PDFs — over publisher URLs. Callers that actually download should still
    validate by content-type/magic bytes and fall through to the rest of
    :func:`work_pdf_candidates`.

    oa_status is 'oa' when at least one candidate PDF URL exists, 'closed'
    when the work is paywalled, and 'none' when it is OA but no direct PDF URL
    is advertised (HTML-only OA).
    """
    candidates = work_pdf_candidates(work)
    if not candidates:
        if not work:
            return None, "none"
        oa = work.get("open_access") or {}
        return None, "closed" if not oa.get("is_oa") else "none"
    return candidates[0], "oa"


def work_pdf_candidates(work: dict[str, Any] | None) -> list[str]:
    """Return all advertised OA PDF URLs for ``work``, best-first and de-duped.

    Ordering: arXiv/repository copies first (canonical, almost always a real
    PDF and never a publisher landing page), then any other location, with
    ``best_oa_location`` leading its tier. OpenAlex's ``best_oa_location`` is
    *not* trusted on its own: for hybrid-OA works it is often the publisher's
    HTML ``/pdf`` route (which returns ``text/html``), while a perfectly good
    arXiv PDF sits further down ``locations``. Empty/landing-page-only
    locations are skipped.
    """
    if not work:
        return []
    # Respect OpenAlex's OA flag: a non-OA work should not surface a PDF URL
    # even if some stray location carries one.
    oa = work.get("open_access") or {}
    if not oa.get("is_oa"):
        return []

    raw: list[tuple[str, Any, int]] = []  # (pdf_url, location dict, order)
    seen: set[str] = set()
    order = 0

    def _add(loc: Any) -> None:
        nonlocal order
        if not isinstance(loc, dict):
            return
        url = (loc.get("pdf_url") or "").strip()
        if url and url not in seen:
            seen.add(url)
            raw.append((url, loc, order))
            order += 1

    _add(work.get("best_oa_location"))
    for loc in work.get("locations") or []:
        _add(loc)

    if not raw:
        return []

    def _rank(item: tuple[str, Any, int]) -> tuple[int, int]:
        url, loc, idx = item
        host = urlsplit(url).netloc.lower()
        source = (loc.get("source") or {}) if isinstance(loc, dict) else {}
        stype = (source.get("type") or "").lower()
        name = (source.get("display_name") or "").lower()
        is_arxiv = "arxiv.org" in host or "arxiv" in name
        is_repo = stype == "repository"
        # Lower rank wins. arXiv > other repositories > publisher/everything.
        tier = 0 if is_arxiv else (1 if is_repo else 2)
        # Within a tier keep insertion order (best_oa_location first).
        return tier, idx

    return [url for url, _, _ in sorted(raw, key=_rank)]


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
