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

export interface ScholarDetail {
  scholar: ScholarSummary;
  papers: PaperSummary[];
  profile: OpenAlexProfile | null;
}

export const listScholars = (q?: string) =>
  request<ScholarSummary[]>(`/scholars${q ? `?q=${encodeURIComponent(q)}` : ""}`);

export const getScholar = (key: string) =>
  request<ScholarDetail>(`/scholars/${encodeURIComponent(key)}`);

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
