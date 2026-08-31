// Single source of truth for React Query key shapes. Components import the
// factory; mutations pass its output to invalidateQueries({ queryKey: ... })
// so the invalidation and the consumer are guaranteed to agree.
//
// Conventions (from cosmic-marinating-locket.md "React Query key conventions"):
// - First segment = resource ("papers", "paper", "topics", "tags", ...).
// - Second segment = id (string) where applicable. Sub-resources nest.
// - Filter objects are always object literals (stable key order).
// - invalidateQueries({ queryKey: ["paper", id] }) cascades to every nested
//   variant: ["paper", id, "markdown"], ["paper", id, "tags"], etc.

export interface PaperFilters {
  status?: string;
  venue?: string;
  in_library?: boolean;
  favorite?: boolean;
  tag?: string[];
  topic?: string[];
  q?: string;
  sort?: string;
  limit?: number;
  offset?: number;
}

export interface CitationListParams {
  sort: "year_asc" | "year_desc" | "cited_desc";
  offset: number;
  limit: number;
}

export const queryKeys = {
  // Library list root — invalidating this cascades to every ["papers", ...]
  // variant.
  papersRoot: () => ["papers"] as const,
  papersList: (f: PaperFilters) => ["papers", f] as const,

  // Per-paper — the leaf query holds the canonical record; the nested
  // children cover markdown body, citations, references, and the per-paper
  // tag list. invalidateQueries({queryKey: ["paper", id]}) hits all of them.
  paper: (id: string) => ["paper", id] as const,
  paperMarkdown: (id: string) => ["paper", id, "markdown"] as const,
  paperSections: (id: string) => ["paper", id, "sections"] as const,
  paperCitations: (id: string, p: CitationListParams) =>
    ["paper", id, "citations", p] as const,
  paperReferences: (id: string, sort: "year_asc" | "year_desc") =>
    ["paper", id, "references", sort] as const,
  paperTags: (id: string) => ["paper", id, "tags"] as const,
  paperCard: (id: string) => ["paper", id, "card"] as const,

  // Disabled-placeholder keys for routes that hold a route-param id which
  // may be undefined on first render. Pair with ``enabled: Boolean(id)``
  // so the query never fires against the placeholder; the placeholder
  // still has to be a real key (React Query requires it) and should
  // never collide with a real per-paper key. The literal string
  // ``"_"`` is reserved: no real paper id starts with it.
  missingPaper: () => ["paper", "_"] as const,
  missingPaperMarkdown: () => ["paper", "_", "markdown"] as const,
  missingPaperSections: () => ["paper", "_", "sections"] as const,
  missingPaperTags: () => ["paper", "_", "tags"] as const,

  // Aggregations — these rarely change; their mutations invalidate
  // explicitly so we can pin staleTime: Infinity on the consumer.
  topics: () => ["topics"] as const,
  tags: () => ["tags"] as const,
  settings: () => ["settings"] as const,
} as const;
