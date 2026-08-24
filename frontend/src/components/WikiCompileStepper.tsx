import { useEffect, useState } from "react";
import { AlertCircle, Check, ChevronDown, ChevronRight, Loader2, Minus } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import type { Job } from "@/api/client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type StageKey = "paper_extract" | "scholar_compile" | "concept_compile" | "question_compile";

type StageStatus = "pending" | "running" | "done" | "skipped" | "failed";

interface IOEntry {
  name: string;
  input: string;
  output: string;
}

interface StageView {
  key: string;
  label: string;
  status: StageStatus;
  // Counters shown under the label; omitted keys are not rendered.
  counters: { label: string; value: number }[];
  // Live bar (1-based index, total) — undefined when the stage hasn't started.
  currentIndex?: number;
  currentTotal?: number;
  // Error text (when status=failed) or skip reason (when status=skipped).
  note?: string;
  // Most recent input/output snippet pairs captured from the pipeline.
  recent: IOEntry[];
}

const STAGE_LABELS: Record<StageKey, string> = {
  paper_extract: "Extract concepts & questions",
  scholar_compile: "Compile scholar pages",
  concept_compile: "Compile concept pages",
  question_compile: "Compile question pages",
};

const STAGE_ORDER: StageKey[] = [
  "paper_extract",
  "scholar_compile",
  "concept_compile",
  "question_compile",
];

const TERMINAL_STATUS = new Set(["done", "failed"]);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function asNumber(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function pickCounters(stageKey: string, sub: Record<string, unknown>) {
  // Per-stage counter names differ (paper_extract uses `extracted`; the
  // others use `compiled`/`stubbed`). Render whichever keys are present.
  const counters: { label: string; value: number }[] = [];
  const pushIf = (label: string, key: string) => {
    const v = asNumber(sub[key]);
    if (v !== undefined && v > 0) counters.push({ label, value: v });
  };
  if (stageKey === "paper_extract") {
    pushIf("extracted", "extracted");
    pushIf("failed", "failed");
    pushIf("skipped", "skipped");
  } else {
    pushIf("compiled", "compiled");
    pushIf("stubbed", "stubbed");
    pushIf("failed", "failed");
  }
  return counters;
}

function classifyStage(
  stageKey: string,
  sub: Record<string, unknown> | undefined,
  currentStage: string,
  jobStatus: string,
): { status: StageStatus; note?: string } {
  if (sub?.skipped === true) {
    return { status: "skipped", note: "No upstream changes" };
  }
  if (typeof sub?.error === "string") {
    return { status: "failed", note: sub.error };
  }
  if (currentStage === stageKey && !TERMINAL_STATUS.has(jobStatus)) {
    return { status: "running" };
  }
  // Has counts -> done. No sub and never started -> pending.
  if (sub && (asNumber(sub.compiled) ?? asNumber(sub.extracted)) !== undefined) {
    return { status: "done" };
  }
  return { status: "pending" };
}

// ---------------------------------------------------------------------------
// Stage column
// ---------------------------------------------------------------------------

function StatusDot({ status }: { status: StageStatus }) {
  if (status === "running") {
    return <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-600" aria-label="running" />;
  }
  if (status === "done") {
    return <Check className="h-3.5 w-3.5 text-emerald-600" aria-label="done" />;
  }
  if (status === "failed") {
    return <AlertCircle className="h-3.5 w-3.5 text-red-600" aria-label="failed" />;
  }
  if (status === "skipped") {
    return <Minus className="h-3.5 w-3.5 text-muted-foreground" aria-label="skipped" />;
  }
  return <span className="block h-2 w-2 rounded-full bg-muted-foreground/30" aria-label="pending" />;
}

function StageBar({
  status,
  index,
  total,
}: {
  status: StageStatus;
  index?: number;
  total?: number;
}) {
  let pct = 0;
  if (status === "done" || status === "skipped") {
    pct = 100;
  } else if (status === "running" && index !== undefined && total && total > 0) {
    pct = Math.max(2, Math.min(100, Math.round((index / total) * 100)));
  }
  const fillColor =
    status === "failed"
      ? "bg-red-500"
      : status === "skipped"
        ? "bg-muted-foreground/40"
        : "bg-primary";
  return (
    <div className="h-1.5 w-full overflow-hidden rounded bg-muted">
      <div
        className={`h-full transition-[width] duration-300 ${fillColor}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function IOBlock({ entry }: { entry: IOEntry }) {
  return (
    <div className="rounded border bg-background/50 p-1.5">
      {entry.name && (
        <div className="truncate text-[11px] font-medium" title={entry.name}>
          {entry.name}
        </div>
      )}
      {entry.input && (
        <details className="mt-0.5">
          <summary className="cursor-pointer text-[10px] uppercase tracking-wide text-muted-foreground hover:text-foreground">
            input
          </summary>
          <pre className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap break-words rounded bg-muted/50 p-1.5 font-mono text-[10.5px] leading-snug text-foreground/80">
            {entry.input}
          </pre>
        </details>
      )}
      {entry.output && (
        <details className="mt-0.5">
          <summary className="cursor-pointer text-[10px] uppercase tracking-wide text-muted-foreground hover:text-foreground">
            output
          </summary>
          <pre className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap break-words rounded bg-muted/50 p-1.5 font-mono text-[10.5px] leading-snug text-foreground/80">
            {entry.output}
          </pre>
        </details>
      )}
    </div>
  );
}

function StageColumn({ view }: { view: StageView }) {
  const { status } = view;
  const showCounts = view.counters.length > 0;
  const hasIO = view.recent.length > 0;
  // Default to open while the stage is running so live IO is visible; closed
  // when the stage is settled (avoids a wall of text after a long compile).
  const [open, setOpen] = useState(status === "running");
  useEffect(() => {
    if (status === "running") setOpen(true);
  }, [status]);
  return (
    <div className="flex min-w-0 flex-col gap-1.5 rounded-md border bg-card p-3">
      <div className="flex items-center gap-2">
        <StatusDot status={status} />
        <span className="truncate text-xs font-medium" title={view.label}>
          {view.label}
        </span>
      </div>
      <StageBar status={status} index={view.currentIndex} total={view.currentTotal} />
      {showCounts && (
        <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
          {view.counters.map((c) => (
            <span key={c.label}>
              {c.label} <span className="tabular-nums text-foreground">{c.value}</span>
            </span>
          ))}
        </div>
      )}
      {view.note && (
        <div
          className={`truncate text-[11px] ${
            status === "failed" ? "text-red-600" : "text-muted-foreground"
          }`}
          title={view.note}
        >
          {view.note}
        </div>
      )}
      {view.currentIndex !== undefined && view.currentTotal !== undefined && view.currentTotal > 0 && (
        <div className="text-[11px] tabular-nums text-muted-foreground">
          {view.currentIndex} / {view.currentTotal}
        </div>
      )}
      {hasIO && (
        <div className="mt-1 border-t pt-1.5">
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            className="flex w-full items-center gap-1 text-[10px] uppercase tracking-wide text-muted-foreground hover:text-foreground"
            aria-expanded={open}
          >
            {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            last {view.recent.length} input/output
            {view.recent.length === 1 ? "" : "s"}
          </button>
          {open && (
            <div className="mt-1 flex max-h-72 flex-col gap-1 overflow-y-auto">
              {view.recent.map((e, i) => (
                <IOBlock key={`${e.name}-${i}`} entry={e} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stepper
// ---------------------------------------------------------------------------

export interface WikiCompileStepperProps {
  job: Job | null;
  className?: string;
}

function asIOEntries(v: unknown): IOEntry[] {
  if (!Array.isArray(v)) return [];
  return v
    .filter((e): e is Record<string, unknown> => typeof e === "object" && e !== null)
    .map((e) => ({
      name: typeof e.name === "string" ? e.name : "",
      input: typeof e.input === "string" ? e.input : "",
      output: typeof e.output === "string" ? e.output : "",
    }));
}

function buildViews(job: Job): StageView[] {
  const stats = (job.stats ?? {}) as Record<string, unknown>;
  const currentStage = typeof stats.stage === "string" ? stats.stage : "";
  const jobStatus = job.status;

  // Recompile: one-stage variant. The recompile endpoint tags
  // stats.wiki_kind = "concept" | "scholar" | "question" and
  // stats.stage = "<kind>_compile".
  const wikiKind = typeof stats.wiki_kind === "string" ? stats.wiki_kind : null;
  if (job.kind === "wiki_recompile" && wikiKind) {
    const stageKey = `${wikiKind}_compile` as StageKey;
    const sub = (stats[stageKey] as Record<string, unknown> | undefined) ?? {};
    const { status, note } = classifyStage(stageKey, sub, currentStage, jobStatus);
    return [
      {
        key: stageKey,
        label: `Recompile ${wikiKind} page`,
        status,
        counters: pickCounters(stageKey, sub),
        currentIndex: asNumber(sub.current_index),
        currentTotal: asNumber(sub.current_total),
        note,
        recent: asIOEntries(sub.recent),
      },
    ];
  }

  // Full 4-stage compile.
  return STAGE_ORDER.map((stageKey) => {
    const sub = (stats[stageKey] as Record<string, unknown> | undefined) ?? {};
    const { status, note } = classifyStage(stageKey, sub, currentStage, jobStatus);
    return {
      key: stageKey,
      label: STAGE_LABELS[stageKey],
      status,
      counters: pickCounters(stageKey, sub),
      currentIndex: asNumber(sub.current_index),
      currentTotal: asNumber(sub.current_total),
      note,
      recent: asIOEntries(sub.recent),
    };
  });
}

function formatElapsed(startedAt: string | null): string {
  if (!startedAt) return "";
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return "";
  const ms = Date.now() - start;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  return `${m}:${rs.toString().padStart(2, "0")}`;
}

export function WikiCompileStepper({ job, className }: WikiCompileStepperProps) {
  // Refresh the elapsed-time display while the job is running.
  const [, force] = useState(0);
  useEffect(() => {
    if (!job) return;
    if (TERMINAL_STATUS.has(job.status)) return;
    const t = window.setInterval(() => force((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [job]);

  if (!job) return null;

  const stats = (job.stats ?? {}) as Record<string, unknown>;
  const views = buildViews(job);
  const isOneStage = views.length === 1;
  const gridCols = isOneStage
    ? "grid-cols-1"
    : "grid-cols-1 sm:grid-cols-2 xl:grid-cols-4";

  const pruned = asNumber(stats.pruned_pages);
  const recomputed = asNumber(stats.recomputed_backlinks);
  const isDone = job.status === "done";
  const isFailed = job.status === "failed";
  const elapsed = formatElapsed(job.started_at);

  return (
    <Card className={className}>
      <CardContent className="space-y-2.5 p-4">
        <div className={`grid gap-2 ${gridCols}`}>
          {views.map((v) => (
            <StageColumn key={v.key} view={v} />
          ))}
        </div>
        {job.message && (
          <div
            className={`truncate text-xs ${
              isFailed ? "text-red-600" : "text-muted-foreground"
            }`}
            title={job.message}
          >
            {job.message}
            {elapsed && !isDone && !isFailed ? ` · elapsed ${elapsed}` : ""}
          </div>
        )}
        {isDone && (pruned !== undefined || recomputed !== undefined) && (
          <div className="text-[11px] text-muted-foreground">
            {pruned !== undefined && (
              <span>
                Pruned {pruned} dead link{pruned === 1 ? "" : "s"}
              </span>
            )}
            {pruned !== undefined && recomputed !== undefined && <span> · </span>}
            {recomputed !== undefined && (
              <span>
                Refreshed {recomputed} backlink{recomputed === 1 ? "" : "s"}
              </span>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
