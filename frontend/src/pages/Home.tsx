import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { listPapers, getHealth, triggerSync, listSubscriptions, type Health, type Job, type PaperSummary, type Subscription } from "@/api/client";

function StatusDot({ s }: { s: string }) {
  const color =
    s === "ready" || s === "summarized" ? "bg-green-500" :
    s === "parsed" ? "bg-blue-500" :
    s === "pdf_ready" ? "bg-yellow-500" :
    s === "failed" ? "bg-red-500" :
    "bg-gray-400";
  return <span className={`inline-block h-2 w-2 rounded-full ${color}`} title={s} />;
}

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [papers, setPapers] = useState<PaperSummary[]>([]);
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [lastJob, setLastJob] = useState<Job | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function refresh() {
    try {
      setHealth(await getHealth());
      setPapers(await listPapers({ limit: 30 }));
      setSubs(await listSubscriptions());
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function onSync() {
    setSyncing(true);
    setErr(null);
    try {
      const job = await triggerSync(72, false); // inline so we get final stats
      setLastJob(job);
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setSyncing(false);
    }
  }

  const today = papers.filter((p) => {
    if (!p.publication_date) return false;
    const d = new Date(p.publication_date);
    const ageMs = Date.now() - d.getTime();
    return ageMs < 72 * 3600 * 1000;
  });

  return (
    <main className="container space-y-6 py-8">
      <section className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Today</h1>
          <p className="text-sm text-muted-foreground">
            New papers from your subscriptions. {subs.length === 0 && (
              <>No subscriptions yet — <Link to="/subscriptions" className="underline">add some</Link>.</>
            )}
          </p>
        </div>
        <Button onClick={onSync} disabled={syncing}>
          {syncing ? "Syncing…" : "Sync now (72h)"}
        </Button>
      </section>

      {err && (
        <Card className="border-red-300">
          <CardContent className="pt-5 text-sm text-red-600">{err}</CardContent>
        </Card>
      )}

      {lastJob && lastJob.stats && (
        <Card>
          <CardContent className="pt-5 text-sm">
            Last sync:{" "}
            <b>{String(lastJob.stats.fetched ?? 0)}</b> fetched,{" "}
            <b className="text-green-600">{String(lastJob.stats.new ?? 0)}</b> new,{" "}
            <b className="text-blue-600">{String(lastJob.stats.updated ?? 0)}</b> updated
            {subs.length > 0 && (
              <> across <b>{String(lastJob.stats.subscriptions ?? 0)}</b> subscription(s)</>
            )}
            .
          </CardContent>
        </Card>
      )}

      {subs.length === 0 && (
        <Card>
          <CardHeader>
            <CardTitle>No subscriptions</CardTitle>
            <CardDescription>
              Without subscriptions, "Sync now" has nothing to fetch. Add a keyword,
              arXiv category, author or venue on the Subscriptions page.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      <section>
        <h2 className="mb-3 text-lg font-semibold">
          Recently fetched <span className="text-sm font-normal text-muted-foreground">({today.length})</span>
        </h2>
        <div className="grid gap-3">
          {today.length === 0 && subs.length > 0 && (
            <Card>
              <CardContent className="pt-5 text-sm text-muted-foreground">
                Nothing in the last 72h. Try a longer sync or a different keyword.
              </CardContent>
            </Card>
          )}
          {today.map((p) => (
            <Card key={p.id}>
              <CardContent className="flex items-start gap-3 p-4">
                <div className="pt-1.5"><StatusDot s={p.status} /></div>
                <div className="min-w-0 flex-1">
                  <Link to={`/papers/${encodeURIComponent(p.id)}`} className="font-medium hover:underline">
                    {p.title}
                  </Link>
                  <div className="text-xs text-muted-foreground">
                    {p.authors.slice(0, 4).join(", ")}
                    {p.authors.length > 4 ? " et al." : ""} · {p.venue ?? p.source}
                    {p.publication_date && <> · {p.publication_date}</>}
                  </div>
                </div>
                <div className="text-xs text-muted-foreground">{p.status}</div>
              </CardContent>
            </Card>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold">
          Library <span className="text-sm font-normal text-muted-foreground">({papers.length})</span>
        </h2>
        <div className="grid gap-3">
          {papers.slice(0, 10).map((p) => (
            <Card key={p.id}>
              <CardContent className="flex items-start gap-3 p-4">
                <div className="pt-1.5"><StatusDot s={p.status} /></div>
                <div className="min-w-0 flex-1">
                  <Link to={`/papers/${encodeURIComponent(p.id)}`} className="font-medium hover:underline">
                    {p.title}
                  </Link>
                  <div className="text-xs text-muted-foreground">
                    {p.venue ?? p.source}{p.publication_date && <> · {p.publication_date}</>}
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
        <div className="mt-3 text-sm">
          <Link to="/library" className="underline">View full library →</Link>
        </div>
      </section>

      {health && (
        <footer className="text-xs text-muted-foreground">
          backend v{health.version} · db {health.db} · mineru {health.mineru}
        </footer>
      )}
    </main>
  );
}
