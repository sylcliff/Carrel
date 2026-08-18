"""Minimal HTTP client for the Semantic Scholar Graph API.

We use S2 for citation data ("cited by" counts and the list of citing papers)
because it is free without an API key, indexes arXiv well, and returns
``citationCount`` / ``influentialCitationCount`` / ``referenceCount`` plus a
paged ``/paper/{id}/citations`` endpoint in a single round-trip pair.

Endpoint contract (verified against the live API):
  - GET /graph/v1/paper/{id}?fields=...
        id forms: ``DOI:<doi>``, ``ARXIV:<bare id>``, or the 40-char S2 paperId
  - GET /graph/v1/paper/{id}/citations?fields=title,year,externalIds&limit=N

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

    return CitationResult(
        s2_paper_id=counts.get("paperId"),
        citation_count=_as_int(counts.get("citationCount")),
        influential_count=_as_int(counts.get("influentialCitationCount")),
        reference_count=_as_int(counts.get("referenceCount")),
        citing_papers=citing,
    )


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
        f"?fields=title,year,externalIds&limit={min(limit, DEFAULT_CITATIONS_LIMIT)}"
    )
    resp = _get_with_retry(client, url)
    if resp is None:
        return []
    try:
        payload = resp.json()
    except ValueError as e:
        raise S2Error(f"invalid JSON from S2 citations: {e}") from e

    out: list[dict[str, Any]] = []
    for item in payload.get("data", []):
        cp = item.get("citingPaper") or {}
        ext = cp.get("externalIds") or {}
        doi = _clean_doi(ext.get("DOI"))
        arxiv_id = _strip_arxiv_version(ext.get("ArXiv")) if ext.get("ArXiv") else None
        out.append({
            "title": (cp.get("title") or "").strip() or None,
            "year": cp.get("year"),
            "doi": doi,
            "arxiv_id": arxiv_id,
            "s2_paper_id": cp.get("paperId"),
        })
    return out


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
