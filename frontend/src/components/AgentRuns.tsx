import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Clock,
  Coins,
  Filter,
  Loader2,
  Play,
  RefreshCw,
  Sparkles,
  X,
  XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  type AgentPipelineSummary,
  type AgentRunDetail,
  type AgentRunOut,
  type AgentStepOut,
  getAgentRun,
  listAgentPipelines,
  listAgentRuns,
} from "@/api/client";
import { PIPELINES_BY_ID } from "@/lib/agentPipelines";
import { cn } from "@/lib/utils";

function fmtTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

function StatusPill({ status }: { status: string }) {
  const map: Record<
    string,
    { cls: string; icon: React.ComponentType<{ className?: string }>; label: string }
  > = {
    success: {
      cls: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
      icon: CheckCircle2,
      label: "success",
    },
    failed: {
      cls: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
      icon: XCircle,
      label: "failed",
    },
    running: {
      cls: "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300",
      icon: Loader2,
      label: "running",
    },
    cancelled: {
      cls: "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-300",
      icon: CircleDashed,
      label: "cancelled",
    },
  };
  const entry = map[status] ?? {
    cls: "bg-muted text-muted-foreground",
    icon: CircleDashed,
    label: status,
  };
  const Icon = entry.icon;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium",
        entry.cls,
      )}
    >
      <Icon className={cn("h-3 w-3", status === "running" && "animate-spin")} />
      {entry.label}
    </span>
  );
}

function StepStatusPill({ status }: { status: string }) {
  const map: Record<string, string> = {
    success: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
    failed: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
    running: "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300",
    skipped: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
  };
  return (
    <span
      className={cn(
        "inline-flex shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium",
        map[status] ?? "bg-muted text-muted-foreground",
      )}
    >
      {status}
    </span>
  );
}

function PipelineRow({
  p,
  active,
  onSelect,
}: {
  p: AgentPipelineSummary;
  active: boolean;
  onSelect: () => void;
}) {
  const staticPipeline = PIPELINES_BY_ID[p.pipeline_id];
  const Icon = staticPipeline?.icon ?? Activity;
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left text-xs transition-colors",
        active
          ? "border-primary/60 bg-primary/5"
          : "border-transparent hover:border-border hover:bg-muted/40",
      )}
      aria-pressed={active}
    >
      <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate">
        <span className="font-medium">{p.pipeline_name}</span>
        <span className="ml-1 text-[10px] text-muted-foreground">
          · {p.run_count} run{p.run_count === 1 ? "" : "s"}
        </span>
      </span>
      {p.last_status && <StatusPill status={p.last_status} />}
    </button>
  );
}

function RunRow({
  run,
  onOpen,
}: {
  run: AgentRunOut;
  onOpen: () => void;
}) {
  const totalTokens =
    typeof run.summary?.total_tokens === "number" ? run.summary.total_tokens : null;
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full items-center gap-2 rounded-md border border-transparent px-2 py-1.5 text-left text-xs transition-colors hover:border-border hover:bg-muted/40"
    >
      <StatusPill status={run.status} />
      <span className="min-w-0 flex-1 truncate" title={run.subject ?? undefined}>
        {run.subject ?? <span className="text-muted-foreground">no subject</span>}
      </span>
      <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
        {fmtDuration(run.duration_ms)}
      </span>
      <span className="hidden shrink-0 text-[10px] tabular-nums text-muted-foreground sm:inline">
        {fmtTokens(totalTokens)} tok
      </span>
      <span className="hidden shrink-0 text-[10px] text-muted-foreground lg:inline">
        {fmtTime(run.started_at)}
      </span>
    </button>
  );
}

function StepRow({ step }: { step: AgentStepOut }) {
  const isLlm = step.kind === "llm";
  return (
    <li className="rounded-md border bg-card/40 p-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="w-5 shrink-0 text-right text-[10px] tabular-nums text-muted-foreground">
          #{step.seq}
        </span>
        <StepStatusPill status={step.status} />
        {isLlm ? (
          <Sparkles className="h-3 w-3 text-amber-600" />
        ) : (
          <Play className="h-3 w-3 text-muted-foreground" />
        )}
        <span className="min-w-0 flex-1 truncate font-medium" title={step.label}>
          {step.label}
        </span>
        {step.node_id && (
          <code className="hidden rounded bg-muted px-1 py-0.5 text-[10px] text-muted-foreground sm:inline">
            {step.node_id}
          </code>
        )}
        {step.feature && (
          <code className="hidden rounded bg-amber-100 px-1 py-0.5 text-[10px] text-amber-800 dark:bg-amber-900/50 dark:text-amber-200 sm:inline">
            {step.feature}
          </code>
        )}
        <span className="ml-auto shrink-0 text-[10px] tabular-nums text-muted-foreground">
          {fmtDuration(step.duration_ms)}
        </span>
      </div>
      {step.model && (
        <div className="mt-1 ml-7 text-[10px] text-muted-foreground">
          model: <code className="font-mono">{step.model}</code>
          {step.total_tokens != null && (
            <span className="ml-2">
              tokens: {fmtTokens(step.prompt_tokens)}↑ /{" "}
              {fmtTokens(step.completion_tokens)}↓ · {fmtTokens(step.total_tokens)} tot
            </span>
          )}
        </div>
      )}
      {step.error && (
        <div className="mt-1 ml-7 flex items-start gap-1 rounded bg-red-50 px-1.5 py-1 text-[11px] text-red-800 dark:bg-red-950/40 dark:text-red-200">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span className="break-all">{step.error}</span>
        </div>
      )}
      {step.output_summary && (
        <div className="mt-1 ml-7 whitespace-pre-wrap break-words text-[11px] text-foreground/80">
          {step.output_summary}
        </div>
      )}
    </li>
  );
}

function RunDetail({
  detail,
  loading,
  onClose,
}: {
  detail: AgentRunDetail | null;
  loading: boolean;
  onClose: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex justify-end bg-black/30"
      onClick={onClose}
    >
      <div
        className="flex h-full w-full max-w-2xl flex-col overflow-hidden bg-background shadow-xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Agent run detail"
      >
        <div className="flex items-center justify-between border-b px-4 py-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-sm font-medium">
              {detail ? <StatusPill status={detail.status} /> : null}
              <span className="truncate">
                {detail?.pipeline_name ?? detail?.pipeline_id ?? "Loading…"}
              </span>
            </div>
            {detail?.subject && (
              <p className="truncate text-[11px] text-muted-foreground">
                {detail.subject}
              </p>
            )}
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close">
            <X className="h-4 w-4" />
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto p-3 text-xs">
          {loading && <div className="text-muted-foreground">Loading run…</div>}
          {detail && (
            <>
              <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                <div className="rounded border bg-card p-2">
                  <div className="text-[10px] text-muted-foreground">Trigger</div>
                  <div className="font-mono text-xs">{detail.trigger}</div>
                </div>
                <div className="rounded border bg-card p-2">
                  <div className="text-[10px] text-muted-foreground">Started</div>
                  <div>{fmtTime(detail.started_at)}</div>
                </div>
                <div className="rounded border bg-card p-2">
                  <div className="text-[10px] text-muted-foreground">Duration</div>
                  <div>{fmtDuration(detail.duration_ms)}</div>
                </div>
                <div className="rounded border bg-card p-2">
                  <div className="text-[10px] text-muted-foreground">Steps</div>
                  <div>
                    {detail.success_count}/{detail.step_count} ok
                    {detail.failed_count > 0 && (
                      <span className="ml-1 text-red-600">
                        · {detail.failed_count} failed
                      </span>
                    )}
                  </div>
                </div>
              </div>
              {detail.summary && Object.keys(detail.summary).length > 0 && (
                <div className="mb-3 rounded border bg-card p-2">
                  <div className="mb-1 text-[10px] font-medium text-muted-foreground">
                    Summary
                  </div>
                  <pre className="overflow-x-auto whitespace-pre-wrap break-words text-[11px]">
                    {JSON.stringify(detail.summary, null, 2)}
                  </pre>
                </div>
              )}
              {detail.error && (
                <div className="mb-3 rounded border border-red-300 bg-red-50 p-2 text-[11px] text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
                  <div className="mb-1 flex items-center gap-1 font-medium">
                    <AlertTriangle className="h-3 w-3" /> Run error
                  </div>
                  {detail.error}
                </div>
              )}
              <ol className="space-y-1.5">
                {detail.steps.map((s) => (
                  <StepRow key={s.id} step={s} />
                ))}
              </ol>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export interface AgentRunsProps {
  /**
   * When set, the component is pinned to a single pipeline: the pipeline
   * sidebar is hidden, the runs request is hard-filtered to this id, and
   * the user can't switch pipelines from here. Used by the per-pipeline
   * page (`/agent/:pipelineId`) to show only the runs that belong to that
   * pipeline. When omitted, all pipelines are listed in the sidebar.
   */
  pipelineId?: string;
}

export default function AgentRuns({ pipelineId }: AgentRunsProps = {}) {
  const pinned = Boolean(pipelineId);
  const [pipelines, setPipelines] = useState<AgentPipelineSummary[]>([]);
  const [selectedPipeline, setSelectedPipeline] = useState<string | null>(
    pipelineId ?? null,
  );
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [runs, setRuns] = useState<AgentRunOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openRun, setOpenRun] = useState<AgentRunDetail | null>(null);
  const [openLoading, setOpenLoading] = useState(false);

  async function refreshPipelines() {
    if (pinned) return;
    try {
      const rows = await listAgentPipelines();
      setPipelines(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function refreshRuns() {
    setLoading(true);
    setError(null);
    try {
      const rows = await listAgentRuns({
        pipeline_id: pipelineId ?? selectedPipeline ?? undefined,
        status: statusFilter || undefined,
        limit: 50,
      });
      setRuns(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refreshPipelines();
  }, [pinned]);

  useEffect(() => {
    if (pipelineId) setSelectedPipeline(pipelineId);
  }, [pipelineId]);

  useEffect(() => {
    void refreshRuns();
  }, [selectedPipeline, statusFilter, pipelineId]);

  async function openRunDetail(id: number) {
    setOpenLoading(true);
    setOpenRun(null);
    try {
      const d = await getAgentRun(id);
      setOpenRun(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setOpenLoading(false);
    }
  }

  const headerRight = useMemo(() => {
    if (pinned) {
      const failed = runs.filter((r) => r.status === "failed").length;
      const lastSubject = runs[0]?.subject ?? null;
      return (
        <span className="text-[10px] text-muted-foreground">
          {runs.length} run{runs.length === 1 ? "" : "s"}
          {failed > 0 && (
            <span className="ml-2 text-red-600">· {failed} failed</span>
          )}
          {lastSubject && (
            <span className="ml-2 truncate" title={lastSubject}>
              · latest: {lastSubject}
            </span>
          )}
        </span>
      );
    }
    const totalRuns = pipelines.reduce((s, p) => s + p.run_count, 0);
    const failedRecent = pipelines.filter((p) => p.last_status === "failed").length;
    return (
      <span className="text-[10px] text-muted-foreground">
        {totalRuns} total run{totalRuns === 1 ? "" : "s"} ·{" "}
        {pipelines.length} pipeline{pipelines.length === 1 ? "" : "s"}
        {failedRecent > 0 && (
          <span className="ml-2 text-red-600">· {failedRecent} last failed</span>
        )}
      </span>
    );
  }, [pipelines, runs, pinned]);

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
            <Activity className="h-3.5 w-3.5" /> Recent agent runs
            {pinned && pipelineId && (
              <span className="ml-1 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                {pipelineId}
              </span>
            )}
          </CardTitle>
          <div className="flex items-center gap-2">
            {headerRight}
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void refreshPipelines();
                void refreshRuns();
              }}
              disabled={loading}
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
              />
              <span className="ml-1">Refresh</span>
            </Button>
          </div>
        </div>
        <p className="text-[11px] text-muted-foreground">
          {pinned
            ? "End-to-end execution trace for this pipeline. Click a run to see each step in order — node id, duration, token usage, and any error or output text."
            : "End-to-end execution trace for every pipeline. Click a run to see each step in order — node id, duration, token usage, and any error or output text."}
        </p>
      </CardHeader>
      <CardContent>
        {error && (
          <div className="mb-2 rounded border border-red-300 bg-red-50 px-2 py-1 text-[11px] text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-200">
            {error}
          </div>
        )}
        <div
          className={cn(
            "grid grid-cols-1 gap-3",
            !pinned && "lg:grid-cols-[260px_1fr]",
          )}
        >
          {!pinned && (
            <div className="space-y-1">
              <div className="mb-1 flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                <Filter className="h-3 w-3" /> Pipeline
              </div>
              <button
                type="button"
                onClick={() => setSelectedPipeline(null)}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left text-xs transition-colors",
                  selectedPipeline === null
                    ? "border-primary/60 bg-primary/5"
                    : "border-transparent hover:border-border hover:bg-muted/40",
                )}
              >
                <Activity className="h-3.5 w-3.5 text-muted-foreground" />
                <span className="flex-1 font-medium">All pipelines</span>
                <span className="text-[10px] tabular-nums text-muted-foreground">
                  {pipelines.reduce((s, p) => s + p.run_count, 0)}
                </span>
              </button>
              {pipelines.length === 0 ? (
                <p className="text-[11px] text-muted-foreground">
                  No runs yet — start a sync or process a paper to see records.
                </p>
              ) : (
                pipelines.map((p) => (
                  <PipelineRow
                    key={p.pipeline_id}
                    p={p}
                    active={selectedPipeline === p.pipeline_id}
                    onSelect={() => setSelectedPipeline(p.pipeline_id)}
                  />
                ))
              )}
            </div>
          )}
          <div>
            <div className="mb-1 flex flex-wrap items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              <Clock className="h-3 w-3" /> Runs
              <span className="ml-2 inline-flex items-center gap-1">
                status:
                {(["", "running", "success", "failed", "cancelled"] as const).map(
                  (s) => (
                    <button
                      key={s || "any"}
                      type="button"
                      onClick={() => setStatusFilter(s)}
                      className={cn(
                        "rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors",
                        statusFilter === s
                          ? "bg-primary/15 text-primary"
                          : "bg-muted/40 text-muted-foreground hover:bg-muted",
                      )}
                    >
                      {s || "any"}
                    </button>
                  ),
                )}
              </span>
            </div>
            {loading && runs.length === 0 ? (
              <div className="text-[11px] text-muted-foreground">Loading runs…</div>
            ) : runs.length === 0 ? (
              <div className="text-[11px] text-muted-foreground">
                No runs match the current filter.
              </div>
            ) : (
              <div className="space-y-1">
                {runs.map((r) => (
                  <RunRow
                    key={r.id}
                    run={r}
                    onOpen={() => {
                      void openRunDetail(r.id);
                    }}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
        <div className="mt-2 flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <Coins className="h-3 w-3" />
          Token totals are aggregated from the LLM steps inside each run.
        </div>
      </CardContent>
      {(openRun || openLoading) && (
        <RunDetail
          detail={openRun}
          loading={openLoading}
          onClose={() => {
            setOpenRun(null);
            setOpenLoading(false);
          }}
        />
      )}
    </Card>
  );
}
