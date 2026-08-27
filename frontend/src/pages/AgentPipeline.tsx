import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft,
  Coins,
  FileText,
  Network,
  Sparkles,
  Wifi,
  type LucideIcon,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import {
  PIPELINES_BY_ID,
  type FlowNode,
  type Pipeline,
} from "@/lib/agentPipelines";
import { PromptEditor } from "@/components/PromptEditor";
import AgentRuns from "@/components/AgentRuns";
import {
  getUsageByFeature,
  getUsagePrompts,
  listJobs,
  type Job,
  type UsageBucket,
  type UsagePrompt,
} from "@/api/client";

function fmt(n: number): string {
  return n.toLocaleString();
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function StepNode({ node, isLast }: { node: FlowNode; isLast: boolean }) {
  const isLlm = node.kind === "llm";
  return (
    <li className="relative flex gap-3 pb-4 last:pb-0">
      {!isLast && (
        <span
          className="absolute left-[11px] top-6 h-[calc(100%-1rem)] w-px bg-border"
          aria-hidden
        />
      )}
      <span
        className={cn(
          "mt-1.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border",
          isLlm
            ? "border-amber-400 bg-amber-100 text-amber-800 dark:border-amber-700 dark:bg-amber-900/40 dark:text-amber-200"
            : "border-border bg-muted text-muted-foreground",
        )}
        title={isLlm ? "LLM step" : "data / service step"}
      >
        {isLlm ? (
          <Sparkles className="h-3 w-3" />
        ) : (
          <span className="block h-1.5 w-1.5 rounded-full bg-current" />
        )}
      </span>
      <div
        className={cn(
          "min-w-0 flex-1 rounded-md border p-2.5",
          isLlm
            ? "border-amber-300 bg-amber-50/50 dark:border-amber-800 dark:bg-amber-950/20"
            : "border-border bg-card",
        )}
      >
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-sm font-medium">{node.label}</span>
          {isLlm && node.feature && (
            <code className="rounded bg-amber-100 px-1 py-0.5 font-mono text-[10px] text-amber-800 dark:bg-amber-900/50 dark:text-amber-200">
              {node.feature}
            </code>
          )}
        </div>
        {node.description && (
          <p className="mt-1 text-[11px] leading-snug text-muted-foreground">
            {node.description}
          </p>
        )}
        {node.source && (
          <p
            className="mt-1 truncate font-mono text-[10px] text-muted-foreground/80"
            title={node.source}
          >
            {node.source}
          </p>
        )}
      </div>
    </li>
  );
}

function PromptInline({
  prompt,
  usage,
  onChanged,
}: {
  prompt: UsagePrompt;
  usage: UsageBucket | null;
  onChanged: (next: UsagePrompt) => void;
}) {
  return (
    <PromptEditor
      prompt={prompt}
      usage={usage}
      fmt={fmt}
      onChanged={onChanged}
    />
  );
}

function statusColor(status: string): string {
  switch (status) {
    case "done":
      return "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200";
    case "failed":
      return "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-200";
    case "running":
      return "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200";
    case "queued":
      return "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200";
    default:
      return "bg-muted text-muted-foreground";
  }
}

function DefinitionRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-2">
      <span className="w-20 shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span
        className={cn(
          "min-w-0 break-words text-foreground",
          mono && "font-mono text-[11px]",
        )}
      >
        {value}
      </span>
    </div>
  );
}

function PipelineHeader({ p }: { p: Pipeline }) {
  const Icon: LucideIcon = p.icon;
  const llmCount = p.nodes.filter((n) => n.kind === "llm").length;
  return (
    <div className="space-y-2">
      <div>
        <Link
          to="/agent"
          className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3 w-3" /> Back to Agent
        </Link>
      </div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-2.5">
          <div className="rounded-md bg-muted p-2 text-foreground">
            <Icon className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <h1 className="text-xl font-semibold">{p.name}</h1>
            <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
              {p.description}
            </p>
            <p className="mt-1 font-mono text-[10px] text-muted-foreground">
              Trigger: {p.trigger}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {llmCount > 0 ? (
            <span className="inline-flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/50 dark:text-amber-300">
              <Sparkles className="h-3 w-3" />
              {llmCount} LLM call{llmCount === 1 ? "" : "s"}
            </span>
          ) : (
            <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
              no LLM
            </span>
          )}
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
            {p.nodes.length} step{p.nodes.length === 1 ? "" : "s"}
          </span>
          {p.jobKinds.length > 0 && (
            <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
              {p.jobKinds.length} job kind
              {p.jobKinds.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export default function AgentPipeline() {
  const { pipelineId } = useParams<{ pipelineId: string }>();
  const pipeline = pipelineId ? PIPELINES_BY_ID[pipelineId] : undefined;

  const [prompts, setPrompts] = useState<UsagePrompt[]>([]);
  const [usageByFeature, setUsageByFeature] = useState<UsageBucket[]>([]);
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      if (!pipeline) return;
      setError(null);
      try {
        const tasks: Promise<unknown>[] = [
          getUsagePrompts(),
          getUsageByFeature(30),
        ];
        if (pipeline.jobKinds.length > 0) {
          tasks.push(
            Promise.all(
              pipeline.jobKinds.map((k) => listJobs({ kind: k, limit: 25 })),
            ).then((arrays) => {
              const merged = new Map<number, Job>();
              for (const arr of arrays) {
                for (const j of arr) {
                  if (!merged.has(j.id)) merged.set(j.id, j);
                }
              }
              return Array.from(merged.values()).sort((a, b) =>
                b.created_at.localeCompare(a.created_at),
              );
            }),
          );
        }
        const [p, byFeature, js] = await Promise.all(tasks);
        if (cancelled) return;
        setPrompts(p as UsagePrompt[]);
        setUsageByFeature(byFeature as UsageBucket[]);
        setJobs(pipeline.jobKinds.length > 0 ? (js as Job[]) : []);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [pipeline]);

  if (!pipeline) {
    return (
      <main className="container max-w-screen-2xl space-y-4 py-6">
        <Link
          to="/agent"
          className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-3 w-3" /> Back to Agent
        </Link>
        <Card>
          <CardContent className="p-4 text-sm">
            No pipeline named <code className="font-mono">{pipelineId}</code>.
            Available:{" "}
            {Object.keys(PIPELINES_BY_ID).map((id, i) => (
              <span key={id}>
                {i > 0 && ", "}
                <Link
                  to={`/agent/${id}`}
                  className="font-mono text-primary hover:underline"
                >
                  {id}
                </Link>
              </span>
            ))}
            .
          </CardContent>
        </Card>
      </main>
    );
  }

  const usageByKey = new Map(usageByFeature.map((b) => [b.key, b]));
  const llmFeatures = pipeline.nodes
    .map((n) => n.feature)
    .filter((f): f is string => Boolean(f));
  const llmPrompts = prompts.filter((p) => llmFeatures.includes(p.feature));
  const totalTokens = llmPrompts.reduce(
    (s, p) => s + (usageByKey.get(p.feature)?.total_tokens ?? 0),
    0,
  );
  const totalCalls = llmPrompts.reduce(
    (s, p) => s + (usageByKey.get(p.feature)?.calls ?? 0),
    0,
  );

  return (
    <main className="container max-w-screen-2xl space-y-4 py-6">
      <PipelineHeader p={pipeline} />

      {error && (
        <Card>
          <CardContent className="p-4 text-sm text-red-600">{error}</CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
                <Network className="h-3.5 w-3.5" /> Flow
              </CardTitle>
            </CardHeader>
            <CardContent>
              <ol className="m-0 list-none p-0">
                {pipeline.nodes.map((n, i) => (
                  <StepNode
                    key={i}
                    node={n}
                    isLast={i === pipeline.nodes.length - 1}
                  />
                ))}
              </ol>
              <div className="mt-3 rounded-md border border-dashed bg-muted/30 px-3 py-2 text-[11px] text-muted-foreground">
                {pipeline.output}
              </div>
            </CardContent>
          </Card>

          <AgentRuns pipelineId={pipeline.id} />

          {pipeline.jobKinds.length > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
                    <FileText className="h-3.5 w-3.5" /> Recent jobs
                  </CardTitle>
                  <span className="text-[10px] text-muted-foreground">
                    {pipeline.jobKinds.length} kind
                    {pipeline.jobKinds.length === 1 ? "" : "s"}:{" "}
                    {pipeline.jobKinds.map((k) => (
                      <code
                        key={k}
                        className="mx-0.5 rounded bg-muted px-1 font-mono text-[10px]"
                      >
                        {k}
                      </code>
                    ))}
                    {jobs ? ` · showing latest ${Math.min(jobs.length, 20)}` : ""}
                  </span>
                </div>
              </CardHeader>
              <CardContent>
                {jobs === null ? (
                  <div className="text-xs text-muted-foreground">Loading…</div>
                ) : jobs.length === 0 ? (
                  <div className="text-xs text-muted-foreground">
                    No jobs recorded for these kinds yet.
                  </div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-[11px]">
                      <thead>
                        <tr className="border-b text-left text-muted-foreground">
                          <th className="px-1 py-1 font-medium">id</th>
                          <th className="px-1 py-1 font-medium">kind</th>
                          <th className="px-1 py-1 font-medium">status</th>
                          <th className="px-1 py-1 font-medium">message</th>
                          <th className="px-1 py-1 font-medium">created</th>
                        </tr>
                      </thead>
                      <tbody>
                        {jobs.slice(0, 20).map((j) => (
                          <tr key={j.id} className="border-b last:border-0">
                            <td className="px-1 py-1 font-mono tabular-nums">
                              {j.id}
                            </td>
                            <td className="px-1 py-1 font-mono text-muted-foreground">
                              {j.kind}
                            </td>
                            <td className="px-1 py-1">
                              <span
                                className={cn(
                                  "rounded px-1.5 py-0.5 font-mono text-[10px]",
                                  statusColor(j.status),
                                )}
                              >
                                {j.status}
                              </span>
                            </td>
                            <td
                              className="max-w-xs truncate px-1 py-1"
                              title={j.message ?? ""}
                            >
                              {j.message ?? "—"}
                            </td>
                            <td className="px-1 py-1 text-muted-foreground">
                              {fmtTime(j.created_at)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </CardContent>
            </Card>
          )}

          {pipeline.relatedRoutes && pipeline.relatedRoutes.length > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
                  <Wifi className="h-3.5 w-3.5" /> Related routes
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ul className="m-0 space-y-1 p-0 text-[11px]">
                  {pipeline.relatedRoutes.map((r) => (
                    <li key={r}>
                      <code className="rounded bg-muted px-1.5 py-0.5 font-mono">
                        {r}
                      </code>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}
        </div>

        <div className="space-y-4">
          {llmPrompts.length > 0 && (
            <Card>
              <CardHeader className="pb-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
                    <Sparkles className="h-3.5 w-3.5 text-amber-600" /> LLM
                    prompts
                  </CardTitle>
                  <span className="text-[10px] text-muted-foreground">
                    {llmPrompts.length} flow
                    {llmPrompts.length === 1 ? "" : "s"} · {fmt(totalCalls)}{" "}
                    call{totalCalls === 1 ? "" : "s"} · {fmt(totalTokens)} tok
                    (30d)
                  </span>
                </div>
              </CardHeader>
              <CardContent>
                <div className="space-y-2">
                  {llmPrompts.map((p) => (
                    <PromptInline
                      key={p.feature}
                      prompt={p}
                      usage={usageByKey.get(p.feature) ?? null}
                      onChanged={(next) =>
                        setPrompts((prev) =>
                          prev.map((q) => (q.feature === next.feature ? next : q)),
                        )
                      }
                    />
                  ))}
                </div>
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
                <Coins className="h-3.5 w-3.5" /> Pipeline at a glance
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-[11px]">
              <DefinitionRow label="Pipeline id" value={pipeline.id} mono />
              <DefinitionRow
                label="Steps"
                value={String(pipeline.nodes.length)}
              />
              <DefinitionRow
                label="LLM calls"
                value={String(
                  pipeline.nodes.filter((n) => n.kind === "llm").length,
                )}
              />
              <DefinitionRow
                label="Job kinds"
                value={
                  pipeline.jobKinds.length === 0
                    ? "—"
                    : pipeline.jobKinds.join(", ")
                }
                mono
              />
              <DefinitionRow label="Output" value={pipeline.output} />
            </CardContent>
          </Card>
        </div>
      </div>

      <div className="pt-2">
        <Link
          to="/agent"
          className="inline-flex h-8 items-center justify-center rounded-md border border-input bg-background px-3 text-sm font-medium transition-colors hover:bg-muted"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          <span className="ml-1">Back to Agent</span>
        </Link>
      </div>
    </main>
  );
}
