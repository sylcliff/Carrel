import { useEffect, useState, type Dispatch, type SetStateAction } from "react";
import { useNavigate } from "react-router-dom";
import {
  Search as SearchIcon,
  ExternalLink,
  FileDown,
  Library as LibraryIcon,
  Globe,
  History as HistoryIcon,
  Star,
  X,
  FileText,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  importPaper,
  searchPapers,
  searchSemantic,
  type ExternalSearchResult,
  type LocalSearchResult,
  type SemanticSearchResult,
} from "@/api/client";

const HISTORY_KEY = "carrel.search.history";
const FAVORITES_KEY = "carrel.search.favorites";
const MAX_SAVED = 20;

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

type Tab = "metadata" | "fulltext" | "openalex";

export default function Search() {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [tab, setTab] = useState<Tab>("metadata");
  const [local, setLocal] = useState<LocalSearchResult[]>([]);
  const [external, setExternal] = useState<ExternalSearchResult[]>([]);
  const [semantic, setSemantic] = useState<SemanticSearchResult[]>([]);
  const [correctedFrom, setCorrectedFrom] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [importing, setImporting] = useState<string | null>(null);
  const [imported, setImported] = useState<Record<string, string>>({});
  const [history, setHistory] = useLocalList(HISTORY_KEY);
  const [favorites, setFavorites] = useLocalList(FAVORITES_KEY);

  const trimmedQ = q.trim();
  const isFav = favorites.some((f) => f.toLowerCase() === trimmedQ.toLowerCase());

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
    setHistory((prev) => {
      const dedup = prev.filter((x) => x.toLowerCase() !== query.toLowerCase());
      return [query, ...dedup].slice(0, MAX_SAVED);
    });
    // Fire both in parallel; each updates its own slice.
    const tasks: [Promise<void>, Promise<void>] = [
      searchPapers(query, 20, correct)
        .then((r) => {
          setLocal(r.local);
          setExternal(r.external);
          // submitted = the query we actually searched (post-correction), so
          // highlight and the "showing results for" line both reflect reality.
          setSubmitted(r.query);
          if (correct) setCorrectedFrom(r.corrected_from);
        })
        .catch((e) => setErr((prev) => prev ?? `Metadata search failed: ${e}`)),
      searchSemantic(query, 10, correct)
        .then((r) => setSemantic(r.results))
        .catch((e) => {
          // Semantic search can fail when no chunks are embedded yet — that's
          // not fatal; show empty results, don't bury the metadata error.
          console.warn("semantic search failed", e);
          setSemantic([]);
        }),
    ];
    await Promise.all(tasks);
    setLoading(false);
  }

  useEffect(() => {
    if (!q.trim()) return;
    const t = setTimeout(() => {
      if (q.trim() !== submitted) runSearch(q.trim(), true);
    }, 350);
    return () => clearTimeout(t);
  }, [q]);

  async function onImport(r: ExternalSearchResult) {
    setImporting(r.openalex_id);
    setErr(null);
    try {
      const out = await importPaper({
        openalex_id: r.openalex_id,
        doi: r.doi ?? undefined,
        arxiv_id: r.arxiv_id ?? undefined,
      });
      setImported((prev) => ({ ...prev, [r.openalex_id]: out.id }));
    } catch (e) {
      setErr(`Import failed: ${e}`);
    } finally {
      setImporting(null);
    }
  }

  // History excludes anything already pinned to favorites (avoids duplicate chips).
  const historyOnly = history.filter(
    (h) => !favorites.some((f) => f.toLowerCase() === h.toLowerCase()),
  );

  return (
    <main className="container max-w-screen-2xl space-y-6 py-8">
      <header className="space-y-2">
        <h1 className="flex items-center gap-2 text-2xl font-bold">
          <SearchIcon className="h-5 w-5" />
          Search
        </h1>
        <p className="text-sm text-muted-foreground">
          Search your local library and OpenAlex at the same time. Click a
          result to open it, or import an external paper into your library.
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
            search original "{correctedFrom}"
          </button>
        </div>
      )}

      {submitted && !loading && (
        <div className="flex gap-1 border-b" role="tablist" aria-label="Search source">
          <TabButton
            active={tab === "metadata"}
            onClick={() => setTab("metadata")}
            icon={<LibraryIcon className="h-3.5 w-3.5" />}
            label="In library"
            count={local.length}
          />
          <TabButton
            active={tab === "fulltext"}
            onClick={() => setTab("fulltext")}
            icon={<FileText className="h-3.5 w-3.5" />}
            label="Full-text"
            count={semantic.length}
          />
          <TabButton
            active={tab === "openalex"}
            onClick={() => setTab("openalex")}
            icon={<Globe className="h-3.5 w-3.5" />}
            label="OpenAlex"
            count={external.length}
          />
        </div>
      )}

      {submitted && !loading && tab === "metadata" && local.length === 0 && (
        <p className="text-sm text-muted-foreground">No matches in your library metadata.</p>
      )}
      {submitted && !loading && tab === "fulltext" && semantic.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No full-text matches. (Run <code>/embed</code> on parsed papers to enable this.)
        </p>
      )}
      {submitted && !loading && tab === "openalex" && external.length === 0 && (
        <p className="text-sm text-muted-foreground">No OpenAlex results.</p>
      )}

      {submitted && !loading && tab === "metadata" && local.length > 0 && (
        <div className="grid gap-2">
          {local.map((r) => (
            <LocalResultRow
              key={r.id}
              r={r}
              q={submitted}
              onOpen={() => nav(`/papers/${encodeURIComponent(r.id)}`)}
            />
          ))}
        </div>
      )}

      {submitted && !loading && tab === "fulltext" && semantic.length > 0 && (
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

      {submitted && !loading && tab === "openalex" && external.length > 0 && (
        <div className="grid gap-2">
          {external.map((r) => {
            const justImported = imported[r.openalex_id];
            return (
              <ExternalResultRow
                key={r.openalex_id}
                r={r}
                q={submitted}
                importing={importing === r.openalex_id}
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

function LocalResultRow({ r, q, onOpen }: { r: LocalSearchResult; q: string; onOpen: () => void }) {
  return (
    <Card>
      <CardContent className="space-y-1 p-4">
        <button onClick={onOpen} className="block w-full text-left font-medium hover:underline">
          <Highlight text={r.title} q={q} />
        </button>
        <Meta authors={r.authors} venue={r.venue} year={r.publication_date} cite={r.citation_count} />
        {r.snippet && (
          <p className="text-sm leading-snug text-muted-foreground">
            <Highlight text={r.snippet} q={q} />
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function ExternalResultRow({
  r,
  q,
  importing,
  importedId,
  onOpen,
  onImport,
}: {
  r: ExternalSearchResult;
  q: string;
  importing: boolean;
  importedId: string | null;
  onOpen: () => void;
  onImport: () => void;
}) {
  const inLibrary = importedId !== null;
  return (
    <Card>
      <CardContent className="space-y-1 p-4">
        <div className="flex items-start gap-2">
          {inLibrary ? (
            <button onClick={onOpen} className="block flex-1 text-left font-medium hover:underline">
              <Highlight text={r.title} q={q} />
            </button>
          ) : (
            <a href={r.cited_by_url ?? "#"} target="_blank" rel="noopener noreferrer" className="block flex-1 font-medium hover:underline">
              <Highlight text={r.title} q={q} />
            </a>
          )}
          {inLibrary ? (
            <Button size="sm" variant="outline" onClick={onOpen}>Open</Button>
          ) : (
            <Button size="sm" onClick={onImport} disabled={importing}>
              <FileDown className="mr-1 h-3.5 w-3.5" />
              {importing ? "Importing…" : "Import"}
            </Button>
          )}
        </div>
        <Meta authors={r.authors} venue={r.venue} year={r.publication_date} cite={r.citation_count} />
        {r.snippet && (
          <p className="text-sm leading-snug text-muted-foreground">
            <Highlight text={r.snippet} q={q} />
          </p>
        )}
        {r.doi && (
          <p className="text-xs text-muted-foreground">
            DOI: {r.doi.replace(/^https?:\/\/(dx\.)?doi\.org\//i, "")}
            {r.arxiv_id && <> · arXiv:{r.arxiv_id}</>}
            {r.cited_by_url && (
              <>
                {" · "}
                <a href={r.cited_by_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 hover:underline">
                  OpenAlex <ExternalLink className="h-3 w-3" />
                </a>
              </>
            )}
          </p>
        )}
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
