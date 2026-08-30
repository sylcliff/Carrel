"""Application settings.

Two layers, merged at startup:
  1. Environment variables (secrets, connection strings) — .env
  2. YAML file (paths, schedules, model choices) — data/config.yaml
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ----------------------------- YAML sub-schemas -----------------------------


class StorageConfig(BaseModel):
    root: Path = Path("./data")
    papers_subdir: str = "papers"
    attachments_subdir: str = "attachments"
    wiki_subdir: str = "wiki"

    def paper_dir(self) -> Path:
        return self.root / self.papers_subdir

    def attachments_dir(self) -> Path:
        return self.root / self.attachments_subdir

    def wiki_dir(self) -> Path:
        return self.root / self.wiki_subdir

    def wiki_kind_dir(self, kind: str) -> Path:
        """Directory for one wiki kind, e.g. ``wiki/scholars``.

        ``kind`` is the singular WikiKind value ("concept"/"scholar"/"question");
        on disk each lives under its plural directory name.
        """
        return self.wiki_dir() / f"{kind}s"


class HttpConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787


class CorsConfig(BaseModel):
    origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:5173"])


class OpenAlexConfig(BaseModel):
    mailto: str | None = None
    api_key: str | None = None
    request_timeout_seconds: int = 30
    max_retries: int = 3
    search_enabled: bool = True
    search_per_page: int = 20


class ArxivConfig(BaseModel):
    request_timeout_seconds: int = 30
    max_retries: int = 3
    max_results_per_query: int = 200
    delay_between_requests_seconds: float = 3.0
    search_enabled: bool = True
    search_per_page: int = 20


class SemanticScholarConfig(BaseModel):
    base_url: str = "https://api.semanticscholar.org"
    api_key: str | None = None  # optional x-api-key; raises rate limit
    request_timeout_seconds: int = 30
    max_retries: int = 3
    # Max requests per second. None = auto: 1.0 with an API key (S2's documented
    # introductory limit, shared across all endpoints), 0.5 without (the
    # unauthenticated pool is shared and may be throttled under load).
    rate_limit_per_second: float | None = None
    # Deprecated: inter-paper spacing is now handled by the global rate limiter
    # inside semanticscholar_client. Retained so existing config.yaml still
    # parses; ignored by the pipeline.
    delay_between_requests_seconds: float = 1.5
    citations_limit: int = 500  # cap on stored citing-paper list
    fetch_on_sync: bool = True  # look up citations for newly synced papers
    # Max reference-less papers (enriched before the references-list feature
    # shipped) to backfill per sync run.
    references_backfill_batch: int = 50
    # Max already-enriched library papers to refresh per sync run — picks the
    # stalest rows (oldest citations_updated_at) so cited-by/reference counts
    # creep forward without re-hitting the whole library every night. Set to 0
    # to disable periodic refresh.
    citations_refresh_batch: int = 25
    search_enabled: bool = True
    search_per_page: int = 20


class CrossrefConfig(BaseModel):
    """Crossref REST API client config (https://api.crossref.org).

    Crossref is the DOI registration authority; a "polite pool" (User-Agent
    containing a ``mailto:``) bumps the rate limit from ~10 to ~50 req/s.
    No API key required.
    """

    base_url: str = "https://api.crossref.org"
    mailto: str | None = None  # polite-pool contact; recommended
    request_timeout_seconds: int = 30
    max_retries: int = 3
    search_enabled: bool = True
    search_per_page: int = 20


class LLMConfig(BaseModel):
    summarize_provider: str = "deepseek"
    summarize_model: str = "deepseek/deepseek-chat"
    fallback_provider: str = "volcengine"
    fallback_model: str = "volcengine/doubao-pro-32k"
    temperature: float = 0.2
    request_timeout_seconds: int = 60
    # Max characters of parsed Markdown fed to the summarizer (after stripping
    # image markup). ~12k chars ≈ 2-3k tokens; keeps per-paper cost bounded.
    max_input_chars: int = 12000
    # Per-paper RAG chat. chat_model/fallback default to the summarizer model
    # when None. rag_top_k chunks are retrieved as context; chat_history_limit
    # trims the conversation turns sent back to the model.
    chat_model: str | None = None
    chat_fallback_model: str | None = None
    chat_temperature: float = 0.3
    rag_top_k: int = 6
    chat_history_limit: int = 6
    # Full-text fallback (paper not yet embedded): max chars of the parsed
    # markdown fed as context, after stripping image markup.
    chat_fulltext_chars: int = 24000

    # ---- Wiki-wide RAG chat (M12) ----
    # Same model family as the per-paper chat; the context is the union of the
    # top-k wiki pages by embedding similarity (full bodies read from disk).
    # chat_model/chat_fallback default to the per-paper chat chain when None.
    wiki_chat_top_k: int = 6
    # Per-page cap when assembling the <wiki-context> block (after stripping
    # frontmatter). Keeps the total prompt bounded even on long scholar pages.
    wiki_chat_fulltext_chars: int = 4000
    # ---- Wiki chat MCP tool loop (M14.x) ----
    # How many round-trips through the model + tool + model the wiki-chat
    # agent is allowed to take before bailing out with an error. Each
    # iteration may yield zero or more text deltas plus any number of
    # tool calls; one tool call counts as part of the same iteration.
    wiki_chat_max_tool_iterations: int = 5

    # ---- Scholar enrich (M14.x) — web-research agent ----
    # Per-click agent on the Scholar Detail page. The agent uses
    # brave_search__brave_web_search + builtin__save_scholar_note to
    # research the scholar and append a "## Web research" note to the
    # preserved <section data-user="true">. Falls back to chat_model /
    # summarize_model when None so the same auth chain covers it.
    wiki_enrich_model: str | None = None
    wiki_enrich_fallback_model: str | None = None
    # Agentic loop budget. Generous: research needs multiple searches
    # plus the final save call plus one retry on tool error.
    wiki_enrich_max_iterations: int = 8

    # ---- Paper dedup LLM judge (M10.6) ----
    # paper_dedup_judge_model defaults to the summarizer so the LLM judge uses
    # whatever model is already authenticated. paper_dedup_judge_fallback
    # defaults to the existing chat fallback chain.
    paper_dedup_judge_model: str | None = None
    paper_dedup_judge_fallback: str | None = None
    # Bump prompt_version when the SYSTEM/USER prompts change; cached verdicts
    # are keyed on this so a bumped version transparently invalidates them.
    paper_dedup_judge_prompt_version: int = 1
    # Single-scan budget for LLM calls so a large borderline queue can't run
    # the meter away. The pipeline stops calling the LLM after this many
    # calls in one run_dedup; remaining pairs are left as suggestions.
    paper_dedup_judge_max_calls_per_run: int = 200

    # ---- LLM output language (paper_card + summarize) ----
    # Controls the language of free-form text the LLM produces for
    # paper_card fields (research_question, method_summary, …) and
    # which of the bilingual tldr_en/tldr_zh/summary_zh fields is the
    # primary output for summarize. Does NOT change the user-facing
    # UI language (the app has no i18n). Switching the value is live:
    # the next LLM call picks up the new value because the call sites
    # read cfg.llm.output_language per invocation. Already-stored
    # paper_card / summary rows are NOT retroactively regenerated;
    # the user re-runs Extract / summarize for those papers to get
    # the new language.
    output_language: Literal["zh", "en"] = "zh"


class EmbeddingsConfig(BaseModel):
    provider: str = "volcengine"
    model: str = "volcengine/doubao-embedding-large-text-240915"
    dim: int = 2048
    request_timeout_seconds: int = 60
    batch_size: int = 50


class DownloadConfig(BaseModel):
    request_timeout_seconds: int = 60
    max_bytes: int = 80 * 1024 * 1024  # reject PDFs larger than this (80 MiB)
    user_agent: str = "Carrel/0.1 (+https://github.com/)"


class MinerUConfig(BaseModel):
    base_url: str = "http://127.0.0.1:8000"
    request_timeout_seconds: int = 900  # CPU parsing can take minutes per paper
    # MinerU parse options (see mineru/cli/api_request.py)
    backend: str = "pipeline"  # pipeline | vlm-engine | hybrid-engine | ...
    parse_method: str = "auto"  # auto | txt | ocr
    lang_list: list[str] = Field(default_factory=lambda: ["en"])
    formula_enable: bool = True
    table_enable: bool = True


class ChunkingConfig(BaseModel):
    target_tokens: int = 900
    overlap_tokens: int = 150
    min_tokens: int = 200


class ScheduleConfig(BaseModel):
    enabled: bool = False
    sync_cron: str = "0 8 * * *"
    # Periodically try to download PDFs for papers that have no open-access PDF
    # (falls back to the institutional SSH server when configured).
    remote_fill_enabled: bool = False
    remote_fill_cron: str = "0 9 * * *"
    # Periodically check arXiv papers for a published journal version.
    publication_check_enabled: bool = False
    publication_check_cron: str = "0 10 * * 1"
    # Periodically compile the LLM wiki (scholar/concept/question pages) from
    # in-library papers. Default off; the UI also offers a manual "Compile wiki".
    wiki_compile_enabled: bool = False
    wiki_compile_cron: str = "17 11 * * *"


class MCPServerConfig(BaseModel):
    """One MCP server to spawn at startup.

    ``command`` + ``args`` defines the subprocess; the runtime env is
    Carrel's ``os.environ`` overlaid with ``env`` from this section and any
    server-specific secrets from :class:`EnvSettings` (currently only
    ``BRAVE_API_KEY`` for the ``brave_search`` server).

    Per-server timeouts default to the global ``MCPConfig`` values when
    left as ``None``, so most servers just override ``enabled`` and the
    command/args.
    """

    enabled: bool = True
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    startup_timeout_seconds: int | None = None
    tool_call_timeout_seconds: int | None = None


class MCPConfig(BaseModel):
    """Top-level MCP integration settings.

    ``enabled`` is the master kill switch; ``env.mcp_enabled`` in
    :class:`EnvSettings` is the second one. The registry only starts
    servers when both are true.
    """

    enabled: bool = True
    # Subprocess launch + initialize() timeout, per server (override per server
    # in MCPServerConfig). First-ever `npx -y` can take 5-10s to fetch the
    # package, so this is sized to leave headroom.
    startup_timeout_seconds: int = 15
    # Per-call timeout for `call_tool`; brave_web_search typically returns
    # in <2s, but network + summarizer calls can take longer.
    tool_call_timeout_seconds: int = 30
    servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


SubKind = Literal["keyword", "author", "venue", "arxiv_category"]


class Subscription(BaseModel):
    kind: SubKind
    value: str
    label: str | None = None


class CarrelYAML(BaseModel):
    """The whole YAML file as one Pydantic model."""

    storage: StorageConfig = Field(default_factory=StorageConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)
    openalex: OpenAlexConfig = Field(default_factory=OpenAlexConfig)
    arxiv: ArxivConfig = Field(default_factory=ArxivConfig)
    semantic_scholar: SemanticScholarConfig = Field(default_factory=SemanticScholarConfig)
    crossref: CrossrefConfig = Field(default_factory=CrossrefConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    mineru: MinerUConfig = Field(default_factory=MinerUConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    subscriptions: list[Subscription] = Field(default_factory=list)


# ----------------------------- Env (secrets) -----------------------------


class EnvSettings(BaseSettings):
    """Secrets and connection strings. All read from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://carrel:carrel_dev@127.0.0.1:5432/carrel"
    deepseek_api_key: str | None = None
    volcano_api_key: str | None = None
    openalex_api_key: str | None = None
    openalex_mailto: str | None = None
    s2_api_key: str | None = None
    summarize_model: str = "deepseek/deepseek-chat"
    fallback_model: str = "volcengine/doubao-pro-32k"
    embedding_model: str = "volcengine/doubao-embedding-large-text-240915"
    embedding_dim: int = 2048
    mineru_base_url: str = "http://127.0.0.1:8000"
    carrel_host: str = "127.0.0.1"
    carrel_port: int = 8787
    carrel_cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    # ---- Institutional SSH download (optional; disabled by default) ----
    # Generic SSH jump host running a paper-download CLI (e.g. scansci-pdf)
    # on an institutional/campus IP. Nothing here is hardcoded; fill .env.
    remote_ssh_enabled: bool = False
    remote_ssh_host: str | None = None
    remote_ssh_port: int = 22
    remote_ssh_user: str | None = None
    # Absolute path to a private key (Ed25519 or RSA).
    remote_ssh_key_path: str | None = None
    # If set, host keys are verified against this known_hosts file; otherwise
    # AutoAddPolicy is used (with a one-time warning).
    remote_ssh_known_hosts_path: str | None = None
    remote_ssh_connect_timeout: int = 25
    # Working directory on the remote host where downloaded PDFs land.
    remote_work_dir: str | None = None
    # Command run on the remote host. Supported placeholders: {id} (the
    # DOI/arXiv identifier, whitelist-validated), {work_dir}, {timeout}.
    # The CLI is expected to print "OK: <path>.pdf" on success.
    remote_command_template: str | None = (
        "mkdir -p '{work_dir}'; timeout {timeout} scansci-pdf get "
        "'{id}' --output '{work_dir}' --strategy legal_only"
    )
    # Per-paper download timeout on the remote (seconds), and SSH retry count.
    remote_dl_timeout: int = 240
    remote_retries: int = 3
    # arXiv→journal detection: don't even query until the arXiv <published>
    # date is this old; throttle re-checks per paper.
    remote_journal_min_age_days: int = 180
    remote_journal_check_throttle_days: int = 30

    # ---- MCP integration (M14) ----
    # API key for the Brave Search MCP server
    # (https://github.com/brave/brave-search-mcp-server). Required for the
    # `brave_search` server; missing → /mcp/health reports it offline.
    brave_api_key: str | None = None
    # Master kill switch for the MCP layer (env-level override of the
    # YAML `mcp.enabled` flag). Defaults to true so a fresh install gets
    # the feature without YAML edits; set to false to fully disable.
    mcp_enabled: bool = True


# ----------------------------- Loader -----------------------------


def load_yaml(path: Path) -> CarrelYAML:
    if not path.exists():
        return CarrelYAML()
    with path.open(encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    return CarrelYAML.model_validate(raw)


def load_settings(
    yaml_path: Path | None = None,
) -> tuple[CarrelYAML, EnvSettings]:
    """Load both layers. YAML env-friendly values can be overridden by env vars."""
    env = EnvSettings()
    cfg = load_yaml(yaml_path) if yaml_path else CarrelYAML()

    # env wins for connection strings and secrets
    if env.database_url:
        # cfg doesn't store db url; FastAPI uses env.database_url directly
        pass
    if env.openalex_mailto and not cfg.openalex.mailto:
        cfg.openalex.mailto = env.openalex_mailto
    if env.openalex_api_key:
        cfg.openalex.api_key = env.openalex_api_key
    if env.s2_api_key:
        cfg.semantic_scholar.api_key = env.s2_api_key
    if env.summarize_model:
        cfg.llm.summarize_model = env.summarize_model
    if env.fallback_model:
        cfg.llm.fallback_model = env.fallback_model
    if env.embedding_model:
        cfg.embeddings.model = env.embedding_model
    if env.embedding_dim:
        cfg.embeddings.dim = env.embedding_dim
    if env.mineru_base_url:
        cfg.mineru.base_url = env.mineru_base_url
    if env.carrel_host:
        cfg.http.host = env.carrel_host
    if env.carrel_port:
        cfg.http.port = env.carrel_port
    if env.carrel_cors_origins:
        cfg.cors.origins = [o.strip() for o in env.carrel_cors_origins.split(",") if o.strip()]
    return cfg, env
