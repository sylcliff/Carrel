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
    # Set by paper dedup when this row has been merged into another via
    # paper_aliases; user_state is cleared and the row is read through
    # resolve_paper_id(). Distinct from any user-initiated archive.
    merged = "merged"


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
    # Run an LLM-with-tools agent that researches the scholar online
    # (Brave web search) and appends a `## Web research` note to the
    # scholar's preserved <section data-user="true"> block. The note is
    # not part of the LLM-generated sections; the user can edit it
    # freely and recompiles don't clobber it. Triggered manually from
    # the Scholar Detail page; not scheduled.
    wiki_enrich = "wiki_enrich"
    # Cluster same-named A-IDs and apply high-confidence scholar merges.
    scholar_dedup = "scholar_dedup"
    # Cluster near-duplicate paper records (DOI / arXiv / s2 / journal_doi
    # bridge / LLM judge) and apply high-confidence paper merges into
    # paper_aliases. UI surface is the Library page "Duplicates" panel.
    paper_dedup = "paper_dedup"
    # Per-paper LLM extraction of concepts + open questions from the parsed
    # markdown. Feeds the concept/question wiki compilations.
    paper_extract = "paper_extract"
    # Bulk fetch + cache an OpenAlex author's full works list. Triggered when a
    # scholar page is first visited (lazy load) or via manual "Refresh from
    # OpenAlex". Reuses the Job / BackgroundTasks / JobOut shape that
    # authors_backfill and citations refresh use.
    scholar_works_sync = "scholar_works_sync"


class WikiKind(str, Enum):
    concept = "concept"
    scholar = "scholar"
    question = "question"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


# ------------------ Token usage ------------------


class TokenUsage(SQLModel, table=True):
    """One LLM call's token usage.

    Populated from the ``usage`` block of litellm completions. ``feature``
    is a free-form string naming the calling subsystem (e.g. "summarize",
    "chat", "wiki_compile", "concept_compile"); it powers the by-feature
    breakdown on the Usage page.
    """

    __tablename__ = "token_usage"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    model: str = Field(max_length=120, index=True)
    feature: str = Field(max_length=64, index=True)
    # Optional context: which job / paper the call belonged to.  Both are
    # nullable so chat calls (no job) and ad-hoc calls can still record.
    job_id: int | None = Field(default=None, index=True)
    paper_id: str | None = Field(default=None, index=True, max_length=64)
    prompt_tokens: int = Field(default=0)
    completion_tokens: int = Field(default=0)
    total_tokens: int = Field(default=0)


class PromptOverride(SQLModel, table=True):
    """Per-feature user override of system + user-template LLM prompts.

    A row exists only when the user has saved an override via the prompt
    editor on the Agent page. Absence means "use the module default".
    ``system`` and ``user_template`` are both nullable so a partial override
    is allowed — the runtime resolver does ``override.<col> or default``.

    The PK is the feature name (matches the ``feature`` column on
    :class:`TokenUsage`); the catalog in :mod:`carrel.prompts` pins the
    valid feature set so the API can 404 on unknown names.

    Read at call time by :mod:`carrel.prompts_runtime` with a 60s in-process
    TTL cache; writes from the editor call :func:`prompts_runtime.invalidate`
    synchronously so the next LLM call sees the new value.
    """

    __tablename__ = "prompt_overrides"

    feature: str = Field(primary_key=True, max_length=64)
    system: str | None = Field(default=None, sa_column=Column(Text))
    user_template: str | None = Field(default=None, sa_column=Column(Text))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


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


class PaperConcept(SQLModel, table=True):
    """One concept the LLM extracted from a single paper's body.

    Compound by ``(paper_id, term_normalized)`` so a paper's "Retrieval-Augmented
    Generation" mention is stored once even if the LLM surfaced it from
    multiple sections.  The display form is preserved verbatim
    (``term_display``) so the wiki page can use the most common surface form.
    """

    __tablename__ = "paper_concepts"

    paper_id: str = Field(foreign_key="papers.id", primary_key=True, max_length=64)
    term_normalized: str = Field(primary_key=True, max_length=200)
    term_display: str = Field(max_length=300)
    # Verbatim span from the paper's body that grounds the extraction. We
    # verify it before write so a hallucinated mention never lands in the DB.
    evidence_quote: str | None = Field(default=None, sa_column=Column(Text))
    # One of METHOD / THEORY / DATASET / DOMAIN / PHENOMENON, set by the
    # extraction LLM.  Nullable: rows from before this column existed have
    # ``category=None``; downstream consumers must treat None as "unknown".
    category: str | None = Field(default=None, max_length=32)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    __table_args__ = (Index("ix_paper_concepts_term", "term_normalized"),)


class PaperQuestion(SQLModel, table=True):
    """One open question the LLM extracted from a single paper's body.

    Shape mirrors :class:`PaperConcept` (compound by
    ``(paper_id, question_normalized)``). Field-level questions are
    deferred — this is per-paper only in v1.
    """

    __tablename__ = "paper_questions"

    paper_id: str = Field(foreign_key="papers.id", primary_key=True, max_length=64)
    question_normalized: str = Field(primary_key=True, max_length=400)
    question_display: str = Field(max_length=600)
    evidence_quote: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    __table_args__ = (Index("ix_paper_questions_question", "question_normalized"),)


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


class WikiChatMessage(SQLModel, table=True):
    """One turn in the global wiki chat transcript (server-persisted).

    There is exactly one conversation about the whole wiki (no per-page
    transcript), so the table has no foreign key to a specific page. The
    whole transcript is replaced on each save (whole-document PUT, like
    :class:`ChatMessage`).
    """

    __tablename__ = "wiki_chat_messages"

    id: int | None = Field(default=None, primary_key=True)
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

    # Stable, kind-qualified identity (e.g. "scholar:A5002874269",
    # "scholar:name:he-li"). Independent of slug/path so that identity
    # changes (A-ID assigned, aliases merged, name spelling normalized)
    # do not break the catalog. The partial unique index over this column
    # (uq_wiki_pages_entity_key_live, created in init_db) guarantees
    # exactly one *live* page per entity; redirect shells are allowed to
    # have the same entity_key as their canonical.
    entity_key: str | None = Field(default=None, index=True, max_length=200)
    # String form of the target entity_key when this row is a redirect
    # shell. Stored as a string (not a self-FK) so that reindex can rebuild
    # the row from disk and cycles never produce a 500.
    redirects_to: str | None = Field(default=None, max_length=200)

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
    # Stub pages have < evidence threshold (e.g. < 3 papers for concepts);
    # the LLM is skipped and a placeholder body is written instead. The
    # partial index makes "find stubs needing a promotion check" cheap.
    stub: bool = Field(default=False, index=True)
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
        # entity_key is already indexed via `Field(index=True)`; a separate
        # redirects_to index helps the resolve_target lookup during reindex.
        Index("ix_wiki_pages_redirects_to", "redirects_to"),
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


class ScholarAlias(SQLModel, table=True):
    """Maps a duplicate OpenAlex Author ID (``alias_aid``) to the canonical one.

    OpenAlex frequently splits one real person across several A-IDs (common for
    Chinese names and early-career authors). The dedup pipeline scores clusters
    of same-named A-IDs and records high-confidence merges here; the scholar
    aggregator resolves every A-ID through this map before grouping so that
    wiki pages and the Scholars page treat the aliases as one person.

    - ``source``: ``auto`` (scoring threshold) | ``user`` (manual accept) |
      ``reject`` (user said "these are different people" — recorded to suppress
      future auto-suggestions).
    - ``confidence``: 0..1 score from the pipeline; 1.0 for user actions.
    - Alias rows are never deleted on re-dedup; a ``reject`` overrides an
      earlier ``auto`` merge.
    """

    __tablename__ = "scholar_aliases"

    id: int | None = Field(default=None, primary_key=True)
    alias_aid: str = Field(index=True, max_length=32)
    canonical_aid: str = Field(index=True, max_length=32)
    display_name: str | None = Field(default=None, max_length=300)
    source: str = Field(default="auto", max_length=16, index=True)
    confidence: float = Field(default=0.0)
    reasons: list[str] | None = Field(default=None, sa_column=Column(JSON))
    note: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    __table_args__ = (
        Index("ix_scholar_aliases_alias_canon", "alias_aid", "canonical_aid", unique=True),
    )


class PaperAlias(SQLModel, table=True):
    """Maps a duplicate paper record (``alias_paper_id``) to the canonical one.

    The pipeline in :mod:`carrel.pipeline.paper_dedup` scores candidate pairs
    and persists high-confidence matches here; the loser row is **kept** in
    ``papers`` (user state is migrated to the canonical and the loser is
    flagged ``status=merged``). Every read path goes through
    :func:`carrel.pipeline.paper_dedup_ops.resolve_paper_id` so the alias is
    transparent to the API consumer, and a merge is reversible by deleting
    the alias row (user-state migration is best-effort, see ``PaperMergeEvent``
    for the snapshot taken at merge time).

    - ``source``: ``auto`` (scoring threshold) | ``user`` (manual accept) |
      ``llm`` (LLM judge returned ``same``) | ``reject`` (user said "these are
      different papers" — recorded to suppress future auto-suggestions).
    - ``confidence``: 0..1 score from the pipeline; 1.0 for user actions.
    - Alias rows are never deleted on re-dedup; a ``reject`` overrides an
      earlier ``auto`` / ``user`` / ``llm`` merge.
    """

    __tablename__ = "paper_aliases"

    id: int | None = Field(default=None, primary_key=True)
    alias_paper_id: str = Field(index=True, max_length=64)
    canonical_paper_id: str = Field(index=True, max_length=64)
    display_label: str | None = Field(default=None, max_length=300)
    source: str = Field(default="auto", max_length=16, index=True)
    confidence: float = Field(default=0.0)
    reasons: list[str] | None = Field(default=None, sa_column=Column(JSON))
    note: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    __table_args__ = (
        Index("ix_paper_aliases_pair", "alias_paper_id", "canonical_paper_id", unique=True),
    )


class PaperDedupVerdict(SQLModel, table=True):
    """Cached LLM judge verdict for a paper pair.

    Populated lazily by :class:`carrel.pipeline.paper_dedup_judge.LLMJudge`.
    Lookup is symmetric: the pair is stored as ``(min(a,b), max(a,b))``. The
    ``prompt_hash`` captures the input + model + prompt_version, so bumping
    ``cfg.llm.paper_dedup_judge_prompt_version`` invalidates cached verdicts
    without touching the ``paper_a_id``/``paper_b_id`` pair.
    """

    __tablename__ = "paper_dedup_verdicts"

    id: int | None = Field(default=None, primary_key=True)
    paper_a_id: str = Field(max_length=64)
    paper_b_id: str = Field(max_length=64)
    prompt_hash: str = Field(max_length=64, index=True)
    model: str = Field(max_length=64)
    prompt_version: int
    verdict: str = Field(max_length=16)  # "same" | "different" | "uncertain"
    confidence: float = Field(default=0.0)
    reasons: list[str] | None = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    __table_args__ = (
        Index("ix_paper_dedup_verdicts_pair", "paper_a_id", "paper_b_id"),
    )


class PaperMergeEvent(SQLModel, table=True):
    """Append-only audit row for every paper merge.

    Captures the loser's user state at the moment of merge so an operator
    (or a future "undo with state restore" feature) can reconstruct what
    the user had on the alias before it was absorbed. Does not block
    subsequent merges of the same alias to other canonicals — each merge
    writes its own event.
    """

    __tablename__ = "paper_merge_events"

    id: int | None = Field(default=None, primary_key=True)
    alias_paper_id: str = Field(max_length=64)
    canonical_paper_id: str = Field(max_length=64)
    source: str = Field(max_length=16)
    confidence: float = Field(default=0.0)
    reasons: list[str] | None = Field(default=None, sa_column=Column(JSON))
    user_state_snapshot: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    user_state_migrated: bool = Field(default=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    __table_args__ = (
        Index("ix_paper_merge_events_pair", "alias_paper_id", "canonical_paper_id"),
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


# ------------------ Agent execution trace (M17) ------------------


class AgentRunStatus(str, Enum):
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"


class AgentStepStatus(str, Enum):
    running = "running"
    success = "success"
    failed = "failed"
    skipped = "skipped"


class AgentRun(SQLModel, table=True):
    """One end-to-end execution of an agent/pipeline.

    An ``AgentRun`` is the durable audit record for a single invocation of
    anything the front-end shows on the /agent page (the per-pipeline flow
    cards). It always has a ``pipeline_id`` matching an entry in
    ``frontend/src/lib/agentPipelines.ts``. It may optionally link to a
    ``Job`` (the existing coarse job row) and a ``paper_id`` when the run
    is per-paper.

    Steps are stored separately in :class:`AgentStep` and linked by
    ``run_id``. The run row carries the aggregate status + timing; per-step
    detail lives on the steps.
    """

    __tablename__ = "agent_runs"

    id: int | None = Field(default=None, primary_key=True)
    pipeline_id: str = Field(max_length=32, index=True)
    # Display name cached from the pipeline catalog so an outdated catalog
    # never breaks the row.
    pipeline_name: str = Field(max_length=120)
    status: str = Field(
        default=AgentRunStatus.running.value, max_length=16, index=True
    )
    # Trigger: "manual" | "scheduled" | "api:<route>" | "background". Lets
    # the UI tell apart a user click from a cron tick from a BackgroundTask.
    trigger: str = Field(default="manual", max_length=32)
    # Free-form context: who/what this run acted on. e.g. a sync run
    # carries ``{"lookback_hours": 24, "sources": null}``; a process run
    # carries ``{"paper_id": "W12345"}``; a wiki compile run carries
    # ``{"limit": 20}``.
    context: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # Aggregate counters from the run (same shape as Job.stats where
    # available). Cheap to read for the list view; the step-level detail
    # is the source of truth for the per-node timeline.
    summary: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = None
    # Optional FK to the coarse Job row that scheduled this run, so the
    # /agent page can link to the existing /sync/jobs/{id} detail. The
    # AgentRun is independent — runs can exist with no Job (e.g. one
    # paper_chat turn).
    job_id: int | None = Field(default=None, index=True, foreign_key="jobs.id")
    paper_id: str | None = Field(default=None, index=True, max_length=64)
    # Top-level subject (e.g. the wiki page slug, the author A-ID, the
    # paper dedup pair). Free-form so each pipeline can use it.
    subject: str | None = Field(default=None, max_length=300)
    step_count: int = Field(default=0)
    success_count: int = Field(default=0)
    failed_count: int = Field(default=0)
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    __table_args__ = (
        Index("ix_agent_runs_pipeline_started", "pipeline_id", "started_at"),
    )


class AgentStep(SQLModel, table=True):
    """One node (step or LLM call) inside an :class:`AgentRun`.

    Mirrors the shape of ``FlowNode`` in agentPipelines.ts: a step has
    ``kind='step'`` or ``kind='llm'``; LLM steps additionally carry the
    ``feature`` key used in the token-usage log so an admin can join
    AgentStep ⨝ TokenUsage on (run_id, feature) to attribute tokens to a
    specific execution.

    ``input_summary`` / ``output_summary`` are deliberately short string
    blobs (or arbitrary JSON truncated to ~4KB) so the step table stays
    small enough to read in bulk; full prompt/response text is left on
    the existing usage / chat_messages tables.
    """

    __tablename__ = "agent_steps"

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(
        foreign_key="agent_runs.id", index=True, ondelete="CASCADE"
    )
    # Order within the run. The recorder increments a per-run counter
    # as steps are entered; this lets the UI render a stable timeline
    # without depending on wall-clock ordering (which is what we want
    # for LLM retries that fire concurrently).
    seq: int = Field(index=True)
    # Stable node id from agentPipelines.ts (e.g. "download", "parse",
    # "summarize", "topics", "find_candidate_pairs", "compile_concept").
    # Nullable so ad-hoc runs (e.g. one paper_chat) can leave it blank.
    node_id: str | None = Field(default=None, max_length=64)
    label: str = Field(max_length=200)
    kind: str = Field(max_length=8)  # "step" | "llm"
    # For LLM steps, the feature key (e.g. "summarize", "topics",
    # "wiki_scholar"). Same as TokenUsage.feature.
    feature: str | None = Field(default=None, max_length=64)
    status: str = Field(
        default=AgentStepStatus.running.value, max_length=16, index=True
    )
    # ``error`` is a short message (type + trimmed text); the full
    # stacktrace is in the application log.
    error: str | None = Field(default=None, sa_column=Column(Text))
    # Compact IO snapshots. The recorder truncates long values to keep
    # the table lightweight — these are for debugging, not for prompt
    # archaeology.
    input_summary: str | None = Field(default=None, sa_column=Column(Text))
    output_summary: str | None = Field(default=None, sa_column=Column(Text))
    # Arbitrary JSON for per-step detail that doesn't fit a column
    # (e.g. counts, list of paper_ids acted on). Capped to ~32KB by the
    # recorder.
    detail: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # When kind='llm' the recorder back-fills these from the TokenUsage
    # callback so the step carries the same numbers as the Usage page.
    model: str | None = Field(default=None, max_length=120)
    prompt_tokens: int | None = Field(default=None)
    completion_tokens: int | None = Field(default=None)
    total_tokens: int | None = Field(default=None)
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    # How long the step ran, in milliseconds. Duplicated from
    # finished_at - started_at for cheap "find slow steps" queries.
    duration_ms: int | None = Field(default=None, index=True)

    __table_args__ = (
        Index("ix_agent_steps_run_seq", "run_id", "seq", unique=True),
        Index("ix_agent_steps_kind_status", "kind", "status"),
    )


# ------------------ OpenAlex persistent cache (A+B) ------------------


class AuthorWorksSync(SQLModel, table=True):
    """Per-author sync state for :class:`AuthorWorksCache`.

    A row exists (or is inserted) for every A-ID whose works the user has
    visited. ``status`` drives the scholar page's behaviour:

      * ``missing`` — never fetched; the page should lazy-kick off a sync.
      * ``loading`` — a sync is in flight; the page should render an empty
        "Loading…" state and let the frontend poll until ``ready``.
      * ``ready`` — serve from :class:`AuthorWorksCache` with zero OA calls.
      * ``stale`` — same as ``ready`` for read purposes; set on a manual
        "Refresh" so we can show "Re-fetching…" before the next page load.
      * ``failed`` — last sync raised; show the empty state and let the
        next visit retry.

    A server restart must reset any ``loading`` rows to ``failed`` (see
    :func:`carrel.db._reset_orphaned_openalex_sync`); the in-process worker
    that owned them is gone.
    """

    __tablename__ = "author_works_sync"

    author_id: str = Field(primary_key=True, max_length=32)  # "A12345"
    total_count: int | None = None
    last_full_sync_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    last_incremental_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    status: str = Field(default="missing", max_length=16)
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AuthorWorksCache(SQLModel, table=True):
    """One row per OpenAlex Work, scoped by author.

    Populated by :mod:`carrel.pipeline.scholar_works_sync` (page-by-page
    cursor walk) and read by ``api/scholars.py:list_scholar_works`` instead
    of hitting OpenAlex live. The same row is reused across A-IDs that
    share a work — the PK is the W-id, ``author_id`` is the author that
    most-recently refreshed it. ``raw_json`` keeps the original pyalex
    dict so future field additions don't require re-fetching.

    ``schema_version`` is a cache-bust key (mirrors
    :attr:`PaperDedupVerdict.prompt_hash`): increment if ``raw_json``'s
    expected shape changes so old rows are detected and re-fetched.
    """

    __tablename__ = "author_works_cache"

    openalex_id: str = Field(primary_key=True, max_length=32)  # "W12345"
    author_id: str = Field(..., max_length=32, index=True)
    title: str = Field(..., sa_column=Column(Text))
    publication_date: date | None = Field(default=None, sa_column=Column(Date))
    publication_year: int | None = None
    venue: str | None = Field(default=None, sa_column=Column(Text))
    doi: str | None = Field(default=None, max_length=255)
    arxiv_id: str | None = Field(default=None, max_length=32)
    cited_by_count: int | None = None
    is_oa: bool = False
    pdf_url: str | None = Field(default=None, sa_column=Column(Text))
    oa_status: str = Field(default="none", max_length=16)
    raw_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    schema_version: int = Field(default=1)
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class WorkByArxivId(SQLModel, table=True):
    """Map a bare arXiv id (no version suffix) to the OpenAlex Work dict.

    Populated by :func:`carrel.cache.openalex_works.lookup_work_by_arxiv_id`
    on first miss. The same arXiv id is queried by sync (B), the import
    path (D), and ``publication_check``; one row per id covers all three
    callers and is reused across authors.

    PK is the bare arXiv id (``stripped of any trailing vN``) so the same
    paper resolved once serves every caller regardless of which form they
    typed.
    """

    __tablename__ = "work_by_arxiv_id"

    arxiv_id: str = Field(primary_key=True, max_length=32)
    openalex_id: str = Field(..., max_length=32)
    raw_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    schema_version: int = Field(default=1)
    fetched_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
