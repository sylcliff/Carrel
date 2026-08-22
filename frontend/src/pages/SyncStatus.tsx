import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { listJobs, triggerSync, type Job } from "@/api/client";
import ScheduledJobsCard from "@/components/ScheduledJobsCard";

const ACTIVE = new Set(["queued", "running"]);
const POLL_ACTIVE_MS = 2000;
const POLL_IDLE_MS = 10000;

const KIND_LABEL: Record<string, string> = {
  sync: "Sync",
  download: "Download",
  parse: "Parse",
  summarize: "Summarize",
  embed: "Embed",
  citations: "Citations",
  topics: "Topics",
  authors_backfill: "Author backfill",
  remote_fill: "Remote fill",
  publication_check: "Publication check",
};
const KIND_KEYS = Object.keys(KIND_LABEL) as Array<keyof typeof KIND_LABEL>;
type KindFilter = "all" | (typeof KIND_KEYS)[number];

function jobColor(status: string): string {
  if (status === "done") return "bg-green-500";
  if (status === "failed") return "bg-red-500";
  return "bg-yellow-400";
}

function fmt(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString();
}

function duration(a: string | null, b: string | null): string {
  if (!a || !b) return "";
  const ms = new Date(b).getTime() - new Date(a).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m${s % 60}s` : `${Math.floor(m / 60)}h${m % 60}m`;
}

export default function SyncStatus() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [kind, setKind] = useState<KindFilter>("all");
  const [statusFilter, setStatusFilter] = useState<"all" | "failed" | "active">("all");
  const [syncing, setSyncing] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const activeRef = useRef(false);
  const [, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const rows = await listJobs({ limit: 200 });
        if (!cancelled) {
          setJobs(rows);
          activeRef.current = rows.some((j) => ACTIVE.has(j.status));
        }
      } catch {
        /* keep polling */
      } finally {
        if (!cancelled) {
          timer = setTimeout(poll, activeRef.current ? POLL_ACTIVE_MS : POLL_IDLE_MS);
        }
      }
    }
    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);

  // 1s ticker so durations on active jobs move.
  useEffect(() => {
    const hasActive = jobs.some((j) => ACTIVE.has(j.status));
    if (!hasActive) return;
    const t = setInterval(() => setTick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, [jobs]);

  async function onSync() {
    setSyncing(true);
    setErr(null);
    try {
      await triggerSync(72, true);
    } catch (e) {
      setErr(String(e));
    } finally {
      setSyncing(false);
    }
  }

  const failed = useMemo(
    () => jobs.filter((j) => j.status === "failed"),
    [jobs],
  );
  const filtered = useMemo(() => {
    return jobs.filter((j) => {
      if (kind !== "all" && j.kind !== kind) return false;
      if (statusFilter === "failed" && j.status !== "failed") return false;
      if (statusFilter === "active" && !ACTIVE.has(j.status)) return false;
      return true;
    });
  }, [jobs, kind, statusFilter]);

  const counts = useMemo(() => {
    const c = { all: jobs.length, failed: failed.length, active: 0 };
    for (const j of jobs) if (ACTIVE.has(j.status)) c.active++;
    return c;
  }, [jobs, failed]);

  return (
    <main className="container max-w-screen-2xl py-8">
      <section className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Sync status</h1>
          <p className="text-sm text-muted-foreground">
            Background jobs: metadata sync, PDF downloads, MinerU parse, embeddings, citation refresh.
          </p>
        </div>
        <Button onClick={onSync} disabled={syncing}>
          {syncing ? "Queuing…" : "Sync now (72h)"}
        </Button>
      </section>

      {err && (
        <Card className="mt-6 border-red-300">
          <CardContent className="pt-5 text-sm text-red-600">{err}</CardContent>
        </Card>
      )}

      {failed.length > 0 && statusFilter !== "failed" && (
        <Card className="mt-6 border-red-300">
          <CardContent className="pt-5">
            <div className="flex items-center justify-between">
              <p className="text-sm font-medium text-red-700">
                {failed.length} failed job{failed.length === 1 ? "" : "s"}
              </p>
              <button
                type="button"
                onClick={() => setStatusFilter("failed")}
                className="text-xs underline hover:text-foreground"
              >
                Show only failed
              </button>
            </div>
            <ul className="mt-2 space-y-1 text-xs text-red-700">
              {failed.slice(0, 5).map((j) => (
                <li key={j.id} className="truncate">
                  <span className="font-mono">#{j.id}</span>{" "}
                  {KIND_LABEL[j.kind] ?? j.kind} — {j.message}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      <div className="mt-6">
        <ScheduledJobsCard />
      </div>

      <h2 className="mt-8 text-lg font-semibold">Recent jobs</h2>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
        <div className="flex rounded-md border overflow-hidden">
          {(["all", "active", "failed"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStatusFilter(s)}
              className={`px-3 py-1.5 capitalize ${
                statusFilter === s ? "bg-foreground text-background" : "hover:bg-muted"
              }`}
            >
              {s} {s === "all" ? `(${counts.all})` : `(${counts[s]})`}
            </button>
          ))}
        </div>
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as KindFilter)}
          className="rounded-md border bg-transparent px-2 py-1.5"
        >
          <option value="all">All kinds</option>
          {Object.entries(KIND_LABEL).map(([k, label]) => (
            <option key={k} value={k}>{label}</option>
          ))}
        </select>
      </div>

      <Card className="mt-4">
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <thead className="border-b text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-2 text-left">ID</th>
                <th className="px-4 py-2 text-left">Kind</th>
                <th className="px-4 py-2 text-left">Status</th>
                <th className="px-4 py-2 text-left">Detail</th>
                <th className="px-4 py-2 text-left">Started</th>
                <th className="px-4 py-2 text-left">Duration</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">
                    No jobs match the current filter.
                  </td>
                </tr>
              )}
              {filtered.map((j) => (
                <JobRow key={j.id} job={j} />
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </main>
  );
}

function JobRow({ job }: { job: Job }) {
  const active = ACTIVE.has(job.status);
  const stats = (job.stats ?? {}) as Record<string, unknown>;
  const paperId = typeof stats.paper_id === "string" ? stats.paper_id : null;
  const paperTitle =
    (typeof stats.paper_title === "string" && stats.paper_title) ||
    (typeof stats.title === "string" && stats.title) ||
    null;

  const detail = active
    ? (typeof stats.stage === "string" ? stats.stage : "Working…")
      + (typeof stats.detail === "string" ? ` — ${stats.detail}` : "")
    : job.status === "done"
      ? (typeof stats.detail === "string" ? stats.detail : summary(stats))
      : job.message || "Failed";

  const end = active ? new Date().toISOString() : job.finished_at;

  return (
    <tr className="border-b last:border-0 align-top hover:bg-muted/40">
      <td className="px-4 py-2 font-mono text-xs text-muted-foreground">{job.id}</td>
      <td className="px-4 py-2">{KIND_LABEL[job.kind] ?? job.kind}</td>
      <td className="px-4 py-2">
        <span className="inline-flex items-center gap-2">
          {active ? (
            <span className="inline-block h-2.5 w-2.5 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-foreground" />
          ) : (
            <span className={`inline-block h-2.5 w-2.5 rounded-full ${jobColor(job.status)}`} />
          )}          <span className="capitalize">{job.status}</span>
        </span>
      </td>
      <td className="px-4 py-2 min-w-[18rem] max-w-[32rem]">
        {paperId ? (
          <Link to={`/papers/${encodeURIComponent(paperId)}`} className="hover:underline">
            {paperTitle ?? paperId}
          </Link>
        ) : (
          <span className="text-muted-foreground">{detail}</span>
        )}
        {job.status === "failed" && job.message && (
          <div className="mt-0.5 text-xs text-red-600 break-words" title={job.message}>
            {job.message}
          </div>
        )}
      </td>
      <td className="px-4 py-2 text-xs text-muted-foreground whitespace-nowrap">
        {fmt(job.started_at ?? job.created_at)}
      </td>
      <td className="px-4 py-2 text-xs font-mono text-muted-foreground whitespace-nowrap">
        {duration(job.started_at, end)}
      </td>
    </tr>
  );
}

function summary(stats: Record<string, unknown>): string {
  if (typeof stats.fetched === "number") {
    return `fetched ${stats.fetched}, new ${stats.new ?? 0}, updated ${stats.updated ?? 0}`;
  }
  return "";
}
