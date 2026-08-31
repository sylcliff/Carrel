import { useCallback, useEffect, useRef, useState } from "react";

import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import CitationRowActions from "@/components/CitationRowActions";
import {
  getJob,
  getPaperCitations,
  refreshPaperCitations,
  type CitationItem,
  type Job,
  type PaperDetail,
} from "@/api/client";
import { citeUrl } from "@/lib/citations";

const TERMINAL = new Set(["done", "failed"]);
const PAGE_SIZE_OPTIONS = [20, 50, 100] as const;

type SortKey = "" | "year_asc" | "year_desc" | "cited_desc";

type Props = {
  paper: PaperDetail;
  onChanged?: () => void;
};

export default function CitationsCard({ paper, onChanged }: Props) {
  // Open by default when there's data — otherwise the list is invisible until
  // the user happens to click the title, and "看不到引用" was the #1 complaint.
  const [open, setOpen] = useState(
    paper.citation_count !== null && paper.citation_count !== undefined && paper.citation_count > 0,
  );
  const [sort, setSort] = useState<SortKey>("");
  const [items, setItems] = useState<CitationItem[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZE_OPTIONS)[number]>(50);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pageSource, setPageSource] = useState<"cache" | "openalex" | "">("");
  const pollTimer = useRef<number | null>(null);

  const count = paper.citation_count;
  const influential = paper.influential_citation_count;
  const updated = paper.citations_updated_at;
  const totalPages = count && count > 0 ? Math.ceil(count / pageSize) : null;

  const fetchPage = useCallback(
    async (offset: number, sortKey: SortKey, size: number) => {
      const opts: { sort?: "year_asc" | "year_desc" | "cited_desc"; offset: number; limit: number } = {
        offset,
        limit: size,
      };
      if (sortKey) opts.sort = sortKey;
      return getPaperCitations(paper.id, opts);
    },
    [paper.id],
  );

  // Load a specific page (1-indexed). Replaces items entirely so the UI is
  // a true pager rather than an appending scroll list.
  const loadPage = useCallback(
    async (p: number, sortKey: SortKey, size: number) => {
      setLoading(true);
      setErr(null);
      try {
        const r = await fetchPage((p - 1) * size, sortKey, size);
        setItems(r.citing);
        setPage(p);
        setPageSource(r.source);
      } catch (e) {
        setErr(String(e));
      } finally {
        setLoading(false);
      }
    },
    [fetchPage],
  );

  // Reset to page 1 when the card opens, the sort changes, or the page-size
  // changes (different size = different offsets, so old page is meaningless).
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setErr(null);
      try {
        const r = await fetchPage(0, sort, pageSize);
        if (cancelled) return;
        setItems(r.citing);
        setPage(1);
        setPageSource(r.source);
      } catch (e) {
        if (!cancelled) setErr(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, sort, pageSize, fetchPage]);

  useEffect(
    () => () => {
      if (pollTimer.current) window.clearInterval(pollTimer.current);
    },
    [],
  );

  function pollJob(jobId: number) {
    if (pollTimer.current) window.clearInterval(pollTimer.current);
    pollTimer.current = window.setInterval(async () => {
      try {
        const j = await getJob(jobId);
        setJob(j);
        if (TERMINAL.has(j.status)) {
          if (pollTimer.current) window.clearInterval(pollTimer.current);
          setRefreshing(false);
          if (j.status === "failed") setErr(j.message || "Refresh failed");
          else {
            // Reload the current page so the freshly enriched data shows up
            // under whichever sort is active.
            const r = await fetchPage((page - 1) * pageSize, sort, pageSize);
            setItems(r.citing);
            setPageSource(r.source);
            onChanged?.();
          }
        }
      } catch (e) {
        if (pollTimer.current) window.clearInterval(pollTimer.current);
        setRefreshing(false);
        setErr(String(e));
      }
    }, 1500);
  }

  async function onRefresh() {
    setErr(null);
    setRefreshing(true);
    try {
      const started = await refreshPaperCitations(paper.id, true);
      setJob(started);
      pollJob(started.id);
    } catch (e) {
      setRefreshing(false);
      setErr(String(e));
    }
  }

  const detail = (job?.stats?.detail as string | undefined) ?? job?.message ?? "";
  const label =
    count === null || count === undefined
      ? "Cited by —"
      : `Cited by ${count.toLocaleString()}`;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-2 text-left"
        >
          {open ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )}
          <CardTitle className="text-base">
            {label}
            {influential ? (
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                {influential.toLocaleString()} influential
              </span>
            ) : null}
            {updated ? (
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                · updated {new Date(updated).toLocaleDateString()}
              </span>
            ) : null}
          </CardTitle>
        </button>
        <div className="flex items-center gap-2">
          {open && (
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as SortKey)}
              className="h-8 rounded-md border border-input bg-background px-2 text-xs"
              title="Sort citing papers"
            >
              <option value="">Original order</option>
              <option value="year_asc">Year ↑ (oldest first)</option>
              <option value="year_desc">Year ↓ (newest first)</option>
              <option value="cited_desc">Cited-by count ↓</option>
            </select>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={onRefresh}
            disabled={refreshing}
            title="Refresh citations from Semantic Scholar + OpenAlex"
          >
            <RefreshCw className={`mr-1 h-3.5 w-3.5 ${refreshing ? "animate-spin" : ""}`} />
            Refresh
          </Button>
        </div>
      </CardHeader>

      {refreshing && (
        <CardContent className="pb-3 text-sm text-muted-foreground">
          {detail || "Querying Semantic Scholar…"}
        </CardContent>
      )}

      {err && !refreshing && (
        <CardContent className="pb-3 text-sm text-red-600">{err}</CardContent>
      )}

      {open && (
        <CardContent className="space-y-2">
          {loading && <div className="text-sm text-muted-foreground">Loading…</div>}
          {!loading && items.length === 0 && (
            <div className="text-sm text-muted-foreground">
              No citing papers recorded.
            </div>
          )}
          {items.length > 0 && (
            <ol className="list-decimal space-y-2 pl-6 text-justify" start={(page - 1) * pageSize + 1}>
              {items.map((c, i) => {
                const url = citeUrl(c);
                const title = c.title || "(untitled)";
                return (
                  <li
                    key={`${c.s2_paper_id ?? c.openalex_id ?? c.doi ?? i}`}
                    className="flex items-baseline text-sm leading-snug"
                  >
                    <span className="min-w-0 flex-1">
                      {c.in_library && c.paper_id ? (
                        <Link
                          to={`/papers/${encodeURIComponent(c.paper_id)}`}
                          className="font-medium text-foreground underline-offset-2 hover:underline"
                        >
                          {title}
                        </Link>
                      ) : url ? (
                        <a
                          href={url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="font-medium text-foreground underline-offset-2 hover:underline"
                        >
                          {title}
                        </a>
                      ) : (
                        <span className="font-medium">{title}</span>
                      )}
                      <span className="ml-2 shrink-0 text-xs text-muted-foreground">
                        {c.venue ? <span className="truncate">{c.venue}</span> : null}
                        {c.venue && c.year ? <span className="mx-1">·</span> : null}
                        {c.year ?? (c.venue ? null : "—")}
                      </span>
                    </span>
                    <CitationRowActions
                      item={c}
                      onImported={(pid) => {
                        setItems((prev) =>
                          prev.map((it) =>
                            it === c ? { ...it, in_library: true, paper_id: pid } : it,
                          ),
                        );
                        onChanged?.();
                      }}
                    />
                  </li>
                );
              })}
            </ol>
          )}

          {items.length > 0 && (
            <Pager
              page={page}
              totalPages={totalPages}
              count={count}
              pageSize={pageSize}
              loading={loading}
              source={pageSource}
              onPage={(p) => loadPage(p, sort, pageSize)}
              onPageSize={setPageSize}
            />
          )}
        </CardContent>
      )}
    </Card>
  );
}

function Pager({
  page,
  totalPages,
  count,
  pageSize,
  loading,
  source,
  onPage,
  onPageSize,
}: {
  page: number;
  totalPages: number | null;
  count: number | null;
  pageSize: number;
  loading: boolean;
  source: "cache" | "openalex" | "";
  onPage: (p: number) => void;
  onPageSize: (n: 20 | 50 | 100) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [draft, setDraft] = useState(String(page));
  useEffect(() => { setDraft(String(page)); }, [page]);
  const lastShown = Math.min(page * pageSize, count ?? page * pageSize);
  const firstShown = (page - 1) * pageSize + 1;
  const totalShown = count ?? lastShown;

  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-xs text-muted-foreground">
      <span>
        Showing {firstShown.toLocaleString()}–{lastShown.toLocaleString()} of {totalShown.toLocaleString()}
        {source === "openalex" ? " (OpenAlex live)" : source === "cache" ? " (cached)" : ""}
      </span>
      <div className="flex items-center gap-1">
        <select
          value={pageSize}
          onChange={(e) => onPageSize(parseInt(e.target.value, 10) as 20 | 50 | 100)}
          disabled={loading}
          className="h-7 rounded-md border border-input bg-background px-2 text-foreground"
          title="Items per page"
        >
          <option value="20">20 / page</option>
          <option value="50">50 / page</option>
          <option value="100">100 / page</option>
        </select>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPage(page - 1)}
          disabled={loading || page <= 1}
        >
          ← Prev
        </Button>
        <span className="px-1">Page</span>
        <input
          ref={inputRef}
          type="number"
          min={1}
          max={totalPages ?? undefined}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            const n = parseInt(draft, 10);
            if (!Number.isFinite(n) || n < 1) {
              setDraft(String(page));
              return;
            }
            const clamped = totalPages ? Math.min(n, totalPages) : n;
            setDraft(String(clamped));
            if (clamped !== page) onPage(clamped);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
          className="h-7 w-14 rounded-md border border-input bg-background px-2 text-center text-foreground"
        />
        {totalPages ? <span>of {totalPages.toLocaleString()}</span> : null}
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPage(page + 1)}
          disabled={loading || (totalPages !== null && page >= totalPages)}
        >
          Next →
        </Button>
      </div>
    </div>
  );
}
