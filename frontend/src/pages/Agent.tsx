import { useEffect, useState } from "react";
import {
  Bot,
  ChevronDown,
  ChevronRight,
  Coins,
  FileText,
  RefreshCw,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import AgentFlows from "@/components/AgentFlows";
import {
  getUsageByFeature,
  getUsagePrompts,
  type UsageBucket,
  type UsagePrompt,
} from "@/api/client";

function fmt(n: number): string {
  return n.toLocaleString();
}

function PromptBlock({ text, label }: { text: string; label: string }) {
  return (
    <div className="space-y-1">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <pre className="max-h-80 overflow-auto rounded-md border bg-muted/30 p-3 text-[11px] leading-relaxed text-foreground whitespace-pre-wrap break-words">
        {text}
      </pre>
    </div>
  );
}

function PromptCard({
  prompt,
  usage,
}: {
  prompt: UsagePrompt;
  usage: UsageBucket | null;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-muted/50"
        aria-expanded={open}
      >
        <div className="mt-0.5 text-muted-foreground">
          {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-sm font-medium">{prompt.label}</span>
            <span
              className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
              title="Token usage feature name"
            >
              {prompt.feature}
            </span>
            {usage && (
              <span
                className="ml-auto rounded bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] text-primary tabular-nums"
                title={`${usage.calls} LLM call(s) recorded in the last 30 days`}
              >
                {fmt(usage.total_tokens)} tok · {usage.calls} call{usage.calls === 1 ? "" : "s"}
              </span>
            )}
          </div>
          <div
            className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground"
            title={prompt.source}
          >
            {prompt.source}
          </div>
          {prompt.notes && (
            <div className="mt-1 text-[11px] text-muted-foreground">{prompt.notes}</div>
          )}
        </div>
      </button>
      {open && (
        <div className="space-y-3 border-t px-3 py-3">
          <PromptBlock text={prompt.system} label="System prompt" />
          <PromptBlock text={prompt.user_template} label="User prompt (template)" />
        </div>
      )}
    </div>
  );
}

function PromptsSection({
  prompts,
  loading,
  usageByKey,
}: {
  prompts: UsagePrompt[];
  loading: boolean;
  usageByKey: Map<string, UsageBucket>;
}) {
  if (loading && prompts.length === 0) {
    return <div className="text-xs text-muted-foreground">Loading prompts…</div>;
  }
  if (prompts.length === 0) {
    return <div className="text-xs text-muted-foreground">No prompts registered.</div>;
  }
  return (
    <div className="space-y-2">
      {prompts.map((p) => (
        <PromptCard key={p.feature} prompt={p} usage={usageByKey.get(p.feature) ?? null} />
      ))}
    </div>
  );
}

export default function Agent() {
  const [prompts, setPrompts] = useState<UsagePrompt[]>([]);
  const [usageByFeature, setUsageByFeature] = useState<UsageBucket[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [p, byFeature] = await Promise.all([
        getUsagePrompts(),
        getUsageByFeature(30),
      ]);
      setPrompts(p);
      setUsageByFeature(byFeature);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  const usageByKey = new Map(usageByFeature.map((b) => [b.key, b]));
  const totalTokens = usageByFeature.reduce((s, b) => s + b.total_tokens, 0);
  const totalCalls = usageByFeature.reduce((s, b) => s + b.calls, 0);
  const trackedFeatures = prompts.filter((p) => usageByKey.has(p.feature)).length;
  const untracked = usageByFeature.filter((b) => !prompts.some((p) => p.feature === b.key));

  return (
    <main className="container max-w-screen-2xl space-y-4 py-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Agent</h1>
          <p className="text-xs text-muted-foreground">
            Every LLM prompt the app issues — system instructions and user-template
            shape, with the last 30 days of token usage per prompt.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          <span className="ml-1">Refresh</span>
        </Button>
      </div>

      {error && (
        <Card>
          <CardContent className="p-4 text-sm text-red-600">{error}</CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="flex items-start gap-3 p-4">
            <div className="rounded-md bg-muted p-2 text-muted-foreground">
              <FileText className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs text-muted-foreground" title="Prompts in catalog">
                Prompts in catalog
              </div>
              <div className="text-2xl font-semibold tabular-nums">{fmt(prompts.length)}</div>
              <div className="truncate text-[11px] text-muted-foreground">
                {trackedFeatures} with recorded usage (30d)
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-start gap-3 p-4">
            <div className="rounded-md bg-muted p-2 text-muted-foreground">
              <Coins className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs text-muted-foreground" title="Tokens consumed by tracked prompts">
                Tokens (30d)
              </div>
              <div className="text-2xl font-semibold tabular-nums">{fmt(totalTokens)}</div>
              <div className="truncate text-[11px] text-muted-foreground">
                across {fmt(totalCalls)} LLM call{totalCalls === 1 ? "" : "s"}
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-start gap-3 p-4">
            <div className="rounded-md bg-muted p-2 text-muted-foreground">
              <Bot className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs text-muted-foreground" title="Cataloged features with at least one call">
                Active prompts
              </div>
              <div className="text-2xl font-semibold tabular-nums">
                {fmt(trackedFeatures)}
                <span className="ml-1 text-sm font-normal text-muted-foreground">
                  / {fmt(prompts.length)}
                </span>
              </div>
              <div className="truncate text-[11px] text-muted-foreground">
                cataloged features that fired
              </div>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-start gap-3 p-4">
            <div className="rounded-md bg-muted p-2 text-muted-foreground">
              <FileText className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs text-muted-foreground" title="Usage features missing from the catalog">
                Uncatalogued usage
              </div>
              <div className="text-2xl font-semibold tabular-nums">{fmt(untracked.length)}</div>
              <div className="truncate text-[11px] text-muted-foreground">
                usage rows not in the prompt catalog
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      <AgentFlows />

      <Card>
        <CardHeader className="pb-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
              <FileText className="h-3.5 w-3.5" /> Prompts in use
            </CardTitle>
            <span className="text-[10px] text-muted-foreground">
              {loading
                ? "Loading…"
                : `${prompts.length} prompt${prompts.length === 1 ? "" : "s"} · click to expand`}
            </span>
          </div>
        </CardHeader>
        <CardContent>
          <PromptsSection
            prompts={prompts}
            loading={loading}
            usageByKey={usageByKey}
          />
        </CardContent>
      </Card>

      {untracked.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-amber-700 dark:text-amber-400">
              Usage rows not in the catalog
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="mb-2 text-xs text-muted-foreground">
              The token-usage log recorded calls for these feature names but the
              prompt catalog has no matching entry — likely a new call site that
              wasn't added to <code className="font-mono">carrel.prompts</code>.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {untracked.map((b) => (
                <span
                  key={b.key}
                  className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
                  title={`${b.calls} call(s) · ${fmt(b.total_tokens)} tokens`}
                >
                  {b.key}
                </span>
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </main>
  );
}
