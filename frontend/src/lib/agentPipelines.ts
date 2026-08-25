import {
  CloudDownload,
  Database,
  FileSearch,
  GitMerge,
  MessagesSquare,
  Network,
  Wrench,
  type LucideIcon,
} from "lucide-react";

type NodeKind = "step" | "llm";

export interface FlowNode {
  label: string;
  kind: NodeKind;
  feature?: string;
  source?: string;
  description?: string;
}

export interface Pipeline {
  id: string;
  name: string;
  icon: LucideIcon;
  trigger: string;
  description: string;
  nodes: FlowNode[];
  output: string;
  jobKinds: string[];
  relatedRoutes?: string[];
}

export const PIPELINES: Pipeline[] = [
  {
    id: "sync",
    name: "Sync (discover)",
    icon: CloudDownload,
    trigger: "POST /sync (manual) or scheduler",
    description:
      "Fetch candidate papers from arXiv + OpenAlex for all enabled subscriptions; insert fresh ones into the inbox (in_library=False).",
    jobKinds: ["sync"],
    relatedRoutes: ["GET /sync/jobs", "GET /sync/jobs/{id}", "POST /sync/{id}/run"],
    nodes: [
      {
        label: "Subscriptions",
        kind: "step",
        source: "carrel/pipeline/runner.py:list_enabled_subscriptions",
        description: "Read every enabled Subscription row from the DB.",
      },
      {
        label: "arXiv fetch",
        kind: "step",
        source: "carrel/sources/arxiv.py:fetch_recent",
        description:
          "Per subscription: query arXiv by category and by keyword. Per-source errors are collected, not raised.",
      },
      {
        label: "OpenAlex fetch",
        kind: "step",
        source: "carrel/sources/openalex_client.py",
        description:
          "Per subscription: query OpenAlex by author, venue, and keyword. Each call is isolated so a single 429/5xx does not kill the sync.",
      },
      {
        label: "Merge + cross-id dedup",
        kind: "step",
        source: "carrel/pipeline/runner.py:upsert_records",
        description:
          "Stronger record wins (has OA id > has venue > has DOI > has abstract). Cross-id dedup: if the canonical id misses, look for a row sharing DOI / arXiv / journal_doi bridge.",
      },
    ],
    output: "Inbox (in_library=False); citations backfilled for library papers",
  },
  {
    id: "process",
    name: "Process paper",
    icon: Wrench,
    trigger: "POST /process (per paper) or /process/pending (batch)",
    description:
      "Drive a paper through the state machine: download PDF, parse with MinerU, LLM summarize, LLM topic-classify.",
    jobKinds: ["download", "parse", "summarize", "topics"],
    relatedRoutes: ["POST /process", "POST /process/pending"],
    nodes: [
      {
        label: "Download PDF",
        kind: "step",
        source: "carrel/pipeline/process.py:_step_download",
        description:
          "Build an ordered PDF-URL list (stored pdf_url + OpenAlex work_pdf_candidates + synthesized arXiv). Try HTTP first, fall back to the institutional SSH jump host if configured.",
      },
      {
        label: "MinerU parse to MD",
        kind: "step",
        source: "carrel/pipeline/process.py:_step_parse",
        description:
          "Submit the PDF to MinerU (HTTP /file_parse). Result is paper.md (formula/table-preserving). The bottleneck step.",
      },
      {
        label: "summarize",
        kind: "llm",
        feature: "summarize",
        source: "carrel/pipeline/summarize.py:summarize_paper",
        description:
          "One LLM call per paper. Returns JSON {tldr_en, tldr_zh, summary_zh, keywords[]}. Non-fatal - a missing key or LLM error leaves the paper at parsed.",
      },
      {
        label: "topics",
        kind: "llm",
        feature: "topics",
        source: "carrel/pipeline/topics.py:topics_paper",
        description:
          "Metadata-only classification (no PDF needed). Returns 1-4 broad research themes, reusing existing topic names verbatim when they fit.",
      },
    ],
    output:
      "Paper.status = summarized; tldr_en / tldr_zh / summary_zh / keywords + PaperTopic rows set",
  },
  {
    id: "embed",
    name: "Embed (RAG index)",
    icon: Database,
    trigger: "POST /embed (per paper, after parse)",
    description:
      "Chunk the parsed Markdown and embed each chunk via Ark; write one row per chunk to the chunks table.",
    jobKinds: ["embed"],
    relatedRoutes: ["POST /embed"],
    nodes: [
      {
        label: "Chunk parsed MD",
        kind: "step",
        source: "carrel/chunking.py:chunk_markdown",
        description:
          "Section-aware splitter with a character cap per chunk. Existing chunks are wiped before the fresh attempt.",
      },
      {
        label: "Embed (Ark)",
        kind: "step",
        source: "carrel/embeddings.py + carrel/pipeline/embed.py:embed_paper",
        description:
          "One embedding call per chunk via Ark. Chunks row is written per chunk; paper.status flips to ready on success.",
      },
    ],
    output: "Paper.status = ready; chunks table populated (one row per chunk)",
  },
  {
    id: "citations",
    name: "Citations (S2)",
    icon: FileSearch,
    trigger:
      "POST /citations/refresh (manual) or auto on /sync (if cfg.semantic_scholar.fetch_on_sync)",
    description:
      "Enrich library papers with Semantic Scholar reference list and cited-by count; best-effort, bounded per run.",
    jobKinds: ["citations"],
    relatedRoutes: ["POST /citations/refresh", "GET /citations/{id}"],
    nodes: [
      {
        label: "Select missing-refs + stale",
        kind: "step",
        source:
          "carrel/pipeline/citations.py:select_missing_references / select_stale",
        description:
          "Two bounded sweeps: (1) library papers with reference_count set but references still NULL; (2) N stalest rows by citations_updated_at, NULLS FIRST.",
      },
      {
        label: "Semantic Scholar API",
        kind: "step",
        source: "carrel/pipeline/citations.py:enrich_paper",
        description:
          "Per-paper S2 call for reference list and cited-by count. Failures (rate limit, S2 downtime) must not fail the whole sync.",
      },
    ],
    output:
      "Paper.citation_count, Paper.references_json, Paper.citations_updated_at refreshed",
  },
  {
    id: "paper_dedup",
    name: "Paper dedup (M10)",
    icon: GitMerge,
    trigger: "POST /dedup/run (manual) or /dedup/auto (auto-apply)",
    description:
      "Find near-duplicate paper pairs (DOI / arXiv / OpenAlex overlap). Strong anchors (exact id match) merge automatically; borderline pairs are sent to the LLM judge.",
    jobKinds: ["paper_dedup"],
    relatedRoutes: ["POST /dedup/run", "POST /dedup/auto"],
    nodes: [
      {
        label: "Find candidate pairs",
        kind: "step",
        source: "carrel/pipeline/paper_dedup.py:find_candidate_pairs",
        description:
          "Cluster papers by shared DOI / arXiv / journal_doi / S2 id. Each pair becomes a candidate for merging or aliasing.",
      },
      {
        label: "Strong-anchor check",
        kind: "step",
        source: "carrel/pipeline/paper_dedup.py:strong_anchor_ok",
        description:
          "If both papers share a strong anchor (exact DOI or arXiv id), merge immediately without consulting the LLM.",
      },
      {
        label: "dedup_judge",
        kind: "llm",
        feature: "dedup_judge",
        source: "carrel/pipeline/paper_dedup_judge.py:judge_pair",
        description:
          "Borderline pairs only. Returns JSON {verdict, confidence, reasons[]}. Verdicts are cached in paper_dedup_verdicts keyed on (paper_a, paper_b, prompt_hash); call budget is per-run via cfg.llm.paper_dedup_judge_max_calls_per_run.",
      },
    ],
    output: "PaperAlias indirection (or merge) for high-confidence pairs",
  },
  {
    id: "scholar_dedup",
    name: "Scholar dedup (M9)",
    icon: GitMerge,
    trigger: "POST /scholar-dedup/run (manual) or /scholar-dedup/auto",
    description:
      "Disambiguate authors across OpenAlex A-IDs: coauthor + affiliation + name scoring; auto-merge high-confidence, queue ambiguous for review.",
    jobKinds: ["scholar_dedup"],
    relatedRoutes: ["POST /scholar-dedup/run", "POST /scholar-dedup/auto"],
    nodes: [
      {
        label: "Find author pairs",
        kind: "step",
        source: "carrel/pipeline/scholar_dedup.py:find_candidate_pairs",
        description:
          "Cluster authors by shared name / shared coauthor set / shared affiliation. Capped to keep the candidate space manageable.",
      },
      {
        label: "Coauthor + affiliation score",
        kind: "step",
        source: "carrel/pipeline/scholar_dedup.py:score_pair",
        description:
          "Heuristic score over coauthor overlap, affiliation string similarity, and name normalization. No LLM call - deterministic.",
      },
    ],
    output: "scholar_aliases / canonical_scholar_id (auto-merge above threshold)",
  },
  {
    id: "wiki",
    name: "Wiki compile (M8)",
    icon: Network,
    trigger: "POST /wiki/compile (full) or /wiki/recompile (one page)",
    description:
      "Multi-stage compile of scholar / concept / question pages over the library. Each stage is wrapped in its own try/except; stages cascade only when the prior one produced work.",
    jobKinds: ["wiki_compile", "wiki_recompile", "paper_extract"],
    relatedRoutes: ["POST /wiki/compile", "POST /wiki/recompile"],
    nodes: [
      {
        label: "paper_extract",
        kind: "llm",
        feature: "extract",
        source: "carrel/pipeline/paper_extract.py:extract_paper",
        description:
          "Per-paper LLM extraction of METHOD/THEORY/DATASET/DOMAIN/PHENOMENON concepts and open questions, every item grounded by a verbatim quote from the parsed body. Feeds the concept + question wiki compilations.",
      },
      {
        label: "scholar_compile",
        kind: "llm",
        feature: "wiki_scholar",
        source: "carrel/pipeline/wiki/scholar_compile.py",
        description:
          "One page per researcher, synthesised from in-library paper metadata + abstracts. Cites each claim with a [^n] footnote.",
      },
      {
        label: "concept_compile",
        kind: "llm",
        feature: "wiki_concept",
        source: "carrel/pipeline/wiki/concept_compile.py",
        description:
          "One page per recurring concept, grounded only in the supplied paper snippets. Old page body is passed as {old_body} so the model can diff / update.",
      },
      {
        label: "question_compile",
        kind: "llm",
        feature: "wiki_question",
        source: "carrel/pipeline/wiki/question_compile.py",
        description:
          "One page per open question the library's papers keep raising. Same {old_body} contract as concept compile.",
      },
    ],
    output:
      "data/wiki/{scholars,concepts,questions}/*.md + wiki_pages / wiki_sources tables reindexed",
  },
  {
    id: "paper_chat",
    name: "Paper chat (RAG)",
    icon: MessagesSquare,
    trigger: "POST /papers/{paper_id}/chat (SSE stream)",
    description:
      "Streaming chat over a single paper. Query is embedded; top-k chunks by similarity are retrieved (fallback: truncated full text); the LLM streams the answer.",
    jobKinds: [],
    relatedRoutes: [
      "POST /papers/{id}/chat",
      "GET /papers/{id}/chat/messages",
      "PUT /papers/{id}/chat/messages",
    ],
    nodes: [
      {
        label: "Embed query",
        kind: "step",
        source: "carrel/api/chat.py:_retrieve_chunks",
        description: "Embed the user's question via Ark (same model as the chunk index).",
      },
      {
        label: "Retrieve top-k chunks",
        kind: "step",
        source: "carrel/api/chat.py:_rank_postgres / _rank_sqlite",
        description:
          "Top-k chunks by embedding similarity (Postgres uses pgvector; SQLite uses a brute-force cosine scan over the paper's chunks). Falls back to the truncated full text if chunks are missing.",
      },
      {
        label: "paper_chat",
        kind: "llm",
        feature: "paper_chat",
        source: "carrel/api/chat.py:paper_chat",
        description:
          "Streaming LLM call (SSE). System prompt anchors the model to the paper context; chat history is appended as recent turns. Persists each turn to chat_messages.",
      },
    ],
    output: "SSE token stream + chat_messages row (per turn)",
  },
  {
    id: "wiki_chat",
    name: "Wiki chat (RAG)",
    icon: MessagesSquare,
    trigger: "POST /wiki/chat (SSE stream)",
    description:
      "Streaming chat over the whole wiki. Top-k pages by synopsis-embedding similarity are pulled into the context block; the LLM streams the answer.",
    jobKinds: [],
    relatedRoutes: [
      "POST /wiki/chat",
      "GET /wiki/chat/messages",
      "PUT /wiki/chat/messages",
    ],
    nodes: [
      {
        label: "Embed query",
        kind: "step",
        source: "carrel/api/wiki_chat.py:_retrieve_pages",
        description: "Embed the user's question via Ark.",
      },
      {
        label: "Retrieve top-k pages",
        kind: "step",
        source: "carrel/api/wiki_chat.py:_top_k_pages_postgres / _top_k_pages_sqlite",
        description:
          "Top-k wiki pages by synopsis-embedding similarity. Empty wiki short-circuits to an SSE error frame.",
      },
      {
        label: "wiki_chat",
        kind: "llm",
        feature: "wiki_chat",
        source: "carrel/api/wiki_chat.py:wiki_chat",
        description:
          "Streaming LLM call (SSE) with a multi-page context block. Each page is labelled with its (kind:slug) so the model can cite it. Persists turns to chat_messages.",
      },
    ],
    output: "SSE token stream + chat_messages row (per turn)",
  },
];

export const PIPELINES_BY_ID: Record<string, Pipeline> = Object.fromEntries(
  PIPELINES.map((p) => [p.id, p]),
);
