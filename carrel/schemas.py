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
    remote: bool = False  # institutional SSH download configured


# -------- Papers --------


class AuthorRef(BaseModel):
    """One author on a paper, as stored (IDs/affiliation preserved)."""

    name: str
    openalex_author_id: str = ""
    affiliation: str | None = None


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
    topics: list[str] = []


class PaperDetail(PaperSummary):
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    pdf_url: str | None = None
    pdf_path: str | None = None
    md_path: str | None = None
    summary_zh: str | None = None
    error: str | None = None
    # Full author records (with IDs/affiliation) for clickable author links;
    # the inherited `authors: list[str]` stays for compact display.
    author_list: list[AuthorRef] = []
    influential_citation_count: int | None = None
    reference_count: int | None = None
    citations_updated_at: datetime | None = None
    notes_markdown: str | None = None
    # Institutional download + arXiv→journal detection.
    pdf_origin: str | None = None
    journal_doi: str | None = None
    pdf_files: dict[str, Any] | None = None
    published_checked_at: datetime | None = None
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


# -------- Per-paper chat transcript --------


class ChatTurnIn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatMessagesIn(BaseModel):
    messages: list[ChatTurnIn]


class ChatMessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ChatMessagesOut(BaseModel):
    paper_id: str
    messages: list[ChatMessageOut]
    updated_at: datetime | None = None


# -------- Wiki-wide chat transcript (M12) --------


class WikiChatTurnIn(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class WikiChatMessagesIn(BaseModel):
    messages: list[WikiChatTurnIn]


class WikiChatMessagesOut(BaseModel):
    """Global wiki-chat transcript envelope (no per-page scope)."""

    messages: list[ChatMessageOut]
    updated_at: datetime | None = None


# -------- Topics (LLM classification) --------


class TopicOut(BaseModel):
    id: int
    name: str
    description: str | None = None


class TopicWithCount(TopicOut):
    paper_count: int


# -------- Scholars (author aggregation) --------


class ScholarSummary(BaseModel):
    """An author aggregated across in-library papers.

    ``key`` is the OpenAlex Author ID (e.g. 'A5013214678') when known, else
    ``'name:<exact name>'`` for arXiv/S2 records without an A-ID.
    """

    key: str
    name: str
    affiliation: str | None = None
    paper_count: int
    first_year: int | None = None
    last_year: int | None = None
    total_citations: int = 0
    has_openalex: bool = True


class OpenAlexProfile(BaseModel):
    """Global OpenAlex metadata for an author (fetched live, cached)."""

    id: str
    name: str | None = None
    affiliation: str | None = None
    works_count: int | None = None
    cited_by_count: int | None = None
    h_index: float | None = None
    orcid: str | None = None
    alternate_names: list[str] = []


class ScholarDetail(BaseModel):
    scholar: ScholarSummary
    papers: list[PaperSummary] = []
    profile: OpenAlexProfile | None = None
    # Compiled scholar wiki page (M8), when one exists.
    wiki_page: "WikiPageDetail | None" = None


class ScholarWorkOut(BaseModel):
    """One published work for a scholar, sourced from OpenAlex.

    ``in_library`` and ``library_id`` are joined in by the
    :mod:`carrel.api.scholars` endpoint so the UI can show an "In library"
    badge or an "Import" button without a second round-trip.
    """

    openalex_id: str
    title: str
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    cited_by_count: int | None = None
    is_oa: bool = False
    pdf_url: str | None = None
    in_library: bool = False
    # Carrel Paper.id of the matching library row, when ``in_library`` is True.
    library_id: str | None = None


class ScholarWorksResponse(BaseModel):
    items: list[ScholarWorkOut] = []
    # Opaque pagination token. The cached path emits ``"offset:<n>"`` and
    # the legacy path emitted OpenAlex's cursor; the frontend treats both
    # opaquely and just feeds the next request's ``cursor`` query.
    next_cursor: str | None = None
    # Total works the cache has for this author. ``None`` when the
    # count couldn't be read; the UI should fall back to "Showing N"
    # without "of M" in that case.
    total: int | None = None
    # Cache state. ``ready`` / ``stale`` = serve from cache; ``loading``
    # = a sync is in flight (the page should poll /sync_status until
    # ready/failed); ``missing`` / ``failed`` = no cached rows yet
    # (lazy-kickoff happens server-side; the frontend should also poll).
    status: str = "ready"


class ScholarSyncStatusOut(BaseModel):
    """Compact read for the scholar page's polling loop."""

    author_id: str
    status: str  # missing | loading | ready | stale | failed
    total_count: int | None = None
    last_full_sync_at: datetime | None = None
    last_error: str | None = None


# -------- LLM-compiled wiki (M8) --------


class WikiSourceOut(BaseModel):
    """One provenance row for a wiki page (a backing paper)."""

    paper_id: str
    paper_title: str | None = None
    year: int | None = None
    heading: str | None = None
    quote: str | None = None
    role: str = "context"


class WikiBacklink(BaseModel):
    """A wiki page that links to this page."""

    id: int
    kind: str
    slug: str
    title: str


class WikiPageSummary(BaseModel):
    """Index row for a compiled wiki page (no file IO)."""

    id: int
    kind: str
    slug: str
    title: str
    summary: str | None = None
    tags: list[str] = []
    links_in_count: int = 0
    confidence: float = 0.0
    evidence_count: int = 0
    scholar_aid: str | None = None
    question_status: str | None = None
    # True when the page is an evidence-threshold stub (concept/question with
    # < 3 backing papers).  False for live pages and for redirect shells.
    # D.7 lets the UI show a "待补证据" pill without re-parsing the file.
    stub: bool = False
    # ``entity_key`` is the stable, kind-qualified identity the catalog
    # reconciles against (see carrel/pipeline/wiki/_entities.py).  ``None``
    # for rows that pre-date the identity migration.
    entity_key: str | None = None
    # Set when this row is a redirect shell — points at the entity_key of
    # the canonical.  A live page has ``redirects_to=None``; a shell has
    # both ``entity_key=None`` and ``redirects_to=<canonical key>``.
    redirects_to: str | None = None
    compiled_at: datetime | None = None
    updated_at: datetime | None = None


class WikiPageDetail(WikiPageSummary):
    """Full wiki page: index row plus the on-disk Markdown and provenance."""

    path: str
    frontmatter: dict[str, Any] = {}
    body: str = ""
    sources: list[WikiSourceOut] = []
    backlinks: list[WikiBacklink] = []
    # When the user requested a slug that now resolves to a redirect shell,
    # the API follows the redirect and tags the response with the summary
    # of the slug they originally asked for.  ``None`` for direct hits.
    redirected_from: "WikiPageSummary | None" = None


class WikiCompileRequest(BaseModel):
    """POST /wiki/compile — batch-compile wiki pages across all kinds.

    The driver runs up to four stages, each isolated by try/except so a
    failure in one doesn't roll back the others:

      1. ``paper_extract`` — per-paper LLM extraction of concepts/questions.
      2. ``scholar_compile`` — synthesize scholar pages.
      3. ``concept_compile`` — synthesize concept pages (≥ 3 papers each).
      4. ``question_compile`` — synthesize open-question pages (≥ 3 papers).

    ``stages`` lets an advanced caller run a single stage (e.g. a backfill
    of paper extractions).  ``None`` (the default) runs all four in order.
    """

    limit: int = Field(default=20, ge=1, le=200)
    background: bool = True
    force: bool = False
    stages: list[str] | None = Field(
        default=None,
        description=(
            "Subset of stages to run. Any of "
            "'paper_extract' | 'scholar_compile' | 'concept_compile' | "
            "'question_compile'. Default: all four in order."
        ),
    )


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


class BulkImportIn(BaseModel):
    """One-shot import of N papers by any combination of identifiers.

    Each item runs through the same resolution + upsert chain as
    ``POST /import``; partial failures don't abort the batch — the response
    carries per-item ``status`` (ok / error) so the UI can show a list view.

    ``background=true`` (default) is the right choice for large batches
    (≥50 papers): the worker runs serially via ``BackgroundTasks`` and the
    caller polls ``GET /sync/jobs/{id}`` for progress. Pass
    ``background=false`` to get the full per-item result inline (suitable
    for 1-20 papers selected from a search).
    """

    items: list[ImportPaperIn] = Field(..., min_length=1, max_length=1000)
    background: bool = True


class BulkImportItemOut(BaseModel):
    id: str | None = None
    title: str | None = None
    created: bool = False
    status: str  # "ok" | "error"
    error: str | None = None


class BulkImportOut(BaseModel):
    job_id: int
    items: list[BulkImportItemOut] | None = None  # null when background=True


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


# -------- Scheduler --------


class ScheduledJobOut(BaseModel):
    """One cron job's current configuration + APScheduler runtime state."""

    id: str
    label: str
    description: str = ""
    enabled: bool
    cron: str
    running: bool = False
    next_run_at: datetime | None = None
    last_status: str | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_message: str | None = None
    last_stats: dict[str, Any] | None = None
    requires: str | None = None  # e.g. "remote_ssh" when gating an optional dep
    requirement_satisfied: bool = True


class SchedulerStatus(BaseModel):
    enabled: bool  # master switch (scheduler running at all)
    jobs: list[ScheduledJobOut]


class ScheduledJobUpdate(BaseModel):
    enabled: bool | None = None
    cron: str | None = None


class SchedulerUpdate(BaseModel):
    """PATCH /schedule body — any subset of the schedule settings.

    Each top-level key mirrors a field in ``ScheduleConfig``. Cron strings are
    validated by re-parsing them with APScheduler; an invalid string 422s.
    """

    enabled: bool | None = None
    sync_cron: str | None = None
    remote_fill_enabled: bool | None = None
    remote_fill_cron: str | None = None
    publication_check_enabled: bool | None = None
    publication_check_cron: str | None = None
    wiki_compile_enabled: bool | None = None
    wiki_compile_cron: str | None = None


class ScheduledRunAck(BaseModel):
    """Returned by POST /schedule/{id}/run after the manual trigger is queued."""

    job_id: str
    running: bool  # true if the function was actually dispatched
    message: str


class ProcessRequest(BaseModel):
    """Trigger PDF download + MinerU parse.

    If ``paper_id`` is given, only that paper is processed; otherwise up to
    ``limit`` pending/failed papers are processed in a batch.
    """

    paper_id: str | None = None
    limit: int = 10
    background: bool = False


class PublicationCheckRequest(BaseModel):
    """Check an arXiv paper for a published journal version."""

    background: bool = False
    force: bool = False  # re-check even if a journal_doi is already recorded


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


class TopicsRequest(BaseModel):
    """Trigger LLM topic classification for one paper or a batch.

    If ``paper_id`` is given, only that paper is classified; otherwise up to
    ``limit`` in-library papers with no topics are processed. ``force=True``
    reclassifies, replacing that paper's existing topic assignments.
    """

    paper_id: str | None = None
    limit: int = 20
    background: bool = False
    force: bool = False


class AuthorsBackfillRequest(BaseModel):
    """Trigger OpenAlex author-ID resolution for one paper or a batch."""

    paper_id: str | None = None
    limit: int = 100
    background: bool = False


class PaperExtractRequest(BaseModel):
    """Trigger LLM concept/question extraction for one paper or a batch.

    Each call returns a Job per paper so the frontend can poll for progress.
    ``deep=True`` widens the section pick (5 head + 5 tail) at the cost of
    a larger LLM input; it is exposed for ops backfills, not the UI.
    """

    paper_id: str | None = None
    limit: int = 20
    background: bool = False
    force: bool = False
    deep: bool = False


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


# -------- Settings (M12) --------


class EnvOverride(BaseModel):
    """One field on a SerialisedSection that .env is currently overriding.

    ``env_var`` is the environment variable name (always surfaced). ``env_value``
    is the live value the process is using from that env var, but only when it
    is safe to expose (i.e. the field isn't flagged as a secret). Secret overrides
    (``OPENALEX_API_KEY``, ``S2_API_KEY``) carry ``env_value=None`` and the UI
    falls back to a generic "set in .env" badge.
    """

    env_var: str
    env_value: str | None = None


class SerialisedSection(BaseModel):
    """One YAML section as returned by GET /settings and re-emitted by PATCH.

    ``values`` carries the effective configuration with secrets masked. For
    fields that ``.env`` overrides at startup, the env-var name and (where
    non-secret) the live value are recorded in ``env_overrides`` so the UI
    can show both the source and the effective value. ``requires_restart``
    flags sections whose change only takes effect after a process restart
    (storage paths, HTTP bind, CORS origins).
    """

    values: dict[str, Any] = Field(default_factory=dict)
    env_overrides: dict[str, EnvOverride] = Field(default_factory=dict)
    requires_restart: bool = False


class EnvEntry(BaseModel):
    """One row in the .env read-only summary card."""

    name: str           # Python attribute name on EnvSettings
    label: str          # human-friendly name
    is_secret: bool
    is_set: bool
    # Only populated for non-secret entries (e.g. database_url,
    # mineru_base_url, host/port, cors origins). Always None for secrets.
    value: str | None = None


class SettingsOut(BaseModel):
    yaml_path: str
    sections: dict[str, SerialisedSection] = Field(default_factory=dict)
    env: list[EnvEntry] = Field(default_factory=list)
    # Convenience list of section names whose PATCH writes to disk but does
    # not mutate the in-memory app_config. Drives the "Restart required"
    # banner on the frontend.
    restart_required_sections: list[str] = Field(default_factory=list)


class SettingsUpdate(BaseModel):
    """PATCH /settings body. Each top-level key is a YAML section name.

    Section bodies for ``SECTION_MODELS`` sections are partial dicts (any
    subset of that section's fields). ``"subscriptions"`` is a full list
    replacement. Absent sections are left untouched.
    """

    sections: dict[str, dict[str, Any] | list[Any]] = Field(default_factory=dict)


# -------- MCP integration (M14) --------


class BraveSearchItem(BaseModel):
    """One result from the Brave web search MCP tool.

    Carrel-internal schema, not a passthrough of the native Brave response —
    only the fields we actually use are surfaced. Extra fields on the
    upstream payload are dropped at the adapter level
    (see :mod:`carrel.search.brave`).
    """

    title: str
    url: str
    description: str | None = None
    # Brave's `age` field is a human-readable relative-time string
    # ("2 hours ago"), not a date. Keep as str.
    age: str | None = None
    language: str | None = None
    family_friendly: bool | None = None
    extra_snippets: list[str] = Field(default_factory=list)


class BraveSearchRequest(BaseModel):
    """Body for ``POST /search/brave``.

    Mirrors a subset of the ``brave_web_search`` tool's input schema (only
    the fields most users want to tune). Brave-specific options like
    ``goggles`` / ``result_filter`` are intentionally omitted — add a
    follow-up endpoint if those become a need.
    """

    query: str = Field(..., min_length=1, max_length=400)
    count: int = Field(10, ge=1, le=20)
    country: str | None = Field(None, max_length=2, description="ISO 3166-1 alpha-2")
    search_lang: str | None = Field(None, max_length=8, description="BCP-47")
    # pd / pw / pm / py, or a YYYY-MM-DDtoYYYY-MM-DD range. Validated server-
    # side by passing the value through to Brave — we don't re-parse it.
    freshness: str | None = None
    safesearch: str | None = Field(None, pattern="^(off|moderate|strict)$")


class BraveSearchResponse(BaseModel):
    query: str
    results: list[BraveSearchItem] = Field(default_factory=list)
    total: int = 0
    took_ms: int = 0


class MCPToolInfo(BaseModel):
    """One tool exposed by one running MCP server (for ``GET /mcp/tools``)."""

    server: str
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MCPServerHealth(BaseModel):
    name: str
    enabled: bool  # YAML-enabled; the server may still be offline (crash, missing dep)
    running: bool
    tool_count: int = 0
    last_error: str | None = None


class MCPHealthResponse(BaseModel):
    """Response for ``GET /mcp/health``."""

    enabled: bool  # master kill switch (cfg + env)
    servers: list[MCPServerHealth] = Field(default_factory=list)
    # Set when a server failed to start so the UI can surface the cause
    # without having to scrape logs.
    error: str | None = None

# Resolve the forward reference from ScholarDetail.wiki_page to WikiPageDetail,
# which is defined later in this module.
ScholarDetail.model_rebuild()
