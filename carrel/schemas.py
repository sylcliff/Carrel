"""Pydantic schemas for the public API (separate from SQLModel tables)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

# -------- Health / meta --------


class HealthResponse(BaseModel):
    status: str
    version: str
    db: str
    mineru: str


# -------- Papers --------


class PaperSummary(BaseModel):
    """Compact representation for card views / lists."""

    id: str
    title: str
    venue: str | None = None
    publication_date: date | None = None
    authors: list[str] = []
    oa_status: str
    status: str
    tldr_zh: str | None = None
    tldr_en: str | None = None
    keywords: list[str] = []
    source: str
    citation_count: int | None = None
    in_library: bool = True
    discovered_at: datetime | None = None
    favorite: bool = False
    tags: list[str] = []


class PaperDetail(PaperSummary):
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    pdf_url: str | None = None
    pdf_path: str | None = None
    md_path: str | None = None
    summary_zh: str | None = None
    error: str | None = None
    influential_citation_count: int | None = None
    reference_count: int | None = None
    citations_updated_at: datetime | None = None
    notes_markdown: str | None = None
    created_at: datetime
    updated_at: datetime


# -------- User annotations (favorites / notes / tags) --------


class FavoriteIn(BaseModel):
    favorite: bool


class FavoriteOut(BaseModel):
    id: str
    favorite: bool


class NotesIn(BaseModel):
    notes_markdown: str


class NotesOut(BaseModel):
    id: str
    notes_markdown: str | None
    updated_at: datetime


class TagIn(BaseModel):
    name: str


class TagOut(BaseModel):
    id: int
    name: str


class TagWithCount(TagOut):
    paper_count: int


# -------- Citations (Semantic Scholar) --------


class CitationItem(BaseModel):
    """One citing paper, with library-membership resolved by the API."""

    title: str | None = None
    year: int | None = None
    venue: str | None = None  # journal / conference / repository name
    doi: str | None = None
    arxiv_id: str | None = None
    s2_paper_id: str | None = None
    openalex_id: str | None = None
    in_library: bool = False
    paper_id: str | None = None  # Carrel Paper.id when in_library


class CitationListOut(BaseModel):
    paper_id: str
    citation_count: int | None = None
    influential_citation_count: int | None = None
    reference_count: int | None = None
    updated_at: datetime | None = None
    truncated: bool = False  # true when more pages are available
    citing: list[CitationItem] = []
    next_offset: int | None = None  # absolute offset for the next page; null = end
    source: str = "cache"  # "cache" (sorted DB list) or "openalex" (live page)
    cached_count: int = 0  # size of the in-DB citation list at request time


class CitationRefreshRequest(BaseModel):
    background: bool = False


class ReferenceListOut(BaseModel):
    paper_id: str
    reference_count: int | None = None
    updated_at: datetime | None = None
    references: list[CitationItem] = []


# -------- Search (M5) --------


class SearchResultIds(BaseModel):
    """Every identifier we know for a paper across sources. All optional."""

    openalex: str | None = None
    doi: str | None = None
    arxiv: str | None = None
    s2: str | None = None


class SearchResultItem(BaseModel):
    """One paper in a merged /search response.

    ``sources`` lists which backends contributed this row — any subset of
    ``"library" | "openalex" | "semantic_scholar" | "arxiv"`` — so the UI can
    render badges and filter client-side. When ``in_library`` is True,
    ``library_id`` is the Carrel Paper.id for navigation and ``status`` its
    pipeline status.
    """

    title: str
    authors: list[str] = []
    abstract: str | None = None
    venue: str | None = None
    venue_type: str | None = None
    publication_date: str | None = None
    citation_count: int | None = None
    tldr: str | None = None
    pdf_url: str | None = None
    snippet: str | None = None
    ids: SearchResultIds = Field(default_factory=SearchResultIds)
    sources: list[str] = []
    in_library: bool = False
    library_id: str | None = None
    status: str | None = None


class SearchResponse(BaseModel):
    query: str  # the query we actually searched with (post-correction)
    corrected_from: str | None = None  # original user query if we spell-fixed it
    results: list[SearchResultItem] = []
    # Per-source soft failures: strings like "semantic_scholar: timeout". Empty
    # when all sources responded.
    warnings: list[str] = []


class ImportPaperIn(BaseModel):
    openalex_id: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    s2: str | None = None
    title: str | None = None  # display title; used as a loose-match fallback


class ImportPaperOut(BaseModel):
    id: str
    created: bool  # True if newly inserted, False if already existed


# -------- Subscriptions --------


class SubscriptionIn(BaseModel):
    kind: str  # "keyword" | "author" | "venue" | "arxiv_category"
    value: str
    label: str | None = None
    enabled: bool = True


class SubscriptionOut(SubscriptionIn):
    id: int
    created_at: datetime


# -------- Sync / Jobs --------


class JobOut(BaseModel):
    id: int
    kind: str
    status: str
    message: str | None = None
    stats: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class SyncRequest(BaseModel):
    """Optional trigger parameters for a manual sync."""

    lookback_hours: int = 24
    sources: list[str] | None = None  # None = all enabled
    background: bool = False  # if true, fire-and-forget; else wait for result


class ProcessRequest(BaseModel):
    """Trigger PDF download + MinerU parse.

    If ``paper_id`` is given, only that paper is processed; otherwise up to
    ``limit`` pending/failed papers are processed in a batch.
    """

    paper_id: str | None = None
    limit: int = 10
    background: bool = False


class EmbedRequest(BaseModel):
    """Trigger chunk + embed for one paper or a batch (M5)."""

    paper_id: str | None = None
    limit: int = 20
    background: bool = False


class SummarizeRequest(BaseModel):
    """Trigger LLM summary for one paper or a batch (M4).

    If ``paper_id`` is given, only that paper is summarized; otherwise up to
    ``limit`` parsed-but-unsummarized papers are processed. ``force=True``
    regenerates fields that already exist (e.g. an S2-sourced ``tldr_en``).
    """

    paper_id: str | None = None
    limit: int = 20
    background: bool = False
    force: bool = False


# -------- Search (M5) full-text --------


class SemanticSearchHit(BaseModel):
    """One chunk match from the full-text vector index."""

    paper_id: str
    chunk_index: int
    heading: str | None = None
    snippet: str  # matched chunk excerpt (or excerpt around the query)
    score: float  # cosine similarity, 0..1


class SemanticSearchResult(BaseModel):
    """A library paper with the chunks that matched the query."""

    id: str
    title: str
    venue: str | None = None
    publication_date: str | None = None
    authors: list[str] = []
    doi: str | None = None
    arxiv_id: str | None = None
    status: str | None = None
    best_score: float
    hits: list[SemanticSearchHit] = []  # top-3 chunks per paper


class SemanticSearchResponse(BaseModel):
    query: str  # post-correction query we embedded and searched with
    corrected_from: str | None = None  # original if spelling was fixed
    results: list[SemanticSearchResult] = []
