import { useCallback, useEffect, useState } from "react";

import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import CitationRowActions from "@/components/CitationRowActions";
import {
  getJob,
  getPaperReferences,
  refreshPaperCitations,
  type CitationItem,
  type Job,
  type PaperDetail,
} from "@/api/client";

const TERMINAL = new Set(["done", "failed"]);

type SortKey = "" | "year_asc" | "year_desc";

type Props = {
  paper: PaperDetail;
  onChanged?: () => void;
};

function citeUrl(c: {
  doi: string | null;
  arxiv_id: string | null;
  s2_paper_id: string | null;
  openalex_id: string | null;
}) {
  if (c.doi) return `https://doi.org/${c.doi}`;
  if (c.arxiv_id) return `https://arxiv.org/abs/${c.arxiv_id}`;
  if (c.s2_paper_id) return `https://www.semanticscholar.org/paper/${c.s2_paper_id}`;
  if (c.openalex_id) {
    const bare = c.openalex_id.includes("/") ? c.openalex_id.split("/").pop() : c.openalex_id;
    return `https://openalex.org/works/${bare}`;
  }
  return null;
}

export default function ReferencesCard({ paper, onChanged }: Props) {
  // Open by default when the paper reports references — mirrors CitationsCard.
  const [open, setOpen] = useState(
    paper.reference_count !== null &&
      paper.reference_count !== undefined &&
      paper.reference_count > 0,
  );
  const [sort, setSort] = useState<SortKey>("");
  const [items, setItems] = useState<CitationItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const count = paper.reference_count;
  const updated = paper.citations_updated_at;

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await getPaperReferences(paper.id, sort ? { sort } : undefined);
      setItems(r.references);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [paper.id, sort]);

  useEffect(() => {
    if (!open) return;
    void load();
  }, [open, load]);

  // The refresh job is shared with citations (same S2 lookup writes both lists);
  // once it finishes, reload references and notify the parent so counts update.
  async function onRefresh() {
    setErr(null);
    setRefreshing(true);
    try {
      const started = await refreshPaperCitations(paper.id, true);
      setJob(started);
      poll(started.id);
    } catch (e) {
      setRefreshing(false);
      setErr(String(e));
    }
  }

  function poll(jobId: number) {
    const timer = window.setInterval(async () => {
      try {
        const j = await getJob(jobId);
        setJob(j);
        if (TERMINAL.has(j.status)) {
          window.clearInterval(timer);
          setRefreshing(false);
          if (j.status === "failed") setErr(j.message || "Refresh failed");
          else {
            await load();
            onChanged?.();
          }
        }
      } catch (e) {
        window.clearInterval(timer);
        setRefreshing(false);
        setErr(String(e));
      }
    }, 1500);
  }

  const detail = (job?.stats?.detail as string | undefined) ?? job?.message ?? "";
  const label =
    count === null || count === undefined
      ? "References —"
      : `References ${count.toLocaleString()}`;

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
              title="Sort references"
            >
              <option value="">Original order</option>
              <option value="year_asc">Year ↑ (oldest first)</option>
              <option value="year_desc">Year ↓ (newest first)</option>
            </select>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={onRefresh}
            disabled={refreshing}
            title="Refresh references from Semantic Scholar"
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
              {count && count > 0
                ? `${count.toLocaleString()} references reported by Semantic Scholar — click Refresh to load the list.`
                : "No references recorded. Click Refresh to fetch them from Semantic Scholar."}
            </div>
          )}
          {items.length > 0 && (
            <ol className="list-decimal space-y-2 pl-6">
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
                      <span className="ml-2 text-xs text-muted-foreground">
                        {c.year ?? "—"}
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
        </CardContent>
      )}
    </Card>
  );
}
