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

    def paper_dir(self) -> Path:
        return self.root / self.papers_subdir

    def attachments_dir(self) -> Path:
        return self.root / self.attachments_subdir


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
    delay_between_requests_seconds: float = 1.5  # politeness between papers
    citations_limit: int = 500  # cap on stored citing-paper list
    fetch_on_sync: bool = True  # look up citations for newly synced papers
    # Max reference-less papers (enriched before the references-list feature
    # shipped) to backfill per sync run.
    references_backfill_batch: int = 50
    search_enabled: bool = True
    search_per_page: int = 20


class LLMConfig(BaseModel):
    summarize_provider: str = "deepseek"
    summarize_model: str = "deepseek/deepseek-chat"
    fallback_provider: str = "volcengine"
    fallback_model: str = "volcengine/doubao-pro-32k"
    temperature: float = 0.2
    request_timeout_seconds: int = 60


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
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    mineru: MinerUConfig = Field(default_factory=MinerUConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
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
