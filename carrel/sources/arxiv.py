"""arXiv fetcher via the public Atom API.

Adapted from galleonli/paper-agent (MIT): the 429 exponential-backoff loop,
paging, and `(query) AND (cat:...)` search composition are reused. Output
shape is a dict (not the original Paper dataclass) so normalize.py can hand
it off to OpenAlex for disambiguation and OA-PDF lookup.

Rate limit courtesy: arXiv asks for >=3s between calls and identifying User-Agent.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

ARXIV_API_BASE = "https://export.arxiv.org/api/query"
ARXIV_MAX_RETRIES = 3
ARXIV_BASE_WAIT_SECONDS = 20
ARXIV_USER_AGENT = "carrel/0.1 (https://github.com/you/carrel)"

ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"


@dataclass(slots=True)
class ArxivEntry:
    """Raw arXiv Atom entry, before normalization."""

    arxiv_id: str  # e.g. "2301.12345"
    title: str
    summary: str
    authors: list[str]
    categories: list[str]
    updated: str  # ISO date string from arXiv
    abs_url: str
    pdf_url: str | None


# ---------------------------------------------------------------------------
# HTTP helper with 429 exponential backoff
# ---------------------------------------------------------------------------


def _get_with_retry(
    client: httpx.Client,
    url: str,
    timeout: int,
    max_retries: int = ARXIV_MAX_RETRIES,
    base_wait: float = ARXIV_BASE_WAIT_SECONDS,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = client.get(url, timeout=timeout)
            # arXiv sometimes returns 200 OK with a plain-text "Rate exceeded."
            # body instead of a 429. Treat it like a 429 (retry/backoff).
            if resp.status_code == 200 and resp.text.lstrip().lower().startswith(
                "rate exceeded"
            ):
                if attempt < max_retries - 1:
                    wait = base_wait * (2**attempt)
                    logger.warning("arXiv rate body; sleeping %.0fs (attempt %d)", wait, attempt + 1)
                    time.sleep(wait)
                    continue
                raise httpx.HTTPStatusError(
                    "arXiv rate limit exceeded (200 body)",
                    request=resp.request,
                    response=resp,
                )
            if resp.status_code == 429 and attempt < max_retries - 1:
                wait = base_wait * (2**attempt)
                logger.warning("arXiv 429; sleeping %.0fs (attempt %d)", wait, attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as e:
            last_exc = e
            status = getattr(e, "response", None) and e.response.status_code
            if status == 429 and attempt < max_retries - 1:
                wait = base_wait * (2**attempt)
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("arXiv request failed after retries")


# ---------------------------------------------------------------------------
# Atom parsing
# ---------------------------------------------------------------------------


def _text(el: ET.Element | None, default: str = "") -> str:
    if el is not None and el.text:
        return el.text.strip()
    return default


def _parse_id_from_abs_url(url: str) -> str:
    m = re.search(r"arxiv\.org/abs/([^/?]+)", url, re.I)
    return m.group(1) if m else url


def _parse_entry(entry: ET.Element) -> ArxivEntry | None:
    ns = {"atom": ATOM_NS, "arxiv": ARXIV_NS}
    id_el = entry.find("atom:id", ns)
    if id_el is None or not id_el.text:
        return None
    abs_url = id_el.text.strip()
    arxiv_id = _parse_id_from_abs_url(abs_url)

    title = _text(entry.find("atom:title", ns)).replace("\n", " ").strip()
    if not title:
        return None

    summary = _text(entry.find("atom:summary", ns)).replace("\n", " ").strip()
    authors = [
        _text(a.find("atom:name", ns))
        for a in entry.findall("atom:author", ns)
        if _text(a.find("atom:name", ns))
    ]
    categories = [
        cat.get("term")
        for cat in entry.findall("atom:category", ns)
        if cat.get("term") and "arxiv.org" in (cat.get("scheme") or "")
    ]
    updated = _text(entry.find("atom:updated", ns))

    pdf_url: str | None = None
    for link in entry.findall("atom:link", ns):
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            pdf_url = (link.get("href") or "").strip() or None
            break

    return ArxivEntry(
        arxiv_id=arxiv_id,
        title=title,
        summary=summary,
        authors=authors,
        categories=categories,
        updated=updated,
        abs_url=abs_url,
        pdf_url=pdf_url,
    )


# ---------------------------------------------------------------------------
# Search query composition
# ---------------------------------------------------------------------------


def _build_category_part(allow: list[str], deny: list[str]) -> str:
    if not allow:
        return "all:all"
    terms = [f"cat:{c.strip()}" for c in allow if c.strip()]
    if not terms:
        return "all:all"
    query = " OR ".join(terms)
    if deny:
        deny_terms = " ".join(f"ANDNOT cat:{c.strip()}" for c in deny if c.strip())
        query = f"{query} {deny_terms}"
    return query


def _one_query(
    client: httpx.Client,
    search_query: str,
    page_size: int,
    start: int,
    sort_by: str,
    timeout: int,
) -> list[ArxivEntry]:
    params = {
        "search_query": search_query,
        "start": str(start),
        "max_results": str(page_size),
        "sortBy": sort_by,
        "sortOrder": "descending",
    }
    url = f"{ARXIV_API_BASE}?{urlencode(params)}"
    resp = _get_with_retry(client, url, timeout=timeout)
    root = ET.fromstring(resp.content)
    entries = root.findall("atom:entry", {"atom": ATOM_NS})
    return [e for e in (_parse_entry(el) for el in entries) if e is not None]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_recent(
    *,
    lookback_hours: int = 24,
    categories: list[str] | None = None,
    deny_categories: list[str] | None = None,
    queries: list[str] | None = None,
    max_results: int = 200,
    timeout: int = 30,
    delay_between_requests: float = 3.0,
) -> list[ArxivEntry]:
    """Fetch papers from arXiv submitted within the last `lookback_hours` hours.

    If `queries` is non-empty, runs one search per query combined with the
    category filter, then merges + dedupes by arXiv ID. Otherwise uses
    category-only (or "all:all" if categories is also empty).
    """
    categories = categories or []
    deny_categories = deny_categories or []
    cat_part = _build_category_part(categories, deny_categories)

    headers = {"User-Agent": ARXIV_USER_AGENT}
    out: list[ArxivEntry] = []
    seen_ids: set[str] = set()

    # arXiv does not support pure "last N hours" filtering on the Atom API;
    # we sort by submittedDate desc and stop once entries are older than the
    # lookback window.
    cutoff = datetime.utcnow().timestamp() - lookback_hours * 3600

    page_size = min(max_results, 200)

    with httpx.Client(headers=headers) as client:
        if queries:
            per_query = max(10, max_results // max(len(queries), 1))
            for i, q in enumerate(queries):
                q = (q or "").strip()
                if not q:
                    continue
                combined = f"({q}) AND ({cat_part})" if cat_part != "all:all" else q
                _drain(
                    client,
                    combined,
                    page_size,
                    per_query,
                    cutoff,
                    seen_ids,
                    out,
                    sort_by="relevance",
                    timeout=timeout,
                )
                if len(out) >= max_results:
                    break
                if i < len(queries) - 1:
                    time.sleep(delay_between_requests)
        else:
            _drain(
                client,
                cat_part,
                page_size,
                max_results,
                cutoff,
                seen_ids,
                out,
                sort_by="submittedDate",
                timeout=timeout,
            )

    return out[:max_results]


def _drain(
    client: httpx.Client,
    search_query: str,
    page_size: int,
    max_results: int,
    cutoff_ts: float,
    seen_ids: set[str],
    out: list[ArxivEntry],
    *,
    sort_by: str,
    timeout: int,
    delay_between_requests: float = 3.0,
) -> None:
    """Pull pages until we hit the cutoff, the cap, or run out of results."""
    start = 0
    while len(out) < max_results:
        batch = _one_query(client, search_query, page_size, start, sort_by, timeout)
        if not batch:
            return
        stop = False
        for e in batch:
            try:
                ts = datetime.fromisoformat(e.updated.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = cutoff_ts + 1  # keep if we can't parse
            if ts < cutoff_ts:
                stop = True
                break
            if e.arxiv_id not in seen_ids:
                seen_ids.add(e.arxiv_id)
                out.append(e)
                if len(out) >= max_results:
                    return
        if stop or len(batch) < page_size:
            return
        start += page_size
        time.sleep(delay_between_requests)


# ---------------------------------------------------------------------------
# Fetch a single arXiv ID (for normalize.py to call)
# ---------------------------------------------------------------------------


def fetch_one(arxiv_id: str, *, timeout: int = 15) -> ArxivEntry | None:
    """Fetch a single paper by arXiv ID; used to enrich after OpenAlex lookup."""
    arxiv_id = arxiv_id.strip()
    if not arxiv_id:
        return None
    url = f"{ARXIV_API_BASE}?{urlencode({'id_list': arxiv_id})}"
    with httpx.Client(headers={"User-Agent": ARXIV_USER_AGENT}) as client:
        try:
            resp = _get_with_retry(client, url, timeout=timeout, max_retries=2, base_wait=10)
        except httpx.HTTPError:
            return None
    root = ET.fromstring(resp.content)
    entries = root.findall("atom:entry", {"atom": ATOM_NS})
    if not entries:
        return None
    return _parse_entry(entries[0])


# ---------------------------------------------------------------------------
# Ad-hoc search (used by the /search endpoint)
# ---------------------------------------------------------------------------


def search(
    query: str,
    *,
    limit: int = 20,
    categories: list[str] | None = None,
    timeout: int = 30,
) -> list[ArxivEntry]:
    """Search arXiv by free-text query with relevance ranking.

    `query` is passed through verbatim, so arXiv advanced syntax
    (``ti:"..."``, ``au:Name``, ``cat:cs.LG``, ``AND/OR``) is supported.
    `categories`, when given, ANDs a category filter onto the query. No
    lookback cutoff — that's subscription-specific and lives in
    :func:`fetch_recent`.
    """
    query = (query or "").strip()
    if not query:
        return []

    if categories:
        cat_part = _build_category_part(categories, [])
        if cat_part != "all:all":
            query = f"({query}) AND ({cat_part})"

    page_size = min(max(limit, 10), 50)
    with httpx.Client(headers={"User-Agent": ARXIV_USER_AGENT}) as client:
        try:
            entries = _one_query(
                client,
                search_query=query,
                page_size=page_size,
                start=0,
                sort_by="relevance",
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            logger.warning("arXiv search failed for %r: %s", query, e)
            return []
    return entries[:limit]
