import { Link } from "react-router-dom";
import {
  ArrowRight,
  Network,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { PIPELINES, type Pipeline, type FlowNode } from "@/lib/agentPipelines";

function NodePill({ node }: { node: FlowNode }) {
  const isLlm = node.kind === "llm";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium",
        isLlm
          ? "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-200"
          : "border-border bg-muted/40 text-foreground",
      )}
      title={isLlm ? `LLM · feature="${node.feature}"` : undefined}
    >
      {isLlm && <Sparkles className="h-3 w-3" />}
      {node.label}
    </span>
  );
}

function PipelineCard({ p }: { p: Pipeline }) {
  const Icon: LucideIcon = p.icon;
  const llmCount = p.nodes.filter((n) => n.kind === "llm").length;
  return (
    <Link
      to={`/agent/${p.id}`}
      className="block rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      aria-label={`Open ${p.name} detail page`}
    >
      <Card className="h-full transition-colors hover:border-primary/40">
        <CardHeader className="space-y-1.5 pb-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
              <Icon className="h-3.5 w-3.5" />
              {p.name}
            </CardTitle>
            {llmCount > 0 ? (
              <span
                className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/50 dark:text-amber-300"
                title="Number of LLM calls per run of this pipeline"
              >
                {llmCount} LLM call{llmCount === 1 ? "" : "s"}
              </span>
            ) : (
              <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
                no LLM
              </span>
            )}
          </div>
          <p className="text-[11px] leading-snug text-muted-foreground">
            {p.description}
          </p>
          <p className="font-mono text-[10px] text-muted-foreground">
            Trigger: {p.trigger}
          </p>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-center gap-1">
            {p.nodes.map((n, i) => (
              <span key={i} className="contents">
                <NodePill node={n} />
                {i < p.nodes.length - 1 && (
                  <ArrowRight className="h-3 w-3 shrink-0 text-muted-foreground" />
                )}
              </span>
            ))}
          </div>
          <p className="mt-2 truncate text-[10px] text-muted-foreground">
            {p.output}
          </p>
        </CardContent>
      </Card>
    </Link>
  );
}

export default function AgentFlows() {
  const totalLlmFlows = PIPELINES.reduce(
    (s, p) => s + p.nodes.filter((n) => n.kind === "llm").length,
    0,
  );
  const llmPipelineCount = PIPELINES.filter((p) =>
    p.nodes.some((n) => n.kind === "llm"),
  ).length;
  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
            <Network className="h-3.5 w-3.5" /> Pipelines &amp; LLM agents
          </CardTitle>
          <span className="text-[10px] text-muted-foreground">
            {PIPELINES.length} pipelines · {llmPipelineCount} call an LLM ·{" "}
            {totalLlmFlows} LLM flows · click any card to drill in
          </span>
        </div>
        <p className="text-[11px] text-muted-foreground">
          Where each LLM call sits in the data lifecycle. Amber{" "}
          <Sparkles className="inline h-3 w-3 text-amber-600" /> nodes are the
          prompts cataloged below — the{" "}
          <code className="font-mono">feature</code> label is the same key used
          in the usage log, so a card here maps 1:1 to a prompt below.
        </p>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {PIPELINES.map((p) => (
            <PipelineCard key={p.id} p={p} />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
