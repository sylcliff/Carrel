"""Thin wrapper around the Crossref REST API (https://api.crossref.org).

Crossref is the DOI registration authority; it carries authoritative metadata
for ~150M+ scholarly works (DOI, authors with ORCID, container title, type,
abstract, publication date, link list). It is the canonical place to look
up a paper by its DOI and the most authoritative source for DOI-anchored
metadata — used here as Carrel's 4th ``/search`` source.

**No API key is required.** Crossref runs a "polite pool" that bumps the
free tier from ~10 to ~50 req/s when the User-Agent contains a ``mailto:``
contact. :func:`configure` builds that User-Agent from
``config.crossref.mailto``.

429 backoff: a recorded 429 latch lives in :data:`crossref_throttle`; the
:func:`throttle_aware` decorator on :func:`search_papers` short-circuits to
``[]`` while the latch is open so the search endpoint surfaces a
``warnings: ["crossref: rate-limited, resets in ..."]`` entry instead of
blocking on a long Retry-After.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from carrel.config import CrossrefConfig
from carrel.sources.throttle import (
    crossref_throttle,
    throttle_aware,
)

logger = logging.getLogger(__name__)


class CrossrefError(Exception):
    """Raised on transport / parse failure that isn't a clean 404 or 429.

    The search endpoint's catch-all treats this the same as any other
    exception: log + return empty list + append a per-source warning.
    """


# Module-level shared client. Rebuilt on each :func:`configure` call so the
# settings UI can swap mailto / timeout without a process restart for
# everything except the very first call (which still uses the previous
# client until the next call).
_client: httpx.Client | None = None
_cfg: CrossrefConfig | None = None


def configure(cfg: CrossrefConfig) -> None:
    """Idempotent. Build a shared :class:`httpx.Client` with the polite-pool UA.

    Safe to call repeatedly; ``_client`` is replaced on each call. The very
    first request after a reconfigure still uses the *old* client (the
    active request holds a reference); subsequent calls see the new one.
    """
    global _client, _cfg
    _cfg = cfg
    ua = _build_user_agent(cfg)
    _client = httpx.Client(
        base_url=cfg.base_url,
        headers={"User-Agent": ua, "Accept": "application/json"},
        timeout=cfg.request_timeout_seconds,
    )


def _build_user_agent(cfg: CrossrefConfig) -> str:
    """Crossref polite-pool wants a ``mailto:`` somewhere in the User-Agent."""
    parts = ["Carrel/0.1"]
    if cfg.mailto:
        parts.append(f"(mailto:{cfg.mailto})")
    return " ".join(parts)


def _get_client() -> httpx.Client:
    """Lazy default getter for scripts and tests that bypass ``configure``."""
    global _client
    if _client is None:
        _client = httpx.Client(
            base_url="https://api.crossref.org",
            headers={"User-Agent": "Carrel/0.1", "Accept": "application/json"},
            timeout=30.0,
        )
    return _client


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------

_BACKOFF_SECONDS = 1.5  # 1.5s, 3.0s, 4.5s — gentle, Crossref isn't an OA-tier budget


def _backoff(attempt: int) -> float:
    return _BACKOFF_SECONDS * (attempt + 1)


def _retry_after(resp: httpx.Response) -> float | None:
    """Parse ``Retry-After`` (seconds, integer or HTTP-date)."""
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        # HTTP-date form: rare from Crossref, just bail.
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@throttle_aware(crossref_throttle, sentinel=[])
def search_papers(
    query: str,
    *,
    limit: int = 20,
    year_from: int | None = None,
    year_to: int | None = None,
    sort: str = "relevance",
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Search Crossref for works matching ``query``.

    Returns raw API ``message.items`` dicts (one per work). Callers use the
    ``work_*`` field helpers + :func:`merge.from_crossref_row` to turn them
    into :class:`MutableSearchHit` instances.

    Sort handling:
      * ``relevance`` (default) — omit the ``sort`` param.
      * ``date`` — ``sort=published&order=desc`` (Crossref's "most recent
        first" sort by publication date).
      * ``citations`` — silently ignore. Crossref's ``is-referenced-by-count``
        is unreliable for newly registered DOIs and would mislead; the per-
        source warning would be noise.

    ``year_from`` / ``year_to`` map to Crossref's ``from-pub-date`` /
    ``until-pub-date`` filter (inclusive). Empty list on 404 or empty query.
    Raises :class:`CrossrefError` on repeated transport failure.
    """
    query = (query or "").strip()
    if not query:
        return []

    httpx_client = client or _get_client()
    params: list[tuple[str, str]] = [
        ("query.bibliographic", query),
        ("rows", str(min(max(limit, 1), 100))),
    ]
    if year_from is not None:
        params.append(("filter", f"from-pub-date:{year_from}"))
    if year_to is not None:
        params.append(("filter", f"until-pub-date:{year_to}"))
    if sort == "date":
        params.append(("sort", "published"))
        params.append(("order", "desc"))
    # sort == "citations" is intentionally a no-op (see docstring).

    url = "/works?" + _urlencode(params)
    resp = _get_with_retry(httpx_client, url)
    if resp is None:
        return []
    try:
        payload = resp.json()
    except ValueError as e:
        raise CrossrefError(f"invalid JSON from Crossref search: {e}") from e
    items = (payload.get("message") or {}).get("items") or []
    return items


def fetch_paper(
    doi: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Look up a single Crossref work by its DOI.

    Returns the raw ``message`` dict (the work object itself, not wrapped in
    the ``status/message`` envelope). ``None`` on 404.
    """
    doi = (doi or "").strip()
    if not doi:
        return None
    httpx_client = client or _get_client()
    url = f"/works/{doi}"
    resp = _get_with_retry(httpx_client, url)
    if resp is None:
        return None
    try:
        payload = resp.json()
    except ValueError as e:
        raise CrossrefError(f"invalid JSON from Crossref work lookup: {e}") from e
    return payload.get("message") or None


def _urlencode(params: list[tuple[str, str]]) -> str:
    from urllib.parse import urlencode

    return urlencode(params)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get_with_retry(client: httpx.Client, url: str) -> httpx.Response | None:
    """GET with 429/5xx backoff. Returns None on 404, raises CrossrefError.

    429: records the latch and bails out (returns None) so the search
    endpoint can soft-fail this source without a noisy error. The
    :func:`throttle_aware` decorator on :func:`search_papers` short-circuits
    the next call while the latch is open.

    5xx: retries with backoff and raises after the budget is exhausted —
    there is no latch to consult on the next call, so the caller needs the
    exception to surface the failure.
    """
    max_retries = _cfg.max_retries if _cfg else 3
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = client.get(url)
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(_backoff(attempt))
                continue
            raise CrossrefError(f"Crossref request failed: {e}") from e

        if resp.status_code == 404:
            logger.debug("Crossref 404 for %s", url)
            return None
        if resp.status_code == 429:
            crossref_throttle.record(_retry_after(resp))
            msg = crossref_throttle.message()
            if msg:
                logger.warning("Crossref 429: %s", msg)
            # Don't burn the remaining retries — the latch is now open and
            # the next call short-circuits via @throttle_aware.
            return None
        if resp.status_code in (500, 502, 503, 504):
            last_exc = CrossrefError(f"Crossref HTTP {resp.status_code}")
            if attempt < max_retries - 1:
                time.sleep(_backoff(attempt))
                logger.warning("Crossref %s; sleeping %.1fs",
                               resp.status_code, _backoff(attempt))
                continue
            raise CrossrefError(f"Crossref HTTP {resp.status_code} after retries")
        if resp.status_code >= 400:
            raise CrossrefError(f"Crossref HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    if last_exc:
        raise CrossrefError(f"Crossref request failed after retries: {last_exc}")
    return None


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def work_doi(work: dict[str, Any] | None) -> str | None:
    """Return the bare DOI (Crossref serves ``"10.xxxx/yyyy"`` without prefix)."""
    if not work:
        return None
    return work.get("DOI") or None


def work_title(work: dict[str, Any] | None) -> str:
    """Crossref's title is a list of one (or more) candidates; take the first."""
    if not work:
        return "(untitled)"
    titles = work.get("title") or []
    title = titles[0].strip() if titles else ""
    return title or "(untitled)"


def work_authors(work: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return ``[{"name", "orcid", "affiliation"}, ...]`` for each Crossref author.

    Crossref gives ``given`` + ``family`` separately and an optional
    ``ORCID`` (already URL-stripped on most responses) and a list of
    ``affiliation`` objects with ``name``. We join the first affiliation
    and surface the ORCID as a bare id when present.
    """
    out: list[dict[str, Any]] = []
    if not work:
        return out
    for a in work.get("author") or []:
        if not isinstance(a, dict):
            continue
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        name = f"{given} {family}".strip() or (a.get("name") or "").strip()
        if not name:
            continue
        orcid_raw = a.get("ORCID")
        orcid = _strip_orcid_url(orcid_raw) if orcid_raw else None
        affiliations = a.get("affiliation") or []
        first_aff = affiliations[0] if affiliations and isinstance(affiliations[0], dict) else None
        aff_name = first_aff.get("name") if first_aff else None
        out.append(
            {
                "name": name,
                "orcid": orcid,
                "affiliation": aff_name,
            }
        )
    return out


def work_venue(work: dict[str, Any] | None) -> str | None:
    """The journal / proceedings name from ``container-title[0]``."""
    if not work:
        return None
    containers = work.get("container-title") or []
    if not containers:
        return None
    name = (containers[0] or "").strip()
    return name or None


def work_venue_type(work: dict[str, Any] | None) -> str | None:
    """Map Crossref's ``type`` to the coarse bucket the UI uses.

    Mapping:
      * ``journal-article`` → ``"journal"``
      * ``proceedings-article`` → ``"conference"``
      * ``book-chapter`` / ``book-part`` / ``book`` → ``"book"``
      * ``posted-content`` → ``"repository"`` (preprints / Zenodo)
      * everything else → raw string.
    """
    if not work:
        return None
    raw = (work.get("type") or "").strip()
    if not raw:
        return None
    mapping = {
        "journal-article": "journal",
        "proceedings-article": "conference",
        "book-chapter": "book",
        "book-part": "book",
        "book": "book",
        "posted-content": "repository",
    }
    return mapping.get(raw, raw)


_JATS_TAG = re.compile(r"<[^>]+>")


def work_abstract(work: dict[str, Any] | None) -> str | None:
    """Strip JATS XML tags from ``abstract`` and return the plain text.

    Crossref returns abstracts like ``"<jats:p>We present ...</jats:p>"``.
    A bare ``re.sub(r"<[^>]+>", "", ...)`` is enough — there is no nested
    inline markup to preserve.
    """
    if not work:
        return None
    raw = work.get("abstract")
    if not raw:
        return None
    text = _JATS_TAG.sub("", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# Publication date priority mirrors OpenAlex: prefer the printed date, then
# online, then issued, then created. Each Crossref date block has
# ``"date-parts": [[YYYY, M, D]]`` (the inner list may be partial).
_DATE_BLOCKS = ("published-print", "published-online", "issued", "created")


def work_publication_date(work: dict[str, Any] | None) -> str | None:
    """Return ``"YYYY-MM-DD"`` (or ``"YYYY"`` when month/day missing)."""
    if not work:
        return None
    for block in _DATE_BLOCKS:
        b = work.get(block)
        if not isinstance(b, dict):
            continue
        parts_list = b.get("date-parts") or []
        if not parts_list or not isinstance(parts_list[0], list):
            continue
        parts = parts_list[0]
        if not parts:
            continue
        try:
            year = int(parts[0])
        except (TypeError, ValueError):
            continue
        month = int(parts[1]) if len(parts) > 1 and parts[1] is not None else None
        day = int(parts[2]) if len(parts) > 2 and parts[2] is not None else None
        if month and day:
            return f"{year:04d}-{month:02d}-{day:02d}"
        if month:
            return f"{year:04d}-{month:02d}"
        return f"{year:04d}"
    # Last-ditch: ``issued.year`` (older Crossref records without date-parts).
    issued = work.get("issued") or {}
    year = issued.get("year") if isinstance(issued, dict) else None
    if year:
        try:
            return f"{int(year):04d}"
        except (TypeError, ValueError):
            return None
    return None


def work_pdf_url(work: dict[str, Any] | None) -> str | None:
    """Pick the best publisher PDF URL from the work's ``link[]`` array.

    Heuristic: prefer links with ``content-version`` in ``{"vor", "am"}`` AND
    (``URL`` ends in ``.pdf`` OR host contains ``arxiv.org/pdf``). This
    rejects the publisher HTML landing pages Crossref often mislabels as
    PDFs and the ``content-version=unspecified`` landing-page rows.
    """
    if not work:
        return None
    for link in work.get("link") or []:
        if not isinstance(link, dict):
            continue
        url = (link.get("URL") or "").strip()
        if not url:
            continue
        content_version = (link.get("content-version") or "").lower()
        if content_version not in ("vor", "am"):
            continue
        if url.lower().endswith(".pdf"):
            return url
        if "arxiv.org/pdf" in url.lower():
            return url
    return None


_ARXIV_DOI = re.compile(r"^10\.48550/arXiv\.(.+)$", re.IGNORECASE)


def work_arxiv_id(work: dict[str, Any] | None) -> str | None:
    """Extract a bare arXiv id (e.g. ``"2401.00001"``) from a Crossref work.

    Crossref's arXiv proxy DOIs are of the form
    ``10.48550/arXiv.2401.00001`` (sometimes ``v1`` / ``v2``). We pull the
    suffix and strip the version for stable identity in the merge layer.
    """
    if not work:
        return None
    doi = work_doi(work)
    if not doi:
        return None
    m = _ARXIV_DOI.match(doi.strip())
    if not m:
        return None
    raw = m.group(1).strip()
    return re.sub(r"v\d+$", "", raw) or None


def _strip_orcid_url(value: str | None) -> str | None:
    """Crossref serves ORCIDs as ``https://orcid.org/0000-0000-0000-0000`` —
    we want the bare id.
    """
    if not value:
        return None
    v = value.strip()
    if "orcid.org/" in v.lower():
        v = v.split("orcid.org/")[-1]
    return v or None


