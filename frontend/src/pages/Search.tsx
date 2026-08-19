import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import { useNavigate } from "react-router-dom";
import {
  Search as SearchIcon,
  ExternalLink,
  FileDown,
  Library as LibraryIcon,
  History as HistoryIcon,
  Star,
  X,
  FileText,
  AlertTriangle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  importPaper,
  searchPapers,
  searchSemantic,
  type SearchResultItem,
  type SearchSource,
  type SemanticSearchResult,
} from "@/api/client";

const HISTORY_KEY = "carrel.search.history";
const FAVORITES_KEY = "carrel.search.favorites";
const MAX_SAVED = 20;

const SOURCE_LABELS: Record<SearchSource, string> = {
  library: "Library",
  openalex: "OA",
  semantic_scholar: "S2",
  arxiv: "arXiv",
};

const SOURCE_BADGE_STYLES: Record<SearchSource, string> = {
  library: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
  openalex: "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200",
  semantic_scholar: "bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-200",
  arxiv: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
};

type SortKey = "relevance" | "citations" | "date";
type ChipFilter = "all" | SearchSource;

function useLocalList(key: string): [string[], Dispatch<SetStateAction<string[]>>] {
  const [list, setList] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return [];
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      return arr.filter((x): x is string => typeof x === "string").slice(0, MAX_SAVED);
    } catch {
      return [];
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(list));
    } catch {
      // localStorage may be unavailable (private mode, quota); ignore.
    }
  }, [key, list]);
  return [list, setList];
}

function Highlight({ text, q }: { text: string; q: string }) {
  if (!q || !text) return <>{text}</>;
  const needle = q.toLowerCase();
  if (!needle) return <>{text}</>;
  const lower = text.toLowerCase();
  const out: React.ReactNode[] = [];
  let i = 0;
  while (i < text.length) {
    const j = lower.indexOf(needle, i);
    if (j < 0) {
      out.push(text.slice(i));
      break;
    }
    if (j > i) out.push(text.slice(i, j));
    out.push(
      <mark
        key={j}
        className="rounded bg-yellow-200 px-0.5 text-foreground dark:bg-yellow-700/40"
      >
        {text.slice(j, j + needle.length)}
      </mark>,
    );
    i = j + needle.length;
  }
  return <>{out}</>;
}

function ChipRow({
  icon,
  items,
  onPick,
  onRemove,
}: {
  icon: React.ReactNode;
  items: string[];
  onPick: (q: string) => void;
  onRemove: (q: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="inline-flex items-center text-xs text-muted-foreground">{icon}</span>
      {items.map((it) => (
        <span
          key={it}
          className="inline-flex items-center gap-1 rounded-full border bg-muted/40 px-2 py-0.5 text-xs"
        >
          <button onClick={() => onPick(it)} className="hover:underline" title={it}>
            {it}
          </button>
          <button
            onClick={() => onRemove(it)}
            className="text-muted-foreground hover:text-foreground"
            aria-label={`Remove ${it}`}
          >
            <X className="h-3 w-3" />
          </button>
        </span>
      ))}
    </div>
  );
}

type Tab = "metadata" | "fulltext";

export default function Search() {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [submitted, setSubmitted] = useState("");
  // The literal string most recently sent to the backend, and whether
  // spelling correction was allowed for it. Re-running on facet changes reuses
  // this so we don't re-correct the same query each time the user nudges a
  // filter.
  const [activeQuery, setActiveQuery] = useState<{ text: string; correct: boolean } | null>(null);
  const [tab, setTab] = useState<Tab>("metadata");
  const [results, setResults] = useState<SearchResultItem[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [semantic, setSemantic] = useState<SemanticSearchResult[]>([]);
  const [correctedFrom, setCorrectedFrom] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [importing, setImporting] = useState<string | null>(null);
  const [imported, setImported] = useState<Record<string, string>>({});
  const [history, setHistory] = useLocalList(HISTORY_KEY);
  const [favorites, setFavorites] = useLocalList(FAVORITES_KEY);

  // Facets.
  const [chipFilter, setChipFilter] = useState<ChipFilter>("all");
  const [sort, setSort] = useState<SortKey>("relevance");
  const [yearPreset, setYearPreset] = useState<string>("any");
  const [yearFrom, setYearFrom] = useState<number | undefined>();
  const [yearTo, setYearTo] = useState<number | undefined>();
  const [minCitations, setMinCitations] = useState<number | undefined>();
  const [oaOnly, setOaOnly] = useState(false);

  const trimmedQ = q.trim();
  const isFav = favorites.some((f) => f.toLowerCase() === trimmedQ.toLowerCase());

  const currentYear = new Date().getFullYear();

  // Apply year preset when it changes.
  useEffect(() => {
    if (yearPreset === "any") {
      setYearFrom(undefined);
      setYearTo(undefined);
    } else if (yearPreset === "1") {
      setYearFrom(currentYear - 1);
      setYearTo(currentYear);
    } else if (yearPreset === "3") {
      setYearFrom(currentYear - 3);
      setYearTo(currentYear);
    } else if (yearPreset === "5") {
      setYearFrom(currentYear - 5);
      setYearTo(currentYear);
    }
    // "custom" leaves the inputs alone.
  }, [yearPreset, currentYear]);

  function pickFromChip(query: string) {
    setQ(query);
    runSearch(query, true);
  }

  function toggleFavorite() {
    const query = q.trim();
    if (!query) return;
    setFavorites((prev) => {
      const idx = prev.findIndex((f) => f.toLowerCase() === query.toLowerCase());
      if (idx >= 0) return prev.filter((_, i) => i !== idx);
      return [query, ...prev].slice(0, MAX_SAVED);
    });
  }

  async function runSearch(query: string, correct = true) {
    if (!query.trim()) return;
    setLoading(true);
    setErr(null);
    if (!correct) setCorrectedFrom(null);
    setSubmitted(query);
    setActiveQuery({ text: query, correct });
    setHistory((prev) => {
      const dedup = prev.filter((x) => x.toLowerCase() !== query.toLowerCase());
      return [query, ...dedup].slice(0, MAX_SAVED);
    });
    const [metaResult] = await Promise.all([
      searchPapers(query, {
        limit: 30,
        correct,
        sort,
        yearFrom,
        yearTo,
        minCitations,
        openAccessOnly: oaOnly,
      }).catch((e) => {
        setErr((prev) => prev ?? `Metadata search failed: ${e}`);
        return null;
      }),
      searchSemantic(query, 10, correct)
        .then((r) => setSemantic(r.results))
        .catch((e) => {
          console.warn("semantic search failed", e);
          setSemantic([]);
        }),
    ]);
    if (metaResult) {
      setResults(metaResult.results);
      setWarnings(metaResult.warnings);
      setSubmitted(metaResult.query);
      if (correct) setCorrectedFrom(metaResult.corrected_from);
    } else {
      setResults([]);
      setWarnings([]);
    }
    setLoading(false);
  }

  // Live search as the user types — spelling correction on (first search of
  // a new literal string).
  useEffect(() => {
    const text = q.trim();
    if (!text) return;
    if (activeQuery && activeQuery.text === text) return;
    const t = setTimeout(() => runSearch(text, true), 350);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  // Re-run the active query (without re-correcting spelling) whenever facets
  // change.
  useEffect(() => {
    if (!activeQuery) return;
    const t = setTimeout(
      () => runSearch(activeQuery.text, activeQuery.correct),
      250,
    );
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sort, yearFrom, yearTo, minCitations, oaOnly]);

  async function onImport(r: SearchResultItem) {
    const key = r.ids.openalex || r.ids.doi || r.ids.arxiv || r.ids.s2 || r.title;
    setImporting(key);
    setErr(null);
    try {
      const out = await importPaper({
        openalex_id: r.ids.openalex ?? undefined,
        doi: r.ids.doi ?? undefined,
        arxiv_id: r.ids.arxiv ?? undefined,
        s2: r.ids.s2 ?? undefined,
      });
      setImported((prev) => ({ ...prev, [key]: out.id }));
    } catch (e) {
      setErr(`Import failed: ${e}`);
    } finally {
      setImporting(null);
    }
  }

  const historyOnly = history.filter(
    (h) => !favorites.some((f) => f.toLowerCase() === h.toLowerCase()),
  );

  // Client-side source chip filter.
  const counts = useMemo(() => {
    const c: Record<ChipFilter, number> = {
      all: results.length,
      library: 0,
      openalex: 0,
      semantic_scholar: 0,
      arxiv: 0,
    };
    for (const r of results) {
      for (const s of r.sources) c[s] += 1;
    }
    return c;
  }, [results]);

  const visible = useMemo(() => {
    if (chipFilter === "all") return results;
    return results.filter((r) => r.sources.includes(chipFilter));
  }, [results, chipFilter]);

  return (
    <main className="container max-w-screen-2xl space-y-6 py-8">
      <header className="space-y-2">
        <h1 className="flex items-center gap-2 text-2xl font-bold">
          <SearchIcon className="h-5 w-5" />
          Search
        </h1>
        <p className="text-sm text-muted-foreground">
          Search your library, OpenAlex, Semantic Scholar, and arXiv together.
          Results are merged and deduplicated by DOI / arXiv id.
        </p>
      </header>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          runSearch(q.trim());
        }}
        className="flex gap-2"
      >
        <input
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search title, author, DOI, arXiv id, abstract…"
          className="h-10 flex-1 rounded-md border border-input bg-background px-3 text-sm"
        />
        <Button type="submit" disabled={loading || !q.trim()}>
          {loading ? "Searching…" : "Search"}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="icon"
          onClick={toggleFavorite}
          disabled={!trimmedQ}
          aria-label={isFav ? "Remove from favorites" : "Save to favorites"}
          title={isFav ? "Remove from favorites" : "Save search to favorites"}
        >
          <Star
            className={
              isFav ? "h-4 w-4 fill-yellow-400 text-yellow-500" : "h-4 w-4"
            }
          />
        </Button>
      </form>

      {favorites.length > 0 && (
        <ChipRow
          icon={<Star className="h-3 w-3 fill-yellow-400 text-yellow-500" />}
          items={favorites}
          onPick={pickFromChip}
          onRemove={(f) => setFavorites((prev) => prev.filter((x) => x !== f))}
        />
      )}
      {historyOnly.length > 0 && (
        <ChipRow
          icon={<HistoryIcon className="h-3 w-3" />}
          items={historyOnly}
          onPick={pickFromChip}
          onRemove={(h) => setHistory((prev) => prev.filter((x) => x !== h))}
        />
      )}

      {err && <p className="text-sm text-red-600">{err}</p>}

      {warnings.length > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-amber-300/60 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-500/40 dark:bg-amber-950/40 dark:text-amber-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <p className="font-medium">Some sources were unavailable:</p>
            <ul className="list-disc pl-5">
              {warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {correctedFrom && submitted && !loading && (
        <div className="rounded-md border border-border/60 bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
          Showing results for{" "}
          <span className="font-medium text-foreground">{submitted}</span>
          {" — "}
          <button
            onClick={() => {
              setQ(correctedFrom);
              runSearch(correctedFrom, false);
            }}
            className="hover:underline"
          >
            search original &quot;{correctedFrom}&quot;
          </button>
        </div>
      )}

      {submitted && !loading && (
        <div className="flex gap-1 border-b" role="tablist" aria-label="Search source">
          <TabButton
            active={tab === "metadata"}
            onClick={() => setTab("metadata")}
            icon={<LibraryIcon className="h-3.5 w-3.5" />}
            label="Papers"
            count={results.length}
          />
          <TabButton
            active={tab === "fulltext"}
            onClick={() => setTab("fulltext")}
            icon={<FileText className="h-3.5 w-3.5" />}
            label="Full-text"
            count={semantic.length}
          />
        </div>
      )}

      {submitted && !loading && tab === "metadata" && (
        <div className="space-y-4">
          {/* Facet bar */}
          <div className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/20 p-2 text-xs">
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as SortKey)}
              className="h-8 rounded border border-input bg-background px-2"
              aria-label="Sort"
            >
              <option value="relevance">Sort: Relevance</option>
              <option value="citations">Sort: Most cited</option>
              <option value="date">Sort: Newest</option>
            </select>
            <select
              value={yearPreset}
              onChange={(e) => setYearPreset(e.target.value)}
              className="h-8 rounded border border-input bg-background px-2"
              aria-label="Year"
            >
              <option value="any">Any time</option>
              <option value="1">Last year</option>
              <option value="3">Last 3 years</option>
              <option value="5">Last 5 years</option>
              <option value="custom">Custom…</option>
            </select>
            {yearPreset === "custom" && (
              <>
                <input
                  type="number"
                  placeholder="From"
                  value={yearFrom ?? ""}
                  onChange={(e) => setYearFrom(e.target.value ? Number(e.target.value) : undefined)}
                  className="h-8 w-20 rounded border border-input bg-background px-2"
                />
                <input
                  type="number"
                  placeholder="To"
                  value={yearTo ?? ""}
                  onChange={(e) => setYearTo(e.target.value ? Number(e.target.value) : undefined)}
                  className="h-8 w-20 rounded border border-input bg-background px-2"
                />
              </>
            )}
            <select
              value={minCitations ?? ""}
              onChange={(e) => setMinCitations(e.target.value ? Number(e.target.value) : undefined)}
              className="h-8 rounded border border-input bg-background px-2"
              aria-label="Minimum citations"
            >
              <option value="">Any citations</option>
              <option value="10">10+</option>
              <option value="50">50+</option>
              <option value="100">100+</option>
            </select>
            <label className="inline-flex h-8 items-center gap-1.5 rounded border border-input bg-background px-2">
              <input
                type="checkbox"
                checked={oaOnly}
                onChange={(e) => setOaOnly(e.target.checked)}
              />
              Open access only
            </label>
          </div>

          {/* Source chip filter */}
          <div className="flex flex-wrap gap-1.5">
            {(["all", "library", "openalex", "semantic_scholar", "arxiv"] as ChipFilter[]).map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setChipFilter(s)}
                className={
                  "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs transition-colors " +
                  (chipFilter === s
                    ? "border-foreground bg-foreground text-background"
                    : "border-border bg-background hover:bg-muted")
                }
              >
                {s === "all" ? "All" : SOURCE_LABELS[s]}
                <span className="opacity-70">({counts[s]})</span>
              </button>
            ))}
          </div>

          {visible.length === 0 ? (
            <p className="text-sm text-muted-foreground">No matches.</p>
          ) : (
            <div className="grid gap-2">
              {visible.map((r) => {
                const key =
                  r.ids.openalex || r.ids.doi || r.ids.arxiv || r.ids.s2 || r.title;
                const justImported = imported[key];
                return (
                  <ResultRow
                    key={key}
                    r={r}
                    q={submitted}
                    importing={importing === key}
                    importedId={justImported ?? (r.in_library ? r.library_id : null)}
                    onOpen={() => {
                      const id = justImported ?? r.library_id;
                      if (id) nav(`/papers/${encodeURIComponent(id)}`);
                    }}
                    onImport={() => onImport(r)}
                  />
                );
              })}
            </div>
          )}
        </div>
      )}

      {submitted && !loading && tab === "fulltext" && (
        <>
          {semantic.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No full-text matches. (Run <code>/embed</code> on parsed papers to enable this.)
            </p>
          ) : (
            <div className="grid gap-2">
              {semantic.map((r) => (
                <SemanticResultRow
                  key={r.id}
                  r={r}
                  q={submitted}
                  onOpen={() => nav(`/papers/${encodeURIComponent(r.id)}`)}
                />
              ))}
            </div>
          )}
        </>
      )}
    </main>
  );
}

function TabButton({
  active, onClick, icon, label, count,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  count: number;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={
        "flex items-center gap-1.5 border-b-2 px-3 py-1.5 text-sm transition-colors " +
        (active
          ? "border-primary text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground")
      }
    >
      {icon}
      {label}
      {count > 0 && <span className="text-xs text-muted-foreground">({count})</span>}
    </button>
  );
}

function SourceBadges({ sources }: { sources: SearchSource[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {sources.map((s) => (
        <span
          key={s}
          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${SOURCE_BADGE_STYLES[s]}`}
        >
          {SOURCE_LABELS[s]}
        </span>
      ))}
    </div>
  );
}

function ResultRow({
  r, q, importing, importedId, onOpen, onImport,
}: {
  r: SearchResultItem;
  q: string;
  importing: boolean;
  importedId: string | null;
  onOpen: () => void;
  onImport: () => void;
}) {
  const inLibrary = importedId !== null;
  const doiBare = r.ids.doi ? r.ids.doi.replace(/^https?:\/\/(dx\.)?doi\.org\//i, "") : null;
  return (
    <Card>
      <CardContent className="space-y-2 p-4">
        <div className="flex items-start gap-2">
          <button
            onClick={inLibrary ? onOpen : undefined}
            className={
              "block flex-1 text-left font-medium " +
              (inLibrary ? "hover:underline" : "cursor-default")
            }
          >
            <Highlight text={r.title} q={q} />
          </button>
          <SourceBadges sources={r.sources} />
          {inLibrary ? (
            <Button size="sm" variant="outline" onClick={onOpen}>Open</Button>
          ) : (
            <Button size="sm" onClick={onImport} disabled={importing}>
              <FileDown className="mr-1 h-3.5 w-3.5" />
              {importing ? "Importing…" : "Import"}
            </Button>
          )}
        </div>
        <Meta
          authors={r.authors}
          venue={r.venue}
          year={r.publication_date}
          cite={r.citation_count}
        />
        {r.tldr && (
          <p className="rounded bg-muted/50 px-2 py-1 text-xs italic text-muted-foreground">
            <Highlight text={r.tldr} q={q} />
          </p>
        )}
        {r.snippet && (
          <p className="text-sm leading-snug text-muted-foreground">
            <Highlight text={r.snippet} q={q} />
          </p>
        )}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          {doiBare && <span>DOI: {doiBare}</span>}
          {r.ids.arxiv && <span>arXiv:{r.ids.arxiv}</span>}
          {r.ids.openalex && (
            <a
              href={`https://openalex.org/works/${r.ids.openalex}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 hover:underline"
            >
              OpenAlex <ExternalLink className="h-3 w-3" />
            </a>
          )}
          {r.ids.s2 && (
            <a
              href={`https://www.semanticscholar.org/paper/${r.ids.s2}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 hover:underline"
            >
              S2 <ExternalLink className="h-3 w-3" />
            </a>
          )}
          {r.pdf_url && (
            <a
              href={r.pdf_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 hover:underline"
            >
              PDF <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function SemanticResultRow({
  r, q, onOpen,
}: {
  r: SemanticSearchResult;
  q: string;
  onOpen: () => void;
}) {
  const pct = (r.best_score * 100).toFixed(1);
  return (
    <Card>
      <CardContent className="space-y-2 p-4">
        <div className="flex items-start gap-2">
          <button onClick={onOpen} className="block flex-1 text-left font-medium hover:underline">
            <Highlight text={r.title} q={q} />
          </button>
          <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
            {pct}%
          </span>
        </div>
        <Meta authors={r.authors} venue={r.venue} year={r.publication_date} cite={null} />
        <div className="space-y-1.5">
          {r.hits.map((h) => (
            <div
              key={h.chunk_index}
              className="rounded border border-border/50 bg-muted/30 p-2 text-sm"
            >
              {h.heading && (
                <div className="mb-0.5 text-xs font-medium text-muted-foreground">
                  §{h.chunk_index} {h.heading}
                </div>
              )}
              <p className="leading-snug">
                <Highlight text={h.snippet} q={q} />
              </p>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function Meta({ authors, venue, year, cite }: { authors: string[]; venue: string | null; year: string | null; cite: number | null }) {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
      {authors.length > 0 && (
        <span>{authors.slice(0, 4).join(", ")}{authors.length > 4 ? " et al." : ""}</span>
      )}
      {venue && <span>📰 {venue}</span>}
      {year && <span>📅 {year}</span>}
      {cite !== null && cite !== undefined && <span>🏆 {cite.toLocaleString()} cited</span>}
    </div>
  );
}
