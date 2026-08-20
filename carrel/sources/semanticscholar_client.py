"""Minimal HTTP client for the Semantic Scholar Graph API.

We use S2 for citation data ("cited by" counts and the list of citing papers)
because it is free without an API key, indexes arXiv well, and returns
``citationCount`` / ``influentialCitationCount`` / ``referenceCount`` plus a
paged ``/paper/{id}/citations`` endpoint in a single round-trip pair.

Endpoint contract (verified against the live API):
  - GET /graph/v1/paper/{id}?fields=...
        id forms: ``DOI:<doi>``, ``ARXIV:<bare id>``, or the 40-char S2 paperId
  - GET /graph/v1/paper/{id}/citations?fields=title,year,externalIds&limit=N
  - GET /graph/v1/paper/search?query=...&fields=...&limit=N&offset=N
        relevance-ranked, max 100 per page
  - GET /graph/v1/paper/search/bulk?query=...&fields=...&sort=citationCount:desc
        token-paginated, max 1000 per page, supports citation/date sort

An optional API key (``x-api-key`` header) raises the rate limit; without one
we back off on 429. We never import the third-party ``semanticscholar`` PyPI
package, just httpx, matching arxiv.py / mineru_client.py.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.semanticscholar.org"
# S2 caps the citations page size at 1000; we default lower to stay polite.
DEFAULT_CITATIONS_LIMIT = 500
MAX_RETRIES = 3
_BASE_WAIT_SECONDS = 2.0

# Module-level shared client, configured once at startup (like openalex/arxiv).
_client: httpx.Client | None = None
_base_url = DEFAULT_BASE_URL
_api_key: str | None = None
_timeout = 30.0


def configure(
    *,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    timeout: float = 30.0,
    user_agent: str = "Carrel/0.1 (+https://github.com/)",
) -> None:
    """Idempotently (re)build the shared httpx client. Safe to call repeatedly."""
    global _client, _base_url, _api_key, _timeout
    if _client is not None:
        _client.close()
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    _base_url = base_url.rstrip("/")
    _api_key = api_key
    _timeout = timeout
    _client = httpx.Client(timeout=timeout, headers=headers)


def _get_client() -> httpx.Client:
    if _client is None:
        configure()
    assert _client is not None
    return _client


class S2Error(Exception):
    """Transport/protocol error talking to Semantic Scholar."""


@dataclass(slots=True)
class CitationResult:
    s2_paper_id: str | None
    citation_count: int | None
    influential_count: int | None
    reference_count: int | None
    citing_papers: list[dict[str, Any]] = field(default_factory=list)
    referenced_papers: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_citations(
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    s2_id: str | None = None,
    limit: int = DEFAULT_CITATIONS_LIMIT,
    client: httpx.Client | None = None,
) -> CitationResult | None:
    """Fetch citation counts and the (capped) citing-paper list.

    Identifier priority: explicit ``s2_id``, then ``DOI:<doi>``, then
    ``ARXIV:<arxiv_id>``. Returns ``None`` when the paper cannot be found in S2
    (404) or no identifier is available. Raises :class:`S2Error` on repeated
    transport/rate-limit failures; callers (sync) should log and continue.
    """
    lookup_id = _build_lookup_id(doi=doi, arxiv_id=arxiv_id, s2_id=s2_id)
    if not lookup_id:
        return None

    httpx_client = client or _get_client()
    counts = _get_counts(httpx_client, lookup_id)
    if counts is None:
        return None

    canonical_id = counts.get("paperId") or lookup_id
    citing = _get_citing_papers(httpx_client, canonical_id, limit=limit)
    referenced = _get_referenced_papers(httpx_client, canonical_id, limit=limit)

    return CitationResult(
        s2_paper_id=counts.get("paperId"),
        citation_count=_as_int(counts.get("citationCount")),
        influential_count=_as_int(counts.get("influentialCitationCount")),
        reference_count=_as_int(counts.get("referenceCount")),
        citing_papers=citing,
        referenced_papers=referenced,
    )


# Fields used by search. TLDR and publicationVenue are sometimes null; the
# normalizer treats any missing field as None rather than dropping the row.
_SEARCH_FIELDS = (
    "paperId,title,abstract,year,venue,publicationVenue,publicationTypes,"
    "publicationDate,externalIds,url,openAccessPdf,authors,citationCount,"
    "referenceCount,fieldsOfStudy,tldr"
)
# Bulk endpoint rejects `tldr` (and a few other recommendation-only fields)
# with HTTP 400 "Unrecognized or unsupported fields". Drop it there; the
# normalizer tolerates its absence.
_SEARCH_FIELDS_BULK = (
    "paperId,title,abstract,year,venue,publicationVenue,publicationTypes,"
    "publicationDate,externalIds,url,openAccessPdf,authors,citationCount,"
    "referenceCount,fieldsOfStudy"
)


def _build_year_param(
    year_from: int | None, year_to: int | None
) -> str | None:
    """S2 wants ``YYYY-YYYY`` / ``YYYY-`` / ``-YYYY``."""
    if year_from is None and year_to is None:
        return None
    return f"{year_from or ''}-{year_to or ''}"


def search_papers(
    query: str,
    *,
    limit: int = 20,
    year_from: int | None = None,
    year_to: int | None = None,
    min_citations: int | None = None,
    venue_types: list[str] | None = None,
    fields_of_study: list[str] | None = None,
    open_access_only: bool = False,
    sort: str = "relevance",
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Search S2 for papers matching ``query``.

    Uses the relevance endpoint (offset pagination, ≤100) by default. When
    ``sort`` is ``citations`` or ``date``, or ``limit`` exceeds 100, switches to
    the bulk endpoint (token pagination, ≤1000) which supports
    ``citationCount:desc`` / ``publicationDate:desc`` sorts. Returns normalized
    dicts (see ``_normalize_search_row``). Empty list on 404 / no query; raises
    :class:`S2Error` on repeated transport failures.
    """
    query = (query or "").strip()
    if not query:
        return []

    httpx_client = client or _get_client()
    use_bulk = sort in ("citations", "date") or limit > 100
    if use_bulk:
        rows = _search_bulk(
            httpx_client, query, limit, sort, year_from, year_to,
            min_citations, fields_of_study, open_access_only,
        )
    else:
        rows = _search_relevance(
            httpx_client, query, min(limit, 100), year_from, year_to,
            min_citations, fields_of_study, open_access_only,
        )

    out: list[dict[str, Any]] = []
    allowed_venue_types = {v.lower() for v in (venue_types or [])}
    for w in rows:
        row = _normalize_search_row(w)
        if row is None:
            continue
        # Safety net: S2's server-side minCitationCount is sometimes honoured
        # loosely; re-check on the client regardless of endpoint.
        if min_citations is not None:
            cc = row.get("citation_count") or 0
            if cc < min_citations:
                continue
        # venueType is not a documented S2 search param, so post-filter.
        if allowed_venue_types:
            vt = (row.get("venue_type") or "").lower()
            if vt not in allowed_venue_types:
                continue
        # openAccessPdf server filter is a hint; also enforce locally.
        if open_access_only and not row.get("pdf_url"):
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _search_relevance(
    client: httpx.Client,
    query: str,
    limit: int,
    year_from: int | None,
    year_to: int | None,
    min_citations: int | None,
    fields_of_study: list[str] | None,
    open_access_only: bool,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {
        "query": query,
        "fields": _SEARCH_FIELDS,
        "limit": str(limit),
    }
    year = _build_year_param(year_from, year_to)
    if year:
        params["year"] = year
    if min_citations is not None:
        params["minCitationCount"] = str(min_citations)
    if fields_of_study:
        params["fieldsOfStudy"] = ",".join(fields_of_study)
    if open_access_only:
        params["openAccessPdf"] = ""
    url = f"{_base_url}/graph/v1/paper/search?" + _urlencode(params)
    resp = _get_with_retry(client, url)
    if resp is None:
        return []
    try:
        payload = resp.json()
    except ValueError as e:
        raise S2Error(f"invalid JSON from S2 search: {e}") from e
    return payload.get("data") or []


def _search_bulk(
    client: httpx.Client,
    query: str,
    limit: int,
    sort: str,
    year_from: int | None,
    year_to: int | None,
    min_citations: int | None,
    fields_of_study: list[str] | None,
    open_access_only: bool,
) -> list[dict[str, Any]]:
    """Token-paginated bulk fetch. Follows the ``token`` cursor as needed."""
    sort_map = {
        "citations": "citationCount:desc",
        "date": "publicationDate:desc",
        "relevance": "relevance:desc",
    }
    params: dict[str, str] = {
        "query": query,
        "fields": _SEARCH_FIELDS_BULK,
        "sort": sort_map.get(sort, "citationCount:desc"),
    }
    year = _build_year_param(year_from, year_to)
    if year:
        params["year"] = year
    if min_citations is not None:
        params["minCitationCount"] = str(min_citations)
    if fields_of_study:
        params["fieldsOfStudy"] = ",".join(fields_of_study)
    if open_access_only:
        params["openAccessPdf"] = ""

    out: list[dict[str, Any]] = []
    token: str | None = None
    # Bulk page size max is 1000; we only need up to `limit`, but pull at least
    # 100 per round so we don't chase tokens for tiny result sets.
    page_size = min(max(limit, 100), 1000)
    while len(out) < limit:
        params["limit"] = str(page_size)
        if token:
            params["token"] = token
        url = f"{_base_url}/graph/v1/paper/search/bulk?" + _urlencode(params)
        resp = _get_with_retry(client, url)
        if resp is None:
            break
        try:
            payload = resp.json()
        except ValueError as e:
            raise S2Error(f"invalid JSON from S2 bulk search: {e}") from e
        data = payload.get("data") or []
        out.extend(data)
        token = payload.get("token")
        if not token or not data:
            break
    return out[:limit]


def _normalize_search_row(w: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce an S2 work into the shape the search merge expects."""
    paper_id = w.get("paperId")
    title = (w.get("title") or "").strip()
    if not paper_id or not title:
        return None
    ext = w.get("externalIds") or {}
    doi = _clean_doi(ext.get("DOI"))
    arxiv_id = _strip_arxiv_version(ext.get("ArXiv")) if ext.get("ArXiv") else None

    venue_name = w.get("venue") or None
    pv = w.get("publicationVenue") or {}
    venue_type = pv.get("type") if isinstance(pv, dict) else None
    if not venue_name and isinstance(pv, dict):
        venue_name = pv.get("name")

    oa_pdf = w.get("openAccessPdf") or {}
    pdf_url = oa_pdf.get("url") if isinstance(oa_pdf, dict) else None

    authors = [
        (a.get("name") or "").strip()
        for a in (w.get("authors") or [])
        if isinstance(a, dict) and a.get("name")
    ]

    # publicationDate is ISO; fall back to year.
    pub_date = w.get("publicationDate")
    if not pub_date and w.get("year"):
        pub_date = str(w.get("year"))

    tldr_obj = w.get("tldr")
    tldr = None
    if isinstance(tldr_obj, dict):
        tldr = tldr_obj.get("text")

    return {
        "s2_paper_id": paper_id,
        "title": title,
        "abstract": w.get("abstract"),
        "authors": authors,
        "venue": venue_name,
        "venue_type": venue_type,
        "publication_date": pub_date,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "pdf_url": pdf_url,
        "url": w.get("url"),
        "citation_count": _as_int(w.get("citationCount")),
        "reference_count": _as_int(w.get("referenceCount")),
        "fields_of_study": w.get("fieldsOfStudy") or [],
        "tldr": tldr,
        "source": "semantic_scholar",
    }


def fetch_paper(
    s2_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Look up a single S2 paper by its id and return the normalized shape.

    Used by the import path when only an S2 paperId is available. Returns None
    on 404; raises :class:`S2Error` on transport failure.
    """
    s2_id = (s2_id or "").strip()
    if not s2_id:
        return None
    httpx_client = client or _get_client()
    url = f"{_base_url}/graph/v1/paper/{s2_id}?fields={_SEARCH_FIELDS}"
    resp = _get_with_retry(httpx_client, url)
    if resp is None:
        return None
    try:
        w = resp.json()
    except ValueError as e:
        raise S2Error(f"invalid JSON from S2 paper lookup: {e}") from e
    return _normalize_search_row(w)


def _urlencode(params: dict[str, str]) -> str:
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in params.items() if v is not None})


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get_counts(client: httpx.Client, lookup_id: str) -> dict[str, Any] | None:
    url = (
        f"{_base_url}/graph/v1/paper/{lookup_id}"
        "?fields=citationCount,influentialCitationCount,referenceCount,externalIds"
    )
    resp = _get_with_retry(client, url)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError as e:
        raise S2Error(f"invalid JSON from S2 paper lookup: {e}") from e


def _get_citing_papers(
    client: httpx.Client, s2_id: str, *, limit: int
) -> list[dict[str, Any]]:
    url = (
        f"{_base_url}/graph/v1/paper/{s2_id}/citations"
        f"?fields=title,year,venue,externalIds&limit={min(limit, DEFAULT_CITATIONS_LIMIT)}"
    )
    resp = _get_with_retry(client, url)
    if resp is None:
        return []
    try:
        payload = resp.json()
    except ValueError as e:
        raise S2Error(f"invalid JSON from S2 citations: {e}") from e

    out: list[dict[str, Any]] = []
    # S2 occasionally returns {"data": null} for papers with no matches.
    for item in (payload.get("data") or []):
        cp = item.get("citingPaper") or {}
        ext = cp.get("externalIds") or {}
        doi = _clean_doi(ext.get("DOI"))
        arxiv_id = _strip_arxiv_version(ext.get("ArXiv")) if ext.get("ArXiv") else None
        out.append({
            "title": (cp.get("title") or "").strip() or None,
            "year": cp.get("year"),
            "venue": _clean_venue(cp.get("venue")),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "s2_paper_id": cp.get("paperId"),
        })
    return out


def _get_referenced_papers(
    client: httpx.Client, s2_id: str, *, limit: int
) -> list[dict[str, Any]]:
    """Papers this paper cites (its bibliography / references).

    Mirrors :func:`_get_citing_papers` but reads ``citedPaper`` from S2's
    ``/paper/{id}/references`` endpoint. Entries without any title or
    identifier (S2 sometimes returns unmatched placeholder rows) are dropped.
    """
    url = (
        f"{_base_url}/graph/v1/paper/{s2_id}/references"
        f"?fields=title,year,venue,externalIds&limit={min(limit, DEFAULT_CITATIONS_LIMIT)}"
    )
    resp = _get_with_retry(client, url)
    if resp is None:
        return []
    try:
        payload = resp.json()
    except ValueError as e:
        raise S2Error(f"invalid JSON from S2 references: {e}") from e

    out: list[dict[str, Any]] = []
    # S2 occasionally returns {"data": null} for papers with no matches.
    for item in (payload.get("data") or []):
        cp = item.get("citedPaper") or {}
        ext = cp.get("externalIds") or {}
        doi = _clean_doi(ext.get("DOI"))
        arxiv_id = _strip_arxiv_version(ext.get("ArXiv")) if ext.get("ArXiv") else None
        title = (cp.get("title") or "").strip() or None
        # S2 returns filler rows for references it couldn't link; skip those
        # unless they at least carry a title worth showing.
        if not title and not (doi or arxiv_id or cp.get("paperId")):
            continue
        out.append({
            "title": title,
            "year": cp.get("year"),
            "venue": _clean_venue(cp.get("venue")),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "s2_paper_id": cp.get("paperId"),
        })
    return out


def _clean_venue(v: Any) -> str | None:
    """S2's `venue` is a free-text string; normalize empties to None."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


def _get_with_retry(client: httpx.Client, url: str) -> httpx.Response | None:
    """GET with 429/5xx backoff. Returns None on 404, raises S2Error otherwise."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(url)
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(_backoff(attempt))
                continue
            raise S2Error(f"S2 request failed: {e}") from e

        if resp.status_code == 404:
            logger.debug("S2 404 for %s", url)
            return None
        if resp.status_code in (429, 500, 502, 503, 504):
            last_exc = S2Error(f"S2 HTTP {resp.status_code}")
            if attempt < MAX_RETRIES - 1:
                wait = _retry_after(resp) or _backoff(attempt)
                logger.warning("S2 %s; sleeping %.1fs", resp.status_code, wait)
                time.sleep(wait)
                continue
            raise S2Error(f"S2 HTTP {resp.status_code} after retries")
        if resp.status_code >= 400:
            raise S2Error(f"S2 HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    if last_exc:
        raise S2Error(f"S2 request failed after retries: {last_exc}")
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_lookup_id(
    *, doi: str | None, arxiv_id: str | None, s2_id: str | None
) -> str | None:
    if s2_id:
        return s2_id
    if doi:
        return f"DOI:{_clean_doi(doi)}"
    if arxiv_id:
        return f"ARXIV:{_strip_arxiv_version(arxiv_id)}"
    return None


def _clean_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.IGNORECASE)
    return d or None


def _strip_arxiv_version(arxiv_id: str | None) -> str | None:
    if not arxiv_id:
        return None
    return re.sub(r"v\d+$", "", arxiv_id.strip())


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int) -> float:
    return _BASE_WAIT_SECONDS * (2 ** attempt)


def _retry_after(resp: httpx.Response) -> float | None:
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except ValueError:
        return None
