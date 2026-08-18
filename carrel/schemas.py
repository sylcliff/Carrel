"""Pydantic schemas for the public API (separate from SQLModel tables)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

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
    created_at: datetime
    updated_at: datetime


# -------- Citations (Semantic Scholar) --------


class CitationItem(BaseModel):
    """One citing paper, with library-membership resolved by the API."""

    title: str | None = None
    year: int | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    s2_paper_id: str | None = None
    in_library: bool = False
    paper_id: str | None = None  # Carrel Paper.id when in_library


class CitationListOut(BaseModel):
    paper_id: str
    citation_count: int | None = None
    influential_citation_count: int | None = None
    reference_count: int | None = None
    updated_at: datetime | None = None
    truncated: bool = False  # stored list hit the citations_limit cap
    citing: list[CitationItem] = []


class CitationRefreshRequest(BaseModel):
    background: bool = False


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
