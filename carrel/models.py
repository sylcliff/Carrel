"""SQLModel ORM models.

Naming convention: tables are lowercase plural; columns are snake_case.
JSONB columns are typed as `dict | list | None` and serialized by SQLModel.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from pgvector.sqlalchemy import Vector
from pgvector.sqlalchemy.halfvec import HALFVEC
from sqlalchemy import Column, Date, DateTime, Index, Text
from sqlmodel import JSON, Field, SQLModel

# Make the Vector column a no-op on non-PostgreSQL dialects (so M1 can run
# against SQLite for smoke testing). On PostgreSQL it is a real vector column;
# on SQLite we use JSON so lists round-trip through sqlite3.
VectorType = Vector(2048).with_variant(JSON(), "sqlite")

# Half-precision vector (fp16) for the wiki layer. pgvector's HNSW caps the
# `vector` type at 2000 dims, which our 2048-dim embedding model exceeds;
# `halfvec` supports up to 4000 dims so it can carry a real HNSW index. The
# existing chunks table stays on Vector(2048) (sequential scan) to avoid a
# data migration. SQLite falls back to JSON like VectorType.
HalfvecType = HALFVEC(2048).with_variant(JSON(), "sqlite")


# ------------------ Enums (stored as VARCHAR) ------------------


class PaperStatus(str, Enum):
    pending = "pending"          # metadata only, awaiting processing
    pdf_ready = "pdf_ready"      # PDF downloaded
    parsed = "parsed"            # PDF -> markdown done
    summarized = "summarized"    # LLM TLDR/abstract done
    ready = "ready"              # chunked + embedded
    failed = "failed"            # permanent failure (after retries)


class OAStatus(str, Enum):
    oa = "oa"                    # open access PDF available
    closed = "closed"            # paywalled, no PDF cached
    none = "none"                # no PDF info
    institutional = "institutional"  # fetched via the institutional SSH jump host


class SourceKind(str, Enum):
    arxiv = "arxiv"
    openalex = "openalex"
    both = "both"


class JobKind(str, Enum):
    sync = "sync"
    download = "download"
    parse = "parse"
    summarize = "summarize"
    topics = "topics"  # LLM topic classification from metadata
    authors_backfill = "authors_backfill"  # resolve author A-IDs from OpenAlex
    embed = "embed"
    citations = "citations"  # refresh Semantic Scholar citation count + citing list
    # Download PDFs for closed papers via the institutional SSH jump host.
    remote_fill = "remote_fill"
    # Check an arXiv paper for a published journal version (and fetch it).
    publication_check = "publication_check"
    # Compile the LLM wiki (batch across scholars/concepts/questions).
    wiki_compile = "wiki_compile"
    # Force-recompile a single wiki page.
    wiki_recompile = "wiki_recompile"


class WikiKind(str, Enum):
    concept = "concept"
    scholar = "scholar"
    question = "question"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


# ------------------ Tables ------------------


class Paper(SQLModel, table=True):
    __tablename__ = "papers"

    # Primary key: OpenAlex Work ID (e.g. "W2741809807"); fallback "arxiv:2301.12345"
    id: str = Field(primary_key=True, max_length=64)
    id_kind: str = Field(max_length=16)  # "openalex" | "arxiv"

    title: str
    abstract: str | None = None
    publication_date: date | None = Field(default=None, sa_column=Column(Date))
    venue: str | None = None  # display name (e.g. "Nature")

    doi: str | None = Field(default=None, index=True, max_length=255)
    arxiv_id: str | None = Field(default=None, index=True, max_length=64)

    pdf_url: str | None = None
    pdf_path: str | None = None  # relative to storage.root; always the active PDF
    md_path: str | None = None
    oa_status: str = Field(default=OAStatus.none.value, max_length=16)
    source: str = Field(default=SourceKind.openalex.value, max_length=16)

    # Where the active PDF came from: "oa" | "arxiv" | "institutional" | "journal".
    pdf_origin: str | None = Field(default=None, max_length=16)
    # Journal DOI for an arXiv paper that has been formally published.
    journal_doi: str | None = Field(default=None, index=True, max_length=255)
    # Named PDF variants on disk, e.g.
    # {"arxiv": "papers/<slug>/arxiv.pdf", "journal": "papers/<slug>/journal.pdf"}.
    # paper.pdf/pdf_path remains the active (parsed) file.
    pdf_files: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # Last time we queried S2/OA for a published version of an arXiv paper.
    published_checked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )

    status: str = Field(default=PaperStatus.pending.value, max_length=16, index=True)
    error: str | None = None

    # Library membership. Sync/import-discover flows store fetched candidates
    # with in_library=False (the inbox); the user explicitly imports a paper,
    # flipping this to True. `discarded` hides an inbox row without deleting it
    # (a deliberate import revives it). `discovered_at` stamps when a sync first
    # surfaced the paper, independent of created_at.
    in_library: bool = Field(default=True, index=True)
    discarded: bool = Field(default=False, index=True)
    discovered_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )

    # User annotations (M7): a star, a single long Markdown note per paper.
    favorite: bool = Field(default=False, index=True)
    notes_markdown: str | None = Field(default=None, sa_column=Column(Text))

    # AI outputs (M4)
    tldr_en: str | None = None
    tldr_zh: str | None = None
    summary_zh: str | None = None
    keywords: list[str] | None = Field(default=None, sa_column=Column(JSON))

    # Raw OpenAlex JSON for debugging / future enrichment
    raw_meta: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))

    # Authors as [{name, openalex_author_id, affiliation}]
    authors: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))

    # Citations (Semantic Scholar). `citing_papers` is a capped list of
    # {title, year, doi, arxiv_id, s2_paper_id}; the authoritative count is
    # `citation_count` (may exceed the stored list length). `references` is
    # the papers this paper cites (its bibliography), same item shape.
    s2_paper_id: str | None = Field(default=None, index=True, max_length=64)
    citation_count: int | None = None
    influential_citation_count: int | None = None
    reference_count: int | None = None
    citing_papers: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    references: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    citations_updated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class Chunk(SQLModel, table=True):
    __tablename__ = "chunks"

    id: int | None = Field(default=None, primary_key=True)
    paper_id: str = Field(foreign_key="papers.id", index=True, max_length=64)
    chunk_index: int
    heading: str | None = None
    content_md: str = Field(sa_column=Column(Text))
    token_count: int | None = None
    # Vector column. Dim is set by the model and must match embeddings.dim.
    # pgvector expects a Python list[float].
    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(VectorType),  # dim will be replaced by migration if changed
    )


class Subscription(SQLModel, table=True):
    __tablename__ = "subscriptions"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(max_length=32)  # "keyword" | "author" | "venue" | "arxiv_category"
    value: str
    label: str | None = None
    enabled: bool = True
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    __table_args__ = (Index("ix_subscriptions_kind_value", "kind", "value", unique=True),)


class Tag(SQLModel, table=True):
    """A user-created label. Many-to-many with Paper via PaperTag."""

    __tablename__ = "tags"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True, max_length=100)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PaperTag(SQLModel, table=True):
    """Association row between papers and tags (composite primary key)."""

    __tablename__ = "paper_tags"

    paper_id: str = Field(foreign_key="papers.id", primary_key=True, max_length=64)
    tag_id: int = Field(foreign_key="tags.id", primary_key=True)

    # The composite PK already indexes paper_id (leftmost prefix); add an index
    # on tag_id for reverse lookups (deleting a tag, counting papers per tag).
    __table_args__ = (Index("ix_paper_tags_tag_id", "tag_id"),)


class Topic(SQLModel, table=True):
    """A system-generated research theme (LLM-assigned, shared across papers).

    Distinct from user ``Tag``: topics form a controlled vocabulary the
    classifier reuses and grows, enabling cross-paper browsing by topic.
    """

    __tablename__ = "topics"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True, max_length=100)
    description: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PaperTopic(SQLModel, table=True):
    """Association row between papers and topics (composite primary key)."""

    __tablename__ = "paper_topics"

    paper_id: str = Field(foreign_key="papers.id", primary_key=True, max_length=64)
    topic_id: int = Field(foreign_key="topics.id", primary_key=True)

    __table_args__ = (Index("ix_paper_topics_topic_id", "topic_id"),)


class ChatMessage(SQLModel, table=True):
    """One turn in a paper's persisted RAG-chat transcript.

    Stored server-side so the conversation follows the user across devices and
    browsers (unlike a localStorage-only transcript). Ordered by ``id``; the
    whole transcript for a paper is replaced on each save (whole-document PUT,
    like notes).
    """

    __tablename__ = "chat_messages"

    id: int | None = Field(default=None, primary_key=True)
    paper_id: str = Field(foreign_key="papers.id", index=True, max_length=64)
    role: str = Field(max_length=16)  # "user" | "assistant"
    content: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class WikiPage(SQLModel, table=True):
    """A compiled wiki page — disk Markdown is the source of truth; this is the index.

    One row per ``(kind, slug)``. The file lives at ``<storage.root>/<path>``
    (e.g. ``wiki/scholars/A5013....md``) and can be rebuilt from disk with
    ``reindex_wiki``. Frontmatter fields are mirrored here so list/filter views
    never touch the filesystem.
    """

    __tablename__ = "wiki_pages"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(max_length=16, index=True)  # WikiKind.value
    slug: str = Field(max_length=200, index=True)
    title: str = Field(max_length=300)

    # Scholar pages only: the OpenAlex A-ID joining the /scholars aggregation.
    scholar_aid: str | None = Field(default=None, index=True, max_length=32)
    # Question pages only: open|contested|partially_solved|resolved.
    question_status: str | None = Field(default=None, max_length=24)

    # Storage-root-relative path to the Markdown file.
    path: str = Field(max_length=500)
    # sha256 of the file bytes at the last sync.
    checksum: str | None = Field(default=None, max_length=64)

    # Frontmatter mirrors (list/filter without file IO).
    summary: str | None = Field(default=None, sa_column=Column(Text))
    tags: list[str] | None = Field(default=None, sa_column=Column(JSON))
    links_out: list[str] | None = Field(default=None, sa_column=Column(JSON))
    links_in_count: int = Field(default=0)
    source_paper_ids: list[str] | None = Field(default=None, sa_column=Column(JSON))

    # 0..1 corroboration score and count of distinct backing papers.
    confidence: float = Field(default=0.0, index=True)
    evidence_count: int = Field(default=0)
    embedding: list[float] | None = Field(default=None, sa_column=Column(HalfvecType))

    compiled_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    __table_args__ = (
        Index("ix_wiki_pages_kind_slug", "kind", "slug", unique=True),
    )


class WikiSource(SQLModel, table=True):
    """Assertion-level provenance: a wiki page backed by a paper (and chunk).

    Scholar pages cite the paper at the abstract level (chunk_id=NULL); concept
    and question pages pin claims to a specific chunk. ``role`` classifies the
    evidence for question pages (support/contradict/context).
    """

    __tablename__ = "wiki_sources"

    id: int | None = Field(default=None, primary_key=True)
    wiki_page_id: int = Field(
        foreign_key="wiki_pages.id", index=True, ondelete="CASCADE"
    )
    paper_id: str = Field(
        foreign_key="papers.id", index=True, max_length=64, ondelete="CASCADE"
    )
    chunk_id: int | None = Field(
        default=None, foreign_key="chunks.id", ondelete="CASCADE"
    )
    heading: str | None = Field(default=None, max_length=300)
    quote: str | None = Field(default=None, sa_column=Column(Text))
    role: str = Field(default="context", max_length=16)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    __table_args__ = (
        Index("ix_wiki_sources_page_paper", "wiki_page_id", "paper_id"),
    )


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(max_length=32, index=True)
    status: str = Field(default=JobStatus.queued.value, max_length=16, index=True)
    message: str | None = None
    stats: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    started_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
