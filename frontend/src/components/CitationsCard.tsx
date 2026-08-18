import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ExternalLink, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  getJob,
  getPaperCitations,
  refreshPaperCitations,
  type CitationList,
  type Job,
  type PaperDetail,
} from "@/api/client";

const TERMINAL = new Set(["done", "failed"]);

type Props = {
  paper: PaperDetail;
  onChanged?: () => void;
};

function citeUrl(c: { doi: string | null; arxiv_id: string | null; s2_paper_id: string | null }) {
  if (c.doi) return `https://doi.org/${c.doi}`;
  if (c.arxiv_id) return `https://arxiv.org/abs/${c.arxiv_id}`;
  if (c.s2_paper_id) return `https://www.semanticscholar.org/paper/${c.s2_paper_id}`;
  return null;
}

export default function CitationsCard({ paper, onChanged }: Props) {
  const [open, setOpen] = useState(false);
  const [list, setList] = useState<CitationList | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  const count = paper.citation_count;
  const influential = paper.influential_citation_count;
  const updated = paper.citations_updated_at;

  const loadCitations = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      setList(await getPaperCitations(paper.id));
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [paper.id]);

  useEffect(() => {
    if (open && list === null && !loading) {
      loadCitations();
    }
  }, [open, list, loading, loadCitations]);

  useEffect(
    () => () => {
      if (timer.current) window.clearInterval(timer.current);
    },
    []
  );

  function pollJob(jobId: number) {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = window.setInterval(async () => {
      try {
        const j = await getJob(jobId);
        setJob(j);
        if (TERMINAL.has(j.status)) {
          if (timer.current) window.clearInterval(timer.current);
          setRefreshing(false);
          if (j.status === "failed") setErr(j.message || "Refresh failed");
          else {
            await loadCitations();
            onChanged?.();
          }
        }
      } catch (e) {
        if (timer.current) window.clearInterval(timer.current);
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
          className="text-left"
        >
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
        <Button
          variant="outline"
          size="sm"
          onClick={onRefresh}
          disabled={refreshing}
          title="Refresh citations from Semantic Scholar"
        >
          <RefreshCw className={`mr-1 h-3.5 w-3.5 ${refreshing ? "animate-spin" : ""}`} />
          Refresh
        </Button>
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
          {list && list.citing.length === 0 && !loading && (
            <div className="text-sm text-muted-foreground">
              No citing papers recorded.
            </div>
          )}
          {list && list.citing.length > 0 && (
            <ul className="space-y-2">
              {list.citing.map((c, i) => {
                const url = citeUrl(c);
                const title = c.title || "(untitled)";
                return (
                  <li key={`${c.s2_paper_id ?? i}`} className="text-sm leading-snug">
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
                      {c.in_library ? " · in your library" : ""}
                    </span>
                    {url && !c.in_library && (
                      <ExternalLink className="ml-1 inline h-3 w-3 text-muted-foreground" />
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          {list?.truncated && (
            <p className="text-xs text-muted-foreground">
              Showing the first {list.citing.length} citing papers; the total is higher.
            </p>
          )}
        </CardContent>
      )}
    </Card>
  );
}
