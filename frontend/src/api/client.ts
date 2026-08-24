const API_BASE = "/api";

export class APIError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new APIError(res.status, text || res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---- Health ----

export interface Health {
  status: string;
  version: string;
  db: string;
  mineru: string;
  remote: boolean;
}

export const getHealth = () => request<Health>("/health");

// ---- Papers ----

export interface PaperSummary {
  id: string;
  title: string;
  venue: string | null;
  publication_date: string | null;
  authors: string[];
  oa_status: string;
  status: string;
  tldr_zh: string | null;
  tldr_en: string | null;
  keywords: string[];
  source: string;
  citation_count: number | null;
  in_library: boolean;
  discovered_at: string | null;
  favorite: boolean;
  tags: string[];
  topics: string[];
}

export interface PaperDetail extends PaperSummary {
  abstract: string | null;
  doi: string | null;
  arxiv_id: string | null;
  pdf_url: string | null;
  pdf_path: string | null;
  md_path: string | null;
  summary_zh: string | null;
  error: string | null;
  influential_citation_count: number | null;
  reference_count: number | null;
  citations_updated_at: string | null;
  notes_markdown: string | null;
  pdf_origin: string | null;
  journal_doi: string | null;
  pdf_files: Record<string, string> | null;
  published_checked_at: string | null;
  created_at: string;
  updated_at: string;
  author_list: AuthorRef[];
}

export interface AuthorRef {
  name: string;
  openalex_author_id: string;
  affiliation: string | null;
}

// ---- Scholars (authors aggregated across the library) ----

export interface ScholarSummary {
  key: string; // OpenAlex A-ID, or "name:<exact name>" when unknown
  name: string;
  affiliation: string | null;
  paper_count: number;
  first_year: number | null;
  last_year: number | null;
  total_citations: number;
  has_openalex: boolean;
}

export interface OpenAlexProfile {
  id: string;
  name: string | null;
  affiliation: string | null;
  works_count: number | null;
  cited_by_count: number | null;
  h_index: number | null;
  orcid: string | null;
  alternate_names: string[];
}

// ---- Wiki ----

export interface WikiSourceOut {
  paper_id: string;
  paper_title: string | null;
  year: number | null;
  heading: string | null;
  quote: string | null;
  role: string;
}

export interface WikiBacklink {
  id: number;
  kind: string;
  slug: string;
  title: string;
}

export interface WikiSource {
  kind: string;
  slug: string;
  title: string;
}

export interface WikiPageSummary {
  id: number;
  kind: string;
  slug: string;
  title: string;
  summary: string | null;
  tags: string[];
  links_in_count: number;
  confidence: number;
  evidence_count: number;
  scholar_aid: string | null;
  question_status: string | null;
  // True when the page is an evidence-threshold stub (concept/question with
  // < 3 backing papers). D.7: the wiki list shows a "待补证据" pill for these.
  stub: boolean;
  // Stable, kind-qualified identity the catalog reconciles against
  // (e.g. "scholar:A5013214678" or "scholar:name:he-li"). Redirect shells
  // have entity_key=null and redirects_to set to the canonical key.
  entity_key: string | null;
  redirects_to: string | null;
  compiled_at: string | null;
  updated_at: string | null;
}

export interface WikiPageDetail extends WikiPageSummary {
  path: string;
  frontmatter: Record<string, unknown>;
  body: string;
  sources: WikiSourceOut[];
  backlinks: WikiBacklink[];
  // Set when the slug the user requested now resolves to a redirect shell
  // — the server followed the redirect and tagged the response with the
  // original slug so the UI can show a "this page moved" notice.
  redirected_from: WikiPageSummary | null;
}

export const listWikiPages = (params?: {
  kind?: string;
  q?: string;
  limit?: number;
  offset?: number;
  includeRedirects?: boolean;
}) => {
  const query = new URLSearchParams();
  if (params?.kind) query.set("kind", params.kind);
  if (params?.q) query.set("q", params.q);
  if (params?.limit !== undefined) query.set("limit", String(params.limit));
  if (params?.offset !== undefined) query.set("offset", String(params.offset));
  // Default is to hide redirects; pass `false` explicitly to be safe with
  // FastAPI's optional-bool semantics.
  if (params?.includeRedirects) query.set("include_redirects", "true");
  const qs = query.toString();
  return request<WikiPageSummary[]>(`/wiki/pages${qs ? `?${qs}` : ""}`);
};

export const getWikiPage = (id: number) => request<WikiPageDetail>(`/wiki/pages/${id}`);

export const getWikiPageBySlug = (kind: string, slug: string) =>
  request<WikiPageDetail>(
    `/wiki/pages/by-kind-slug/${encodeURIComponent(kind)}/${encodeURIComponent(slug)}`,
  );

export const compileWiki = (body: {
  limit?: number;
  background?: boolean;
  force?: boolean;
}) => request<Job>("/wiki/compile", { method: "POST", body: JSON.stringify(body) });

export const recompileWikiPage = (id: number) =>
  request<Job>(`/wiki/pages/${id}/recompile`, { method: "POST" });

export interface ScholarDetail {
  scholar: ScholarSummary;
  papers: PaperSummary[];
  profile: OpenAlexProfile | null;
  wiki_page: WikiPageDetail | null;
}

export const listScholars = (q?: string) =>
  request<ScholarSummary[]>(`/scholars${q ? `?q=${encodeURIComponent(q)}` : ""}`);

export const getScholar = (key: string) =>
  request<ScholarDetail>(`/scholars/${encodeURIComponent(key)}`);

export interface ScholarWork {
  openalex_id: string;
  title: string;
  year: number | null;
  venue: string | null;
  doi: string | null;
  arxiv_id: string | null;
  cited_by_count: number | null;
  is_oa: boolean;
  pdf_url: string | null;
  in_library: boolean;
  library_id: string | null;
}

export interface ScholarWorksResponse {
  items: ScholarWork[];
  next_cursor: string | null;
  // OpenAlex's reported total work count for this author — same on every
  // page. ``null`` when the count couldn't be read; the UI shows just
  // "Showing N" without "of M" in that case.
  total: number | null;
}

export const getScholarWorks = (
  key: string,
  opts: { cursor?: string | null; limit?: number; signal?: AbortSignal } = {},
) => {
  const params = new URLSearchParams();
  if (opts.cursor) params.set("cursor", opts.cursor);
  if (opts.limit) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return request<ScholarWorksResponse>(
    `/scholars/${encodeURIComponent(key)}/works${qs ? `?${qs}` : ""}`,
    opts.signal ? { signal: opts.signal } : undefined,
  );
};

export interface CitationItem {
  title: string | null;
  year: number | null;
  venue: string | null;
  doi: string | null;
  arxiv_id: string | null;
  s2_paper_id: string | null;
  openalex_id: string | null;
  in_library: boolean;
  paper_id: string | null;
}

export interface CitationList {
  paper_id: string;
  citation_count: number | null;
  influential_citation_count: number | null;
  reference_count: number | null;
  updated_at: string | null;
  truncated: boolean;
  citing: CitationItem[];
  next_offset: number | null;
  source: "cache" | "openalex";
  cached_count: number;
}

export const listPapers = (params?: {
  limit?: number;
  offset?: number;
  status?: string;
  venue?: string;
  in_library?: boolean;
  favorite?: boolean;
  tag?: string[];
  topic?: string[];
  q?: string;
  sort?: string;
}) => {
  const q = new URLSearchParams();
  if (params?.limit) q.set("limit", String(params.limit));
  if (params?.offset) q.set("offset", String(params.offset));
  if (params?.status) q.set("status", params.status);
  if (params?.venue) q.set("venue", params.venue);
  if (params?.in_library !== undefined) q.set("in_library", String(params.in_library));
  if (params?.favorite !== undefined) q.set("favorite", String(params.favorite));
  if (params?.q) q.set("q", params.q);
  if (params?.sort) q.set("sort", params.sort);
  if (params?.tag) for (const t of params.tag) q.append("tag", t);
  if (params?.topic) for (const t of params.topic) q.append("topic", t);
  const qs = q.toString();
  return request<PaperSummary[]>(`/papers${qs ? `?${qs}` : ""}`);
};

export const getPaper = (id: string) => request<PaperDetail>(`/papers/${encodeURIComponent(id)}`);

export const deletePaper = (id: string) =>
  request<{ id: string; deleted: boolean; removed_files: boolean }>(
    `/papers/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );

// Inbox → library (for papers discovered by sync). Metadata-only; the paper
// stays "pending" until the user runs Process pending.
export const importPaperToLibrary = (id: string) =>
  request<{ id: string; imported: boolean; in_library: boolean }>(
    `/papers/${encodeURIComponent(id)}/import`,
    { method: "POST" },
  );

// Soft-remove a discovered paper from the inbox (inverse of import for inbox rows).
export const discardPaper = (id: string) =>
  request<{ id: string; discarded: boolean }>(
    `/papers/${encodeURIComponent(id)}/discard`,
    { method: "POST" },
  );

export const getPaperMarkdown = (id: string) =>
  request<{ id: string; body: string | null; md_path: string | null }>(
    `/papers/${encodeURIComponent(id)}/markdown`
  );

// ---- Per-paper chat transcript (server-persisted) ----

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatMessage extends ChatTurn {
  id: number;
  created_at?: string;
  updated_at?: string;
}

export const getChatMessages = (id: string) =>
  request<{ paper_id: string; messages: ChatMessage[]; updated_at: string | null }>(
    `/papers/${encodeURIComponent(id)}/chat/messages`,
  );

export const saveChatMessages = (id: string, messages: ChatTurn[]) =>
  request<{ paper_id: string; messages: ChatMessage[]; updated_at: string | null }>(
    `/papers/${encodeURIComponent(id)}/chat/messages`,
    { method: "PUT", body: JSON.stringify({ messages }) },
  );

// ---- Wiki-wide chat transcript (server-persisted) ----

export const getWikiChatMessages = () =>
  request<{ messages: ChatMessage[]; updated_at: string | null }>(
    `/wiki/chat/messages`,
  );

export const saveWikiChatMessages = (messages: ChatTurn[]) =>
  request<{ messages: ChatMessage[]; updated_at: string | null }>(
    `/wiki/chat/messages`,
    { method: "PUT", body: JSON.stringify({ messages }) },
  );

// ---- Annotations: favorites, notes, tags ----

export interface Tag {
  id: number;
  name: string;
}

export interface TagWithCount extends Tag {
  paper_count: number;
}

export const toggleFavorite = (id: string, favorite: boolean) =>
  request<{ id: string; favorite: boolean }>(
    `/papers/${encodeURIComponent(id)}/favorite`,
    { method: "POST", body: JSON.stringify({ favorite }) },
  );

export const saveNotes = (id: string, notes_markdown: string) =>
  request<{ id: string; notes_markdown: string | null; updated_at: string }>(
    `/papers/${encodeURIComponent(id)}/notes`,
    { method: "PUT", body: JSON.stringify({ notes_markdown }) },
  );

export const listPaperTags = (id: string) =>
  request<Tag[]>(`/papers/${encodeURIComponent(id)}/tags`);

export const addPaperTag = (id: string, name: string) =>
  request<Tag>(`/papers/${encodeURIComponent(id)}/tags`, {
    method: "POST",
    body: JSON.stringify({ name }),
  });

export const removePaperTag = (id: string, tagId: number) =>
  request<{ id: number; paper_id: string; detached: boolean }>(
    `/papers/${encodeURIComponent(id)}/tags/${tagId}`,
    { method: "DELETE" },
  );

export const listTags = () => request<TagWithCount[]>("/tags");

export const getPaperCitations = (
  id: string,
  opts?: { sort?: "year_asc" | "year_desc" | "cited_desc"; offset?: number; limit?: number },
) => {
  const q = new URLSearchParams();
  if (opts?.sort) q.set("sort", opts.sort);
  if (opts?.offset !== undefined) q.set("offset", String(opts.offset));
  if (opts?.limit !== undefined) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return request<CitationList>(`/papers/${encodeURIComponent(id)}/citations${qs ? `?${qs}` : ""}`);
};

export const refreshPaperCitations = (id: string, background = true) =>
  request<Job>(`/papers/${encodeURIComponent(id)}/refresh-citations`, {
    method: "POST",
    body: JSON.stringify({ background }),
  });

// Check an arXiv paper for a published journal version (fetch + swap to it).
export const checkPublication = (id: string, background = true, force = false) =>
  request<Job>(`/papers/${encodeURIComponent(id)}/check-publication`, {
    method: "POST",
    body: JSON.stringify({ background, force }),
  });

export interface ReferenceList {
  paper_id: string;
  reference_count: number | null;
  updated_at: string | null;
  references: CitationItem[];
}

export const getPaperReferences = (
  id: string,
  opts?: { sort?: "year_asc" | "year_desc" },
) => {
  const q = new URLSearchParams();
  if (opts?.sort) q.set("sort", opts.sort);
  const qs = q.toString();
  return request<ReferenceList>(
    `/papers/${encodeURIComponent(id)}/references${qs ? `?${qs}` : ""}`,
  );
};

// ---- Processing (download PDF + parse with MinerU) ----

export const processPaper = (paperId: string, background = false) =>
  request<Job[]>("/process", {
    method: "POST",
    body: JSON.stringify({ paper_id: paperId, background }),
  });

export const processPending = (limit = 10, background = false) =>
  request<Job[]>("/process", {
    method: "POST",
    body: JSON.stringify({ limit, background }),
  });

// ---- Subscriptions ----

export interface Subscription {
  id: number;
  kind: "keyword" | "author" | "venue" | "arxiv_category";
  value: string;
  label: string | null;
  enabled: boolean;
  created_at: string;
}

export const listSubscriptions = () => request<Subscription[]>("/subscriptions");

export const addTopJournals = () =>
  request<Subscription[]>("/subscriptions/top-journals", { method: "POST" });

export const createSubscription = (body: {
  kind: Subscription["kind"];
  value: string;
  label?: string;
  enabled?: boolean;
}) =>
  request<Subscription>("/subscriptions", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const deleteSubscription = (id: number) =>
  request<{ deleted: boolean }>(`/subscriptions/${id}`, { method: "DELETE" });

// ---- Sync / Jobs ----

export interface Job {
  id: number;
  kind: string;
  status: string;
  message: string | null;
  stats: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  progress?: number;
}

export const triggerSync = (lookbackHours = 24, background = false) =>
  request<Job>("/sync", {
    method: "POST",
    body: JSON.stringify({ lookback_hours: lookbackHours, background }),
  });

export interface JobFilter {
  kind?: string;
  status?: string;
  limit?: number;
}

export const listJobs = (filter: JobFilter = {}) => {
  const params = new URLSearchParams();
  if (filter.kind) params.set("kind", filter.kind);
  if (filter.status) params.set("status", filter.status);
  if (filter.limit) params.set("limit", String(filter.limit));
  const q = params.toString();
  return request<Job[]>(`/sync/jobs${q ? `?${q}` : ""}`);
};

export const getJob = (id: number) => request<Job>(`/sync/jobs/${id}`);

// -------- Schedule / cron --------

export interface ScheduledJob {
  id: string;
  label: string;
  description: string;
  enabled: boolean;
  cron: string;
  running: boolean;
  next_run_at: string | null;
  last_status: string | null;
  last_started_at: string | null;
  last_finished_at: string | null;
  last_message: string | null;
  last_stats: Record<string, unknown> | null;
  requires: string | null;
  requirement_satisfied: boolean;
}

export interface SchedulerStatus {
  enabled: boolean;
  jobs: ScheduledJob[];
}

export const getSchedule = () =>
  request<SchedulerStatus>("/schedule");

export const updateSchedule = (body: {
  enabled?: boolean;
  sync_cron?: string;
  remote_fill_enabled?: boolean;
  remote_fill_cron?: string;
  publication_check_enabled?: boolean;
  publication_check_cron?: string;
}) =>
  request<SchedulerStatus>("/schedule", {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const runScheduledJob = (jobId: string) =>
  request<{ job_id: string; running: boolean; message: string }>(
    `/schedule/${encodeURIComponent(jobId)}/run`,
    { method: "POST" },
  );

// -------- Settings (M12) --------

// One field on a SerialisedSection that .env is overriding. env_value is
// only populated for non-secret env vars; for secret overrides
// (OPENALEX_API_KEY, S2_API_KEY) it's null and the UI shows a generic
// "set in .env" badge.
export interface EnvOverride {
  env_var: string;
  env_value?: string | null;
}

export interface SerialisedSection {
  values: Record<string, unknown>;
  env_overrides: Record<string, EnvOverride>;
  requires_restart: boolean;
}

export interface EnvEntry {
  name: string;
  label: string;
  is_secret: boolean;
  is_set: boolean;
  value?: string | null;
}

export interface Settings {
  yaml_path: string;
  sections: Record<string, SerialisedSection>;
  env: EnvEntry[];
  restart_required_sections: string[];
}

export const getSettings = () => request<Settings>("/settings");

export const updateSettings = (
  sections: Record<string, Record<string, unknown>>,
) =>
  request<Settings>("/settings", {
    method: "PATCH",
    body: JSON.stringify({ sections }),
  });

// -------- Search (M5) --------

export type SearchSource = "library" | "openalex" | "semantic_scholar" | "arxiv";

export interface SearchResultIds {
  openalex: string | null;
  doi: string | null;
  arxiv: string | null;
  s2: string | null;
}

export interface SearchResultItem {
  title: string;
  authors: string[];
  abstract: string | null;
  venue: string | null;
  venue_type: string | null;
  publication_date: string | null;
  citation_count: number | null;
  tldr: string | null;
  pdf_url: string | null;
  snippet: string | null;
  ids: SearchResultIds;
  sources: SearchSource[];
  in_library: boolean;
  library_id: string | null;
  status: string | null;
}

export interface SearchResponse {
  query: string;
  corrected_from: string | null;
  results: SearchResultItem[];
  warnings: string[];
}

export interface SearchOptions {
  limit?: number;
  correct?: boolean;
  yearFrom?: number;
  yearTo?: number;
  minCitations?: number;
  openAccessOnly?: boolean;
  sort?: "relevance" | "citations" | "date";
  sources?: SearchSource[];
}

export const searchPapers = (q: string, opts: SearchOptions = {}) => {
  const params = new URLSearchParams();
  params.set("q", q);
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.correct !== undefined) params.set("correct", opts.correct ? "true" : "false");
  if (opts.yearFrom !== undefined) params.set("year_from", String(opts.yearFrom));
  if (opts.yearTo !== undefined) params.set("year_to", String(opts.yearTo));
  if (opts.minCitations !== undefined) params.set("min_citations", String(opts.minCitations));
  if (opts.openAccessOnly) params.set("open_access_only", "true");
  if (opts.sort) params.set("sort", opts.sort);
  if (opts.sources && opts.sources.length > 0) {
    for (const s of opts.sources) params.append("sources", s);
  }
  return request<SearchResponse>(`/search?${params.toString()}`);
};

export interface SemanticHit {
  paper_id: string;
  chunk_index: number;
  heading: string | null;
  snippet: string;
  score: number;
}

export interface SemanticSearchResult {
  id: string;
  title: string;
  venue: string | null;
  publication_date: string | null;
  authors: string[];
  doi: string | null;
  arxiv_id: string | null;
  status: string | null;
  best_score: number;
  hits: SemanticHit[];
}

export interface SemanticSearchResponse {
  query: string;
  corrected_from: string | null;
  results: SemanticSearchResult[];
}

export const searchSemantic = (q: string, limit = 10, correct = true) =>
  request<SemanticSearchResponse>(
    `/search/semantic?q=${encodeURIComponent(q)}&limit=${limit}&correct=${correct ? "true" : "false"}`,
  );

// ---- Embedding (M5: chunk + embed parsed papers) ----

export const embedPaper = (paperId: string, background = false) =>
  request<Job[]>("/embed", {
    method: "POST",
    body: JSON.stringify({ paper_id: paperId, background }),
  });

export const embedPending = (limit = 20, background = false) =>
  request<Job[]>("/embed", {
    method: "POST",
    body: JSON.stringify({ limit, background }),
  });

// ---- Summarization (M4: LLM TL;DR + Chinese abstract + keywords) ----

export const summarizePaper = (
  paperId: string,
  background = false,
  force = false,
) =>
  request<Job[]>("/summarize", {
    method: "POST",
    body: JSON.stringify({ paper_id: paperId, background, force }),
  });

export const summarizePending = (limit = 20, background = false) =>
  request<Job[]>("/summarize", {
    method: "POST",
    body: JSON.stringify({ limit, background }),
  });

// ---- Topics (LLM classification into research themes) ----

export interface TopicWithCount {
  id: number;
  name: string;
  description: string | null;
  paper_count: number;
}

export const listTopics = () => request<TopicWithCount[]>("/topics");

// ---- Authors backfill (resolve OpenAlex Author IDs) ----

export const backfillAuthors = (
  opts: { paperId?: string; limit?: number; background?: boolean } = {},
) =>
  request<Job[]>("/authors-backfill", {
    method: "POST",
    body: JSON.stringify({
      paper_id: opts.paperId,
      limit: opts.limit ?? 100,
      background: opts.background ?? false,
    }),
  });

// ---- Scholar dedup (merge duplicate OpenAlex A-IDs) ----

export interface DedupSuggestion {
  a: string;
  b: string;
  display_name: string | null;
  score: number;
  coauthor: number;
  affiliation: number;
  topic: number;
  reasons: string[];
  paper_counts: Record<string, number>;
  affiliations: Record<string, string | null>;
}

export interface DedupAlias {
  alias_aid: string;
  canonical_aid: string;
  display_name: string | null;
  source: "auto" | "user" | "reject";
  confidence: number;
  reasons: string[];
}

export interface DedupSnapshot {
  suggestions: DedupSuggestion[];
  applied: DedupAlias[];
  rejected: DedupAlias[];
}

export const getDedupSnapshot = (signal?: AbortSignal) =>
  request<DedupSnapshot>("/scholar-dedup/suggestions", { signal });

export const runDedup = (opts: { autoApply?: boolean; background?: boolean } = {}) =>
  request<Job>("/scholar-dedup/run", {
    method: "POST",
    body: JSON.stringify({
      auto_apply: opts.autoApply ?? true,
      background: opts.background ?? true,
    }),
  });

export const mergeScholar = (body: {
  alias_aid: string;
  canonical_aid: string;
  display_name?: string | null;
}) =>
  request<DedupAlias>("/scholar-dedup/merge", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const rejectScholarPair = (body: {
  a: string;
  b: string;
  display_name?: string | null;
}) =>
  request<DedupAlias>("/scholar-dedup/reject", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const deleteScholarAlias = (aliasAid: string, canonicalAid: string) =>
  request<{ deleted: boolean }>(
    `/scholar-dedup/aliases/${encodeURIComponent(aliasAid)}/${encodeURIComponent(canonicalAid)}`,
    { method: "DELETE" },
  );

// ---- Paper dedup (merge duplicate paper rows: DOI / arXiv / s2 / bridge) ----

export interface PaperDedupSuggestion {
  a: string;
  b: string;
  score: number;
  title: number;
  authors: number;
  strong_anchors: string[];
  reasons: string[];
  llm_verdict: {
    verdict: string;
    confidence: number;
    model: string | null;
    reasons: string[];
  } | null;
  title_a: string | null;
  title_b: string | null;
  year_a: number | null;
  year_b: number | null;
  doi_a: string | null;
  doi_b: string | null;
  arxiv_id_a: string | null;
  arxiv_id_b: string | null;
  s2_paper_id_a: string | null;
  s2_paper_id_b: string | null;
}

export interface PaperDedupAlias {
  alias_paper_id: string;
  canonical_paper_id: string;
  display_label: string | null;
  source: "auto" | "user" | "llm" | "reject";
  confidence: number;
  reasons: string[];
}

export interface PaperDedupComponent {
  canonical_id: string;
  alias_ids: string[];
  display_label: string | null;
  reasons: string[];
  avg_score: number;
  sources: string[];
}

export interface PaperDedupSnapshot {
  suggestions: PaperDedupSuggestion[];
  applied: PaperDedupAlias[];
  rejected: PaperDedupAlias[];
  components: PaperDedupComponent[];
}

export const getPaperDedupSnapshot = (signal?: AbortSignal) =>
  request<PaperDedupSnapshot>("/paper-dedup/suggestions", { signal });

export const runPaperDedup = (opts: { autoApply?: boolean; background?: boolean } = {}) =>
  request<Job>("/paper-dedup/run", {
    method: "POST",
    body: JSON.stringify({
      auto_apply: opts.autoApply ?? true,
      background: opts.background ?? true,
    }),
  });

export const mergePaper = (body: {
  alias_paper_id: string;
  canonical_paper_id: string;
  display_label?: string | null;
}) =>
  request<PaperDedupAlias>("/paper-dedup/merge", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const rejectPaperPair = (body: {
  a: string;
  b: string;
  display_label?: string | null;
}) =>
  request<PaperDedupAlias>("/paper-dedup/reject", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const deletePaperAlias = (aliasPaperId: string, canonicalPaperId: string) =>
  request<{ deleted: boolean }>(
    `/paper-dedup/aliases/${encodeURIComponent(aliasPaperId)}/${encodeURIComponent(canonicalPaperId)}`,
    { method: "DELETE" },
  );

export const classifyTopics = (
  opts: { paperId?: string; limit?: number; background?: boolean; force?: boolean } = {},
) =>
  request<Job[]>("/topics", {
    method: "POST",
    body: JSON.stringify({
      paper_id: opts.paperId,
      limit: opts.limit ?? 20,
      background: opts.background ?? false,
      force: opts.force ?? false,
    }),
  });

export const importPaper = (body: {
  openalex_id?: string;
  doi?: string;
  arxiv_id?: string;
  s2?: string;
  title?: string;
}) =>
  request<{ id: string; created: boolean }>("/import", {
    method: "POST",
    body: JSON.stringify(body),
  });

// ---- Token usage (M13) ----

export interface UsageSummary {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
}

export interface UsageBucket {
  key: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
}

export interface UsageDay {
  day: string; // YYYY-MM-DD, oldest first
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
}

export interface UsageRecent {
  id: number;
  created_at: string | null;
  model: string;
  feature: string;
  job_id: number | null;
  paper_id: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export const getUsageSummary = (sinceDays?: number) => {
  const q = sinceDays ? `?since_days=${sinceDays}` : "";
  return request<UsageSummary>(`/usage/summary${q}`);
};

export const getUsageByModel = (sinceDays?: number) => {
  const q = sinceDays ? `?since_days=${sinceDays}` : "";
  return request<UsageBucket[]>(`/usage/by-model${q}`);
};

export const getUsageByFeature = (sinceDays?: number) => {
  const q = sinceDays ? `?since_days=${sinceDays}` : "";
  return request<UsageBucket[]>(`/usage/by-feature${q}`);
};

export const getUsageByDay = (days = 30) =>
  request<UsageDay[]>(`/usage/by-day?days=${days}`);

export const getUsageRecent = (limit = 20) =>
  request<UsageRecent[]>(`/usage/recent?limit=${limit}`);
