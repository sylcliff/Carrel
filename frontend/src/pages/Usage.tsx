import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  Bot,
  Coins,
  Cpu,
  Layers,
  RefreshCw,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  getUsageByDay,
  getUsageByFeature,
  getUsageByModel,
  getUsageRecent,
  getUsageSummary,
  type UsageBucket,
  type UsageDay,
  type UsageRecent,
  type UsageSummary,
} from "@/api/client";

type WindowKey = "7" | "30" | "all";

const WINDOW_OPTIONS: { key: WindowKey; label: string; sinceDays?: number; days: number }[] = [
  { key: "7", label: "Last 7 days", sinceDays: 7, days: 7 },
  { key: "30", label: "Last 30 days", sinceDays: 30, days: 30 },
  { key: "all", label: "All time · 90d chart", days: 90 },
];

function fmt(n: number): string {
  return n.toLocaleString();
}

function fmtRelative(iso: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const ms = Date.now() - t;
  if (ms < 60_000) return "just now";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}

function featureLabel(feat: string): string {
  switch (feat) {
    case "summarize": return "Paper summarize";
    case "extract": return "Concept / question extract";
    case "topics": return "Topics classify";
    case "dedup_judge": return "Paper dedup LLM";
    case "wiki_scholar": return "Wiki · scholar";
    case "wiki_concept": return "Wiki · concept";
    case "wiki_question": return "Wiki · question";
    case "paper_chat": return "Paper chat";
    case "wiki_chat": return "Wiki chat";
    default: return feat;
  }
}

interface KpiTileProps {
  icon: React.ReactNode;
  label: string;
  value: string;
  hint?: string;
}

function KpiTile({ icon, label, value, hint }: KpiTileProps) {
  return (
    <Card>
      <CardContent className="flex items-start gap-3 p-4">
        <div className="rounded-md bg-muted p-2 text-muted-foreground">{icon}</div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs text-muted-foreground" title={label}>{label}</div>
          <div className="text-2xl font-semibold tabular-nums">{value}</div>
          {hint && <div className="truncate text-[11px] text-muted-foreground" title={hint}>{hint}</div>}
        </div>
      </CardContent>
    </Card>
  );
}

function BucketTable({ rows, labelKey }: { rows: UsageBucket[]; labelKey: (k: string) => string }) {
  if (rows.length === 0) {
    return <div className="text-xs text-muted-foreground">No calls yet.</div>;
  }
  const max = Math.max(...rows.map((r) => r.total_tokens), 1);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th className="py-1.5 pr-3 font-normal">{labelKey.length === 0 ? "Key" : "Label"}</th>
            <th className="py-1.5 pr-3 text-right font-normal">Calls</th>
            <th className="py-1.5 pr-3 text-right font-normal">Prompt</th>
            <th className="py-1.5 pr-3 text-right font-normal">Completion</th>
            <th className="py-1.5 pr-3 text-right font-normal">Total</th>
            <th className="py-1.5 text-left font-normal">Share</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-b last:border-0">
              <td className="py-1.5 pr-3" title={r.key}>
                <span className="truncate">{labelKey(r.key)}</span>
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">{fmt(r.calls)}</td>
              <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">{fmt(r.prompt_tokens)}</td>
              <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">{fmt(r.completion_tokens)}</td>
              <td className="py-1.5 pr-3 text-right tabular-nums">{fmt(r.total_tokens)}</td>
              <td className="py-1.5">
                <div className="h-1.5 w-full overflow-hidden rounded bg-muted">
                  <div
                    className="h-full bg-primary"
                    style={{ width: `${Math.max(2, Math.round((r.total_tokens / max) * 100))}%` }}
                  />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DayChart({ days }: { days: UsageDay[] }) {
  if (days.every((d) => d.total_tokens === 0)) {
    return <div className="text-xs text-muted-foreground">No calls in this window.</div>;
  }
  const max = Math.max(...days.map((d) => d.total_tokens), 1);
  const cellW = 100 / days.length;
  return (
    <div>
      <div className="flex h-32 w-full items-end gap-px">
        {days.map((d) => {
          const pct = Math.max(0.5, (d.total_tokens / max) * 100);
          const callPct = Math.max(0.5, Math.min(100, d.calls * 2));
          return (
            <div
              key={d.day}
              className="group relative flex-1"
              style={{ minWidth: 0 }}
              title={`${d.day} · ${fmt(d.total_tokens)} tokens · ${d.calls} calls`}
            >
              <div
                className="w-full rounded-t bg-primary/80"
                style={{ height: `${pct}%` }}
              />
              {d.calls > 0 && (
                <div
                  className="mt-px w-full bg-amber-500/70"
                  style={{ height: `${callPct}%` }}
                />
              )}
            </div>
          );
        })}
      </div>
      <div className="mt-1 flex justify-between text-[10px] text-muted-foreground tabular-nums">
        <span>{days[0]?.day}</span>
        <span>{days[days.length - 1]?.day}</span>
      </div>
      <div className="mt-2 flex items-center gap-3 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-sm bg-primary/80" /> total tokens
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-sm bg-amber-500/70" /> call count
        </span>
      </div>
      <div className="mt-1 text-[10px] text-muted-foreground" style={{ height: 0, opacity: 0 }}>
        {`bar-width: ${cellW.toFixed(2)}%`}
      </div>
    </div>
  );
}

function RecentList({ rows }: { rows: UsageRecent[] }) {
  if (rows.length === 0) {
    return <div className="text-xs text-muted-foreground">No calls yet.</div>;
  }
  return (
    <div className="max-h-72 overflow-y-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-background">
          <tr className="border-b text-left text-muted-foreground">
            <th className="py-1 pr-2 font-normal">When</th>
            <th className="py-1 pr-2 font-normal">Feature</th>
            <th className="py-1 pr-2 font-normal">Model</th>
            <th className="py-1 pr-2 text-right font-normal">Prompt</th>
            <th className="py-1 pr-2 text-right font-normal">Completion</th>
            <th className="py-1 text-right font-normal">Total</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b last:border-0">
              <td className="py-1 pr-2 text-muted-foreground">{fmtRelative(r.created_at)}</td>
              <td className="py-1 pr-2" title={r.feature}>
                {featureLabel(r.feature)}
                {r.paper_id && (
                  <Link
                    to={`/papers/${encodeURIComponent(r.paper_id)}`}
                    className="ml-1 text-[10px] text-muted-foreground hover:text-foreground"
                    title={`paper ${r.paper_id}`}
                  >
                    paper
                  </Link>
                )}
              </td>
              <td className="py-1 pr-2 truncate text-muted-foreground" title={r.model}>
                <span className="font-mono text-[11px]">{r.model}</span>
              </td>
              <td className="py-1 pr-2 text-right tabular-nums">{fmt(r.prompt_tokens)}</td>
              <td className="py-1 pr-2 text-right tabular-nums">{fmt(r.completion_tokens)}</td>
              <td className="py-1 text-right tabular-nums">{fmt(r.total_tokens)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Usage() {
  const [windowKey, setWindowKey] = useState<WindowKey>("30");
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  const [byFeature, setByFeature] = useState<UsageBucket[]>([]);
  const [byModel, setByModel] = useState<UsageBucket[]>([]);
  const [byDay, setByDay] = useState<UsageDay[]>([]);
  const [recent, setRecent] = useState<UsageRecent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const opt = WINDOW_OPTIONS.find((o) => o.key === windowKey)!;

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [sumS, featS, modelS, dayS, recentS] = await Promise.all([
        getUsageSummary(opt.sinceDays),
        getUsageByFeature(opt.sinceDays),
        getUsageByModel(opt.sinceDays),
        getUsageByDay(opt.days),
        getUsageRecent(20),
      ]);
      setSummary(sumS);
      setByFeature(featS);
      setByModel(modelS);
      setByDay(dayS);
      setRecent(recentS);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowKey]);

  return (
    <main className="container max-w-screen-2xl space-y-4 py-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Token usage</h1>
          <p className="text-xs text-muted-foreground">
            Per-call prompt + completion tokens recorded for every LLM request.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex overflow-hidden rounded-md border">
            {WINDOW_OPTIONS.map((o) => (
              <button
                key={o.key}
                type="button"
                onClick={() => setWindowKey(o.key)}
                className={`px-2.5 py-1 text-xs ${
                  windowKey === o.key ? "bg-primary text-primary-foreground" : "hover:bg-muted"
                }`}
              >
                {o.label}
              </button>
            ))}
          </div>
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            <span className="ml-1">Refresh</span>
          </Button>
        </div>
      </div>

      {error && (
        <Card>
          <CardContent className="p-4 text-sm text-red-600">{error}</CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <KpiTile
          icon={<Coins className="h-4 w-4" />}
          label="Total tokens"
          value={summary ? fmt(summary.total_tokens) : "—"}
          hint="prompt + completion"
        />
        <KpiTile
          icon={<Activity className="h-4 w-4" />}
          label="Prompt tokens"
          value={summary ? fmt(summary.prompt_tokens) : "—"}
          hint="input side"
        />
        <KpiTile
          icon={<Bot className="h-4 w-4" />}
          label="Completion tokens"
          value={summary ? fmt(summary.completion_tokens) : "—"}
          hint="output side"
        />
        <KpiTile
          icon={<Layers className="h-4 w-4" />}
          label="LLM calls"
          value={summary ? fmt(summary.calls) : "—"}
          hint="recorded"
        />
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium">Tokens per day</CardTitle>
        </CardHeader>
        <CardContent>
          <DayChart days={byDay} />
        </CardContent>
      </Card>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
              <Layers className="h-3.5 w-3.5" /> By feature
            </CardTitle>
          </CardHeader>
          <CardContent>
            <BucketTable rows={byFeature} labelKey={featureLabel} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-1.5 text-sm font-medium">
              <Cpu className="h-3.5 w-3.5" /> By model
            </CardTitle>
          </CardHeader>
          <CardContent>
            <BucketTable rows={byModel} labelKey={(k) => k} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium">Recent calls</CardTitle>
        </CardHeader>
        <CardContent>
          <RecentList rows={recent} />
        </CardContent>
      </Card>
    </main>
  );
}
