import { useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
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
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  getJob,
  importPaper,
  importPapers,
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

  // Bulk-import state. ``selected`` is the set of row keys the user has
  // ticked (only not-in-library rows are eligible). ``bulkJob`` is the
  // server-side Job driving a background batch import; we poll ``getJob``
  // every 1.5s while it's running. ``bulkDoneSummary`` is a short
  // "Imported N" line that surfaces for a few seconds after a successful
  // inline import or background job, then auto-dismisses.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkJob, setBulkJob] = useState<{
    id: number;
    total: number;
    mode: "inline" | "background";
  } | null>(null);
  const [bulkProgress, setBulkProgress] = useState<{
    succeeded: number;
    failed: number;
    current: string | null;
  } | null>(null);
  const [bulkDoneSummary, setBulkDoneSummary] = useState<string | null>(null);
  // Sequence counter so a newer batch (or a new search) can supersede an
  // in-flight poll without showing stale results.
  const bulkSeqRef = useRef(0);
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
        // Server caps at 1000; see SearchFilters in carrel/api/search.py.
        // A 1000-row list is the user's explicit "see everything for one
        // query" target so they can bulk-import the whole result page.
        limit: 1000,
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

  // ---- Bulk import ----

  // Each result row gets a stable key derived from the strongest identifier
  // available; the same key the single-row Import button uses so the
  // `imported` map and the `selected` set share one namespace.
  function rowKey(r: SearchResultItem): string {
    return (
      r.ids.openalex ||
      r.ids.doi ||
      r.ids.arxiv ||
      r.ids.s2 ||
      r.title
    );
  }

  function toggleSelected(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  // Toggle every not-in-library row in the currently filtered set.
  function toggleSelectAllEligible() {
    const allKeys = visible
      .filter((r) => !r.in_library)
      .map(rowKey);
    const allSelected = allKeys.length > 0 && allKeys.every((k) => selected.has(k));
    setSelected((prev) => {
      if (allSelected) {
        const next = new Set(prev);
        for (const k of allKeys) next.delete(k);
        return next;
      }
      const next = new Set(prev);
      for (const k of allKeys) next.add(k);
      return next;
    });
  }

  function clearSelection() {
    setSelected(new Set());
  }

  // Pull the SearchResultItem for each selected key. Selection is the
  // checkbox state; the items we POST to /import/bulk come from the live
  // ``results`` list so the server gets the same identifiers the user sees.
  function gatherSelectedItems(): SearchResultItem[] {
    const out: SearchResultItem[] = [];
    for (const r of visible) {
      if (selected.has(rowKey(r))) out.push(r);
    }
    return out;
  }

  async function runBulkImport() {
    if (bulkJob) return; // a batch is already running
    const items = gatherSelectedItems();
    if (items.length === 0) return;
    setErr(null);
    setBulkDoneSummary(null);

    const payload = items.map((r) => ({
      openalex_id: r.ids.openalex ?? undefined,
      doi: r.ids.doi ?? undefined,
      arxiv_id: r.ids.arxiv ?? undefined,
      s2: r.ids.s2 ?? undefined,
      title: r.title,
    }));

    // ≤20 items: inline (per-item results back immediately, no polling).
    // >20 items: background (returns just a job_id; we poll /sync/jobs/{id}).
    const inline = items.length <= 20;
    if (inline) {
      bulkSeqRef.current += 1;
      const seq = bulkSeqRef.current;
      setBulkJob({ id: -seq, total: items.length, mode: "inline" });
      try {
        const out = await importPapers({ items: payload, background: false });
        if (seq !== bulkSeqRef.current) return; // a newer batch superseded us
        const next: Record<string, string> = {};
        let ok = 0;
        let failed = 0;
        if (out.items) {
          for (let i = 0; i < out.items.length; i++) {
            const it = out.items[i];
            const row = items[i];
            const key = rowKey(row);
            if (it.status === "ok" && it.id) {
              next[key] = it.id;
              ok += 1;
            } else {
              failed += 1;
            }
          }
        }
        setImported((prev) => ({ ...prev, ...next }));
        clearSelection();
        setBulkJob(null);
        setBulkProgress(null);
        setBulkDoneSummary(
          failed > 0
            ? `Imported ${ok} of ${items.length} (${failed} failed — see console)`
            : `Imported ${ok} ${ok === 1 ? "paper" : "papers"}`,
        );
        if (failed > 0) {
          console.warn(
            "Bulk import per-item errors:",
            out.items?.filter((it) => it.status === "error"),
          );
        }
      } catch (e) {
        if (seq !== bulkSeqRef.current) return;
        setErr(`Bulk import failed: ${e}`);
        setBulkJob(null);
        setBulkProgress(null);
      }
      return;
    }

    // Background path.
    try {
      const out = await importPapers({ items: payload, background: true });
      setBulkJob({ id: out.job_id, total: items.length, mode: "background" });
      setBulkProgress({ succeeded: 0, failed: 0, current: items[0]?.title ?? null });
    } catch (e) {
      setErr(`Bulk import failed: ${e}`);
      setBulkJob(null);
    }
  }

  // Poll the background job every 1.5s; update progress + finalize.
  useEffect(() => {
    if (!bulkJob || bulkJob.mode !== "background") return;
    const jobId = bulkJob.id;
    const total = bulkJob.total;
    const seq = ++bulkSeqRef.current;
    const timer = window.setInterval(async () => {
      try {
        const next = await getJob(jobId);
        if (seq !== bulkSeqRef.current) return;
        const stats = (next.stats ?? {}) as {
          succeeded?: number;
          failed?: number;
          current?: string | null;
        };
        setBulkProgress({
          succeeded: stats.succeeded ?? 0,
          failed: stats.failed ?? 0,
          current: stats.current ?? null,
        });
        if (next.status === "done" || next.status === "failed") {
          window.clearInterval(timer);
          // Background mode doesn't return per-item ids, so re-run the
          // current search to refresh in_library flags. The active query
          // is captured in ``activeQuery`` state (may be null if the user
          // hasn't searched yet, but the bulk button is only enabled after
          // a search so this is safe).
          if (next.status === "done" && activeQuery) {
            void runSearch(activeQuery.text, activeQuery.correct);
          }
          clearSelection();
          setBulkJob(null);
          const ok = stats.succeeded ?? 0;
          const failed = stats.failed ?? 0;
          setBulkDoneSummary(
            failed > 0
              ? `Imported ${ok} of ${total} (${failed} failed)`
              : `Imported ${ok} ${ok === 1 ? "paper" : "papers"}`,
          );
          setBulkProgress(null);
        }
      } catch (e) {
        window.clearInterval(timer);
        if (seq !== bulkSeqRef.current) return;
        setErr(`Job poll failed: ${e}`);
        setBulkJob(null);
        setBulkProgress(null);
      }
    }, 1500);
    return () => window.clearInterval(timer);
    // runSearch and activeQuery change reference on every render; the
    // interval should only restart when the active job changes, not on
    // every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bulkJob]);

  // Auto-dismiss the post-import toast so the user doesn't have to
  // manually close it.
  useEffect(() => {
    if (!bulkDoneSummary) return;
    const t = window.setTimeout(() => setBulkDoneSummary(null), 5000);
    return () => window.clearTimeout(t);
  }, [bulkDoneSummary]);

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

          {/* Source chip filter + bulk-select-all toggle */}
          <div className="flex flex-wrap items-center gap-1.5">
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
            {(() => {
              const eligibleKeys = visible
                .filter((r) => !r.in_library)
                .map(rowKey);
              if (eligibleKeys.length === 0) return null;
              const allSelected = eligibleKeys.every((k) => selected.has(k));
              return (
                <button
                  type="button"
                  onClick={toggleSelectAllEligible}
                  className="ml-auto inline-flex items-center gap-1 rounded border border-border bg-background px-2 py-0.5 text-xs hover:bg-muted"
                  title={
                    allSelected
                      ? "Deselect all visible (eligible)"
                      : "Select all visible (eligible)"
                  }
                >
                  {allSelected ? "Deselect all" : "Select all"}
                  <span className="text-muted-foreground">({eligibleKeys.length})</span>
                </button>
              );
            })()}
          </div>

          {visible.length === 0 ? (
            <p className="text-sm text-muted-foreground">No matches.</p>
          ) : (
            <div className="grid gap-2">
              {visible.map((r) => {
                const key = rowKey(r);
                const justImported = imported[key];
                return (
                  <ResultRow
                    key={key}
                    r={r}
                    q={submitted}
                    importing={importing === key}
                    importedId={justImported ?? (r.in_library ? r.library_id : null)}
                    selectable={!r.in_library}
                    selected={selected.has(key)}
                    onToggleSelect={() => toggleSelected(key)}
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

          {/* Bulk import action bar — sticky to the bottom of the viewport
              so it stays visible while the user scrolls a long result list.
              Three modes: idle (N selected), running (progress), done
              (transient toast). */}
          <BulkActionBar
            selectedCount={selected.size}
            totalEligible={visible.filter((r) => !r.in_library).length}
            running={
              bulkJob
                ? {
                    processed:
                      (bulkProgress?.succeeded ?? 0) +
                      (bulkProgress?.failed ?? 0),
                    total: bulkJob.total,
                    current: bulkProgress?.current ?? null,
                    succeeded: bulkProgress?.succeeded ?? 0,
                    failed: bulkProgress?.failed ?? 0,
                  }
                : null
            }
            doneSummary={bulkDoneSummary}
            onImport={runBulkImport}
            onClear={clearSelection}
            onDismissDone={() => setBulkDoneSummary(null)}
          />
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
  selectable, selected, onToggleSelect,
}: {
  r: SearchResultItem;
  q: string;
  importing: boolean;
  importedId: string | null;
  onOpen: () => void;
  onImport: () => void;
  selectable: boolean;
  selected: boolean;
  onToggleSelect: () => void;
}) {
  const inLibrary = importedId !== null;
  const doiBare = r.ids.doi ? r.ids.doi.replace(/^https?:\/\/(dx\.)?doi\.org\//i, "") : null;
  return (
    <Card>
      <CardContent className="space-y-2 p-4">
        <div className="flex items-start gap-2">
          {selectable && (
            <input
              type="checkbox"
              checked={selected}
              onChange={onToggleSelect}
              aria-label={`Select ${r.title} for import`}
              className="mt-1 h-4 w-4 shrink-0 cursor-pointer accent-primary"
            />
          )}
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

// Sticky bottom-of-viewport bar that surfaces the "Import N selected"
// action, runs a single progress display while a bulk job is in flight,
// and shows a transient "Imported N" toast for ~5s after completion.
// Hidden entirely when there's nothing to show (no selection, no job).
function BulkActionBar({
  selectedCount,
  totalEligible,
  running,
  doneSummary,
  onImport,
  onClear,
  onDismissDone,
}: {
  selectedCount: number;
  totalEligible: number;
  running: {
    processed: number;
    total: number;
    current: string | null;
    succeeded: number;
    failed: number;
  } | null;
  doneSummary: string | null;
  onImport: () => void;
  onClear: () => void;
  onDismissDone: () => void;
}) {
  // The bar is only relevant when there's a selection, a job in flight,
  // or a transient success/error to show.
  const visible = selectedCount > 0 || running !== null || doneSummary !== null;
  if (!visible) return null;

  const pct = running
    ? Math.min(100, Math.round((running.processed / Math.max(1, running.total)) * 100))
    : 0;
  // Indeterminate vs determinate: when processed < total we show a track
  // with the filled portion; when finished (== total) the track goes full
  // and the toast shows underneath. We keep the bar mounted while doneSummary
  // is showing so the user can see the final state before it auto-dismisses.
  return (
    <div className="sticky bottom-0 z-10 mt-2 rounded-md border border-border/60 bg-background/95 px-3 py-2 shadow-sm backdrop-blur supports-[backdrop-filter]:bg-background/75">
      {running ? (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-3 text-sm">
            <div className="flex items-center gap-2 truncate">
              <Loader2 className="h-4 w-4 animate-spin shrink-0" />
              <span className="truncate">
                Importing {running.processed} / {running.total}
                {running.current && (
                  <span className="ml-1 text-muted-foreground">
                    — {running.current}
                  </span>
                )}
              </span>
            </div>
            <span className="shrink-0 text-xs text-muted-foreground">
              {running.succeeded} ok · {running.failed} failed
            </span>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded bg-muted">
            <div
              className="h-full bg-primary transition-[width] duration-300"
              style={{ width: `${pct}%` }}
            />
          </div>
        </div>
      ) : doneSummary ? (
        <div className="flex items-center justify-between gap-3 text-sm">
          <span>{doneSummary}</span>
          <Button size="sm" variant="ghost" onClick={onDismissDone}>
            Dismiss
          </Button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <div className="flex items-center gap-2">
            <span className="font-medium">{selectedCount} selected</span>
            {totalEligible > selectedCount && (
              <span className="text-xs text-muted-foreground">
                ({totalEligible - selectedCount} more available)
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="ghost" onClick={onClear}>
              Clear
            </Button>
            <Button size="sm" onClick={onImport} disabled={selectedCount === 0}>
              <FileDown className="mr-1 h-3.5 w-3.5" />
              Import {selectedCount}
            </Button>
          </div>
        </div>
      )}
    </div>
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
