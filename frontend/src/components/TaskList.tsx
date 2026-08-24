import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { listJobs, type Job } from "@/api/client";

const ACTIVE = new Set(["queued", "running"]);
const HISTORY_LIMIT = 8;
const ACTIVE_POLL_MS = 2000;
const IDLE_POLL_MS = 15000;

function jobColor(status: string): string {
  if (status === "done") return "bg-green-500";
  if (status === "failed") return "bg-red-500";
  return "bg-yellow-400";
}

function formatTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function elapsed(started: string | null, now: number): string {
  if (!started) return "";
  const t = new Date(started).getTime();
  if (!Number.isFinite(t)) return "";
  const secs = Math.max(0, Math.round((now - t) / 1000));
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return s ? `${m}m${s}s` : `${m}m`;
}

interface Props {
  onProcessed?: () => void;
  refreshNonce?: number;
}

export function TaskList({ onProcessed, refreshNonce = 0 }: Props) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [, setTick] = useState(0);
  const prevStatus = useRef<Map<number, string>>(new Map());
  const activeRef = useRef(false);
  const pollNowRef = useRef<() => void>(() => {});

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    async function poll() {
      try {
        const rows = await listJobs();
        if (cancelled) return;

        if (onProcessed) {
          for (const j of rows) {
            if (
              j.kind !== "download" &&
              j.kind !== "parse" &&
              j.kind !== "summarize" &&
              j.kind !== "embed" &&
              j.kind !== "citations" &&
              j.kind !== "wiki_compile"
            )
              continue;
            const prev = prevStatus.current.get(j.id);
            if (prev && ACTIVE.has(prev) && !ACTIVE.has(j.status)) {
              onProcessed();
            }
          }
        }
        for (const j of rows) prevStatus.current.set(j.id, j.status);

        setJobs(rows);
        activeRef.current = rows.some((j) => ACTIVE.has(j.status));
      } catch {
        /* backend may be briefly unavailable; keep polling */
      } finally {
        if (!cancelled) {
          timer = setTimeout(
            poll,
            activeRef.current ? ACTIVE_POLL_MS : IDLE_POLL_MS,
          );
        }
      }
    }

    pollNowRef.current = poll;
    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (refreshNonce > 0) pollNowRef.current();
  }, [refreshNonce]);

  const hasActive = jobs.some((j) => ACTIVE.has(j.status));
  useEffect(() => {
    if (!hasActive) return;
    const t = setInterval(() => setTick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, [hasActive]);

  const active = jobs.filter((j) => ACTIVE.has(j.status));
  const history = jobs.filter((j) => !ACTIVE.has(j.status)).slice(0, HISTORY_LIMIT);

  if (jobs.length === 0) return null;

  return (
    <Card>
      <CardContent className="space-y-3 pt-5">
        <div className="flex items-center justify-between text-sm">
          <h3 className="font-medium">
            Tasks
            {active.length > 0 && (
              <span className="ml-2 rounded bg-muted px-1.5 py-0.5 text-xs font-normal text-muted-foreground">
                {active.length} active
              </span>
            )}
          </h3>
          {history.length > 0 && (
            <button
              type="button"
              onClick={() => setShowHistory((v) => !v)}
              className="text-xs text-muted-foreground underline hover:text-foreground"
            >
              {showHistory ? "Hide recent" : `Recent (${history.length})`}
            </button>
          )}
        </div>

        {active.length === 0 && history.length === 0 && (
          <p className="text-xs text-muted-foreground">No recent tasks.</p>
        )}

        <ul className="space-y-3">
          {active.map((j) => (
            <TaskRow key={j.id} job={j} now={Date.now()} />
          ))}
        </ul>

        {showHistory && history.length > 0 && (
          <ul className="space-y-2 border-t pt-3">
            {history.map((j) => (
              <TaskRow key={j.id} job={j} now={Date.now()} compact />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function TaskRow({ job, now, compact = false }: { job: Job; now: number; compact?: boolean }) {
  const active = ACTIVE.has(job.status);
  const stats = (job.stats ?? {}) as Record<string, unknown>;
  const paperId = typeof stats.paper_id === "string" ? stats.paper_id : null;
  const paperTitle =
    (typeof stats.paper_title === "string" && stats.paper_title) ||
    (typeof stats.title === "string" && stats.title) ||
    null;
  const stage = typeof stats.stage === "string" ? stats.stage : null;
  const detail =
    (typeof stats.detail === "string" && stats.detail) ||
    (active ? "Working…" : job.status === "done" ? "Done" : job.message || "Failed");

  const stageLabel = active && stage && stage !== "queued" ? stageLabelFor(stage) : null;
  const titleEl = paperId ? (
    <Link
      to={`/papers/${encodeURIComponent(paperId)}`}
      className="font-medium hover:underline"
      title={paperTitle ?? paperId}
    >
      {paperTitle ?? paperId}
    </Link>
  ) : (
    <span className="font-medium" title={job.message ?? undefined}>
      {job.kind === "sync" ? "Sync" : paperTitle ?? "Task"}
    </span>
  );

  return (
    <li className="min-w-0">
      <div className="flex items-start gap-2.5">
        {active ? (
          <span className="mt-1 inline-block h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-foreground" />
        ) : (
          <span className={`mt-1.5 inline-block h-2.5 w-2.5 shrink-0 rounded-full ${jobColor(job.status)}`} />
        )}
        <div className="min-w-0 flex-1">
          <div className={compact ? "truncate text-sm" : "text-sm"}>{titleEl}</div>
          <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
            {stageLabel && (
              <span className="rounded bg-muted px-1.5 py-0.5 uppercase tracking-wide">
                {stageLabel}
              </span>
            )}
            <span className="truncate">{detail}</span>
          </div>
        </div>
        <span className="shrink-0 font-mono text-xs text-muted-foreground">
          {active ? elapsed(job.started_at, now) : formatTime(job.finished_at)}
        </span>
      </div>
      {job.status === "failed" && job.message && (
        <div className="mt-1 ml-6 truncate text-xs text-red-600" title={job.message}>
          {job.message}
        </div>
      )}
    </li>
  );
}

function stageLabelFor(stage: string): string {
  if (stage === "download") return "download";
  if (stage === "parse") return "parse";
  if (stage === "summarize") return "summarize";
  if (stage === "embed") return "embed";
  if (stage === "citations") return "citations";
  if (stage === "paper_extract") return "extract";
  if (stage === "scholar_compile") return "scholars";
  if (stage === "concept_compile") return "concepts";
  if (stage === "question_compile") return "questions";
  if (stage === "done") return "done";
  return stage;
}
