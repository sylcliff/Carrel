import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  backfillAuthors,
  listJobs,
  listScholars,
  type ScholarSummary,
} from "@/api/client";
import { topicColorClass } from "@/lib/topicColor";

/** Build a /scholars route key from an author record. */
export function scholarKey(openalexAuthorId: string, name: string): string {
  const id = (openalexAuthorId || "").trim();
  if (id) return id;
  return `name:${name.trim()}`;
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export default function Scholars() {
  const [scholars, setScholars] = useState<ScholarSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const rows = await listScholars();
      setScholars(rows);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Poll jobs while an authors-backfill batch is running; refresh the list
  // once the queue drains.
  useEffect(() => {
    if (!running) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const jobs = await listJobs({ kind: "authors_backfill", limit: 50 });
        if (cancelled) return;
        const active = jobs.filter((j) =>
          ["queued", "running"].includes(j.status),
        );
        const latest = jobs[0];
        const detail =
          (latest?.stats as { detail?: string } | null)?.detail ?? null;
        setProgress(
          active.length > 0
            ? `${active.length} in progress${detail ? ` — ${detail}` : ""}`
            : "Finishing…",
        );
        if (active.length === 0) {
          setRunning(false);
          setProgress(null);
          refresh();
        }
      } catch {
        // Transient poll failure; keep polling.
      }
    };
    tick();
    pollRef.current = window.setInterval(tick, 2000);
    return () => {
      cancelled = true;
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [running, refresh]);

  async function resolveAll() {
    setRunning(true);
    setProgress("Starting…");
    try {
      await backfillAuthors({ limit: 300, background: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRunning(false);
      setProgress(null);
    }
  }

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return scholars;
    return scholars.filter((s) => s.name.toLowerCase().includes(needle));
  }, [scholars, q]);

  const nameOnlyCount = useMemo(
    () => scholars.filter((s) => !s.has_openalex).length,
    [scholars],
  );

  return (
    <main className="container max-w-screen-2xl space-y-4 py-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-2xl font-bold">Scholars</h1>
        <div className="flex items-center gap-3">
          <Button
            size="sm"
            variant="outline"
            onClick={resolveAll}
            disabled={running}
            title="Look up missing OpenAlex author IDs from each paper's DOI/arXiv ID"
          >
            {running ? "Resolving…" : "Resolve authors"}
          </Button>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter by name…"
            className="h-9 w-64 rounded-md border border-input bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
      </div>
      <p className="text-sm text-muted-foreground">
        Authors in your library, ranked by how many of their papers you have.
        {!loading && ` ${scholars.length} scholar(s).`}
        {!loading && nameOnlyCount > 0 && (
          <> {nameOnlyCount} matched by name only — run{" "}
          <strong>Resolve authors</strong> to merge them via OpenAlex.</>
        )}
      </p>
      {progress && (
        <p className="text-sm text-muted-foreground">{progress}</p>
      )}

      {error && <p className="text-sm text-red-600">{error}</p>}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading scholars…</p>
      ) : filtered.length === 0 ? (
        <Card>
          <CardContent className="p-6 text-sm text-muted-foreground">
            No scholars found. Import some papers first.
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((s) => (
            <Link
              key={s.key}
              to={`/scholars/${encodeURIComponent(s.key)}`}
              className="block"
            >
              <Card className="h-full transition-colors hover:bg-muted/30">
                <CardContent className="flex gap-3 p-4">
                  <div
                    className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-sm font-semibold ${topicColorClass(s.name)}`}
                  >
                    {initials(s.name)}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start justify-between gap-2">
                      <h2 className="truncate font-medium leading-snug">
                        {s.name}
                      </h2>
                      <span
                        className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-xs font-medium"
                        title="Papers in your library"
                      >
                        {s.paper_count}
                      </span>
                    </div>
                    {s.affiliation && (
                      <p
                        className="truncate text-xs text-muted-foreground"
                        title={s.affiliation}
                      >
                        {s.affiliation}
                      </p>
                    )}
                    <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                      {(s.first_year || s.last_year) && (
                        <span>
                          {s.first_year ?? "?"}–{s.last_year ?? "?"}
                        </span>
                      )}
                      {s.total_citations > 0 && (
                        <span>🏆 {s.total_citations} cites</span>
                      )}
                      {!s.has_openalex && (
                        <span className="italic">name-only</span>
                      )}
                    </div>
                  </div>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </main>
  );
}
