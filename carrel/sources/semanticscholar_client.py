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
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.semanticscholar.org"
DEFAULT_USER_AGENT = "Carrel/0.1 (+https://github.com/)"
# S2 caps the citations page size at 1000; we default lower to stay polite.
DEFAULT_CITATIONS_LIMIT = 500
MAX_RETRIES = 3
_BASE_WAIT_SECONDS = 2.0
# S2 rate limits (per semanticscholar.org/product/api): an API key gets an
# introductory 1 request/second shared across ALL endpoints; unauthenticated
# traffic shares a congested pool that "may be further throttled". We default
# the unauthenticated tier conservatively to 0.5 RPS.
DEFAULT_RPS_WITH_KEY = 1.0
DEFAULT_RPS_WITHOUT_KEY = 0.5
# Cap a server-supplied Retry-After so one 429 can't stall the serial sync
# worker for minutes. 30s bounds the worst case while giving S2 room.
_MAX_RETRY_AFTER_SECONDS = 30.0

# Module-level shared client, configured once at startup (like openalex/arxiv).
_client: httpx.Client | None = None
_base_url = DEFAULT_BASE_URL
_api_key: str | None = None
_timeout = 30.0
_user_agent = DEFAULT_USER_AGENT
_max_retries = MAX_RETRIES


class _RateLimiter:
    """Process-global min-interval gate (one token, no burst) for S2.

    Thread-safe. ``configure(0)`` (the default) makes every method a no-op, so
    paths that never call :func:`configure` (tests injecting a mock client,
    direct/script use via the lazy client) are never throttled.

    Coordinates all threads within one process. The app runs as a single
    uvicorn process; if it ever moves to multiple workers, a cross-process
    lock would be required here.
    """

    def __init__(self, jitter_fraction: float = 0.15) -> None:
        self._lock = threading.Lock()
        self._interval = 0.0  # 0 == disabled
        self._next_allowed = 0.0  # monotonic timestamp
        self._jitter_fraction = jitter_fraction

    def configure(self, interval: float) -> None:
        with self._lock:
            self._interval = max(0.0, float(interval))
            if self._interval <= 0.0:
                self._next_allowed = 0.0

    @property
    def enabled(self) -> bool:
        return self._interval > 0.0

    @property
    def interval(self) -> float:
        return self._interval

    def acquire(self) -> None:
        """Block until the next request is allowed, then reserve its slot."""
        with self._lock:
            if self._interval <= 0.0:
                return
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait <= 0.0:
                # Gate is open now; reserve the slot after this one.
                self._next_allowed = now + self._jittered_interval()
                return
            # Reserve the next slot BEFORE sleeping so concurrent arrivals
            # stagger by one interval instead of all waking together.
            self._next_allowed += self._jittered_interval()
        time.sleep(max(0.0, wait))

    def penalty(self, seconds: float) -> None:
        """Push the gate to ``now + seconds`` so every thread backs off.

        Used on a 429 with a Retry-After header. Monotonic max: never moves
        the gate earlier than an already-scheduled wait.
        """
        if seconds <= 0.0:
            return
        with self._lock:
            if self._interval <= 0.0:
                return
            target = time.monotonic() + seconds
            if target > self._next_allowed:
                self._next_allowed = target

    def _jittered_interval(self) -> float:
        if self._jitter_fraction <= 0.0:
            return self._interval
        j = random.uniform(-self._jitter_fraction, self._jitter_fraction)
        return max(0.0, self._interval * (1.0 + j))


# Process-global gate, armed by configure() at startup.
_limiter = _RateLimiter()


def _build_client(
    base_url: str, api_key: str | None, timeout: float, user_agent: str
) -> httpx.Client:
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    return httpx.Client(base_url=base_url, timeout=timeout, headers=headers)


def configure(
    *,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    timeout: float = 30.0,
    user_agent: str = DEFAULT_USER_AGENT,
    max_retries: int = MAX_RETRIES,
    rate_limit_per_second: float | None = None,
) -> None:
    """Idempotently (re)build the shared httpx client and arm the rate limiter.

    Safe to call repeatedly. Production calls this once from the FastAPI
    lifespan, which ARMS the limiter. The lazy :func:`_get_client` path does
    NOT call this, so direct/script use and tests that inject their own
    client are never throttled.
    """
    global _client, _base_url, _api_key, _timeout, _user_agent, _max_retries
    if _client is not None:
        _client.close()
    _base_url = base_url.rstrip("/")
    _api_key = api_key
    _timeout = timeout
    _user_agent = user_agent
    _max_retries = max(1, int(max_retries))
    _client = _build_client(_base_url, _api_key, _timeout, _user_agent)

    if rate_limit_per_second is not None and rate_limit_per_second > 0:
        interval = 1.0 / rate_limit_per_second
    elif api_key:
        interval = 1.0 / DEFAULT_RPS_WITH_KEY
    else:
        interval = 1.0 / DEFAULT_RPS_WITHOUT_KEY
    _limiter.configure(interval)
    logger.info(
        "S2 rate limiter armed: interval=%.2fs (api_key=%s)",
        interval,
        bool(api_key),
    )


def _get_client() -> httpx.Client:
    # Bare default client for direct/script use. Deliberately does NOT call
    # configure() (and therefore does NOT arm _limiter), so this path stays
    # unthrottled and tests injecting their own client are unaffected.
    global _client
    if _client is None:
        _client = _build_client(DEFAULT_BASE_URL, None, _timeout, DEFAULT_USER_AGENT)
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
    # S2's bulk endpoint only accepts paperId / publicationDate / citationCount
    # (no `relevance`); when the bulk path is taken with sort=relevance (e.g.
    # limit > 100 with the default sort), fall back to a valid S2 sort. See
    # test_search_relevance_with_high_limit_uses_bulk_with_valid_sort.
    sort_map = {
        "citations": "citationCount:desc",
        "date": "publicationDate:desc",
        "relevance": "publicationDate:desc",
    }
    params: dict[str, str] = {
        "query": query,
        "fields": _SEARCH_FIELDS_BULK,
        "sort": sort_map[sort],
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
    for attempt in range(_max_retries):
        # Global gate: spaces every request across every endpoint/page/thread.
        _limiter.acquire()
        try:
            resp = client.get(url)
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < _max_retries - 1:
                time.sleep(_backoff(attempt))
                continue
            raise S2Error(f"S2 request failed: {e}") from e

        if resp.status_code == 404:
            logger.debug("S2 404 for %s", url)
            return None
        if resp.status_code in (429, 500, 502, 503, 504):
            last_exc = S2Error(f"S2 HTTP {resp.status_code}")
            if attempt < _max_retries - 1:
                retry_after = _retry_after(resp)
                if resp.status_code == 429 and retry_after is not None:
                    wait = min(retry_after, _MAX_RETRY_AFTER_SECONDS)
                    # Global backoff: push the gate so ALL threads hold until
                    # this elapses, killing the thundering herd. The next
                    # acquire() below blocks for the wait; sleep here too for
                    # the disabled-limiter (test/direct) path.
                    _limiter.penalty(wait)
                    if not _limiter.enabled:
                        time.sleep(wait)
                    logger.warning("S2 429; global backoff %.1fs", wait)
                else:
                    wait = _backoff(attempt)
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
    base = _BASE_WAIT_SECONDS * (2 ** attempt)
    # Equal jitter in [0.5*base, base] to desync concurrent retries.
    return base * (0.5 + random.random() * 0.5)


def _retry_after(resp: httpx.Response) -> float | None:
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except ValueError:
        return None
