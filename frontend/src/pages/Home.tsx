import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  discardPaper,
  embedPending,
  getHealth,
  importPaperToLibrary,
  listPapers,
  listSubscriptions,
  processPending,
  summarizePending,
  triggerSync,
  type Health,
  type PaperSummary,
  type Subscription,
} from "@/api/client";
import { StatusDot } from "@/components/StatusDot";
import { TaskList } from "@/components/TaskList";
import { OaBadge } from "@/components/OaBadge";
import { TopJournalSection } from "@/components/TopJournalSection";
import { FileDown, X } from "lucide-react";

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [papers, setPapers] = useState<PaperSummary[]>([]);
  const [inbox, setInbox] = useState<PaperSummary[]>([]);
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [processing, setProcessing] = useState(false);
  const [embedding, setEmbedding] = useState(false);
  const [summarizing, setSummarizing] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [lastSync, setLastSync] = useState<{ fetched: number; discovered: number; updated: number; subscriptions: number } | null>(null);
  const [taskNonce, setTaskNonce] = useState(0);
  const [err, setErr] = useState<string | null>(null);

  async function refresh() {
    try {
      setHealth(await getHealth());
      const [library, discovered] = await Promise.all([
        listPapers({ limit: 30 }),
        listPapers({ in_library: false, limit: 100 }),
      ]);
      setPapers(library);
      setInbox(discovered);
      setSubs(await listSubscriptions());
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function onImport(p: PaperSummary) {
    setBusyId(p.id);
    setErr(null);
    try {
      await importPaperToLibrary(p.id);
      setInbox((prev) => prev.filter((x) => x.id !== p.id));
      await refresh();
    } catch (e) {
      setErr(`Import failed: ${String(e)}`);
    } finally {
      setBusyId(null);
    }
  }

  async function onDiscard(p: PaperSummary) {
    setBusyId(p.id);
    setErr(null);
    try {
      await discardPaper(p.id);
      setInbox((prev) => prev.filter((x) => x.id !== p.id));
    } catch (e) {
      setErr(`Discard failed: ${String(e)}`);
    } finally {
      setBusyId(null);
    }
  }

  async function onSync() {
    setSyncing(true);
    setErr(null);
    try {
      const job = await triggerSync(72, false); // inline so we get final stats
      if (job.stats) {
        setLastSync({
          fetched: Number(job.stats.fetched ?? 0),
          discovered: Number(job.stats.new_discovered ?? 0),
          updated: Number(job.stats.updated ?? 0),
          subscriptions: Number(job.stats.subscriptions ?? subs.length),
        });
      }
      setTaskNonce((n) => n + 1);
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setSyncing(false);
    }
  }

  async function onProcessPending() {
    setProcessing(true);
    setErr(null);
    try {
      await processPending(10, true);
      setTaskNonce((n) => n + 1);
    } catch (e) {
      setErr(String(e));
    } finally {
      setProcessing(false);
    }
  }

  async function onEmbedPending() {
    setEmbedding(true);
    setErr(null);
    try {
      await embedPending(20, true);
      setTaskNonce((n) => n + 1);
    } catch (e) {
      setErr(String(e));
    } finally {
      setEmbedding(false);
    }
  }

  async function onSummarizePending() {
    setSummarizing(true);
    setErr(null);
    try {
      await summarizePending(20, true);
      setTaskNonce((n) => n + 1);
    } catch (e) {
      setErr(String(e));
    } finally {
      setSummarizing(false);
    }
  }

  const today = papers.filter((p) => {
    if (!p.publication_date) return false;
    const d = new Date(p.publication_date);
    const ageMs = Date.now() - d.getTime();
    return ageMs < 72 * 3600 * 1000;
  });

  return (
    <main className="container max-w-screen-2xl py-8">
      <section className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Today</h1>
          <p className="text-sm text-muted-foreground">
            New papers from your subscriptions. {subs.length === 0 && (
              <>No subscriptions yet — <Link to="/subscriptions" className="underline">add some</Link>.</>
            )}
          </p>
        </div>
        <div className="flex gap-2">
          <Button onClick={onProcessPending} disabled={processing} variant="outline">
            {processing ? "Queuing…" : "Process pending"}
          </Button>
          <Button onClick={onSummarizePending} disabled={summarizing} variant="outline" title="Backfill LLM summaries for already-parsed papers">
            {summarizing ? "Queuing…" : "Summarize parsed"}
          </Button>
          <Button onClick={onEmbedPending} disabled={embedding} variant="outline">
            {embedding ? "Queuing…" : "Embed parsed"}
          </Button>
          <Button onClick={onSync} disabled={syncing}>
            {syncing ? "Syncing…" : "Sync now (72h)"}
          </Button>
        </div>
      </section>

      <div className="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_400px]">
        <div className="min-w-0 space-y-6">
          {err && (
            <Card className="border-red-300">
              <CardContent className="pt-5 text-sm text-red-600">{err}</CardContent>
            </Card>
          )}

          {lastSync && (
            <Card>
              <CardContent className="pt-5 text-sm">
                Last sync:{" "}
                <b>{lastSync.fetched}</b> fetched,{" "}
                <b className="text-green-600">{lastSync.discovered}</b> new discoveries,{" "}
                <b className="text-blue-600">{lastSync.updated}</b> updated
                {subs.length > 0 && (
                  <> across <b>{lastSync.subscriptions}</b> subscription(s)</>
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

          {inbox.length > 0 && (
            <section>
              <h2 className="mb-3 text-lg font-semibold">
                Discovered <span className="text-sm font-normal text-muted-foreground">({inbox.length})</span>
              </h2>
              <p className="-mt-2 mb-3 text-xs text-muted-foreground">
                New papers from your subscriptions. Import the ones you want to add to your library.
              </p>
              <div className="grid gap-3">
                {inbox.map((p, i) => (
                  <Card key={p.id}>
                    <CardContent className="flex items-start gap-3 p-4">
                      <span className="w-7 shrink-0 select-none pt-1 text-right text-sm tabular-nums text-muted-foreground">
                        {i + 1}
                      </span>
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
                      <div className="flex shrink-0 items-center gap-3 text-xs text-muted-foreground">
                        {p.citation_count !== null && p.citation_count !== undefined && (
                          <span title="Citations (Semantic Scholar)">🏆 {p.citation_count}</span>
                        )}
                        <OaBadge oaStatus={p.oa_status} status={p.status} />
                        <Button
                          size="sm"
                          onClick={() => onImport(p)}
                          disabled={busyId === p.id}
                          title="Add to library (metadata only)"
                        >
                          <FileDown className="mr-1 h-3.5 w-3.5" />
                          {busyId === p.id ? "Importing…" : "Import"}
                        </Button>
                        <button
                          type="button"
                          onClick={() => onDiscard(p)}
                          disabled={busyId === p.id}
                          className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                          title="Discard from inbox"
                          aria-label="Discard"
                        >
                          <X className="h-4 w-4" />
                        </button>
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            </section>
          )}

          <section>
            <h2 className="mb-3 text-lg font-semibold">
              Recently added <span className="text-sm font-normal text-muted-foreground">({today.length})</span>
            </h2>
            <div className="grid gap-3">
              {today.length === 0 && subs.length > 0 && (
                <Card>
                  <CardContent className="pt-5 text-sm text-muted-foreground">
                    Nothing in the last 72h. Import discoveries above or sync more subscriptions.
                  </CardContent>
                </Card>
              )}
              {today.map((p, i) => (
                <Card key={p.id}>
                  <CardContent className="flex items-start gap-3 p-4">
                    <div className="pt-1.5"><StatusDot s={p.status} /></div>
                    <span className="w-7 shrink-0 select-none pt-1 text-right text-sm tabular-nums text-muted-foreground">
                      {i + 1}
                    </span>
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
                    <div className="flex shrink-0 items-center gap-3 text-xs text-muted-foreground">
                      {p.citation_count !== null && p.citation_count !== undefined && (
                        <span title="Citations (Semantic Scholar)">🏆 {p.citation_count}</span>
                      )}
                      <OaBadge oaStatus={p.oa_status} status={p.status} />
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          </section>

          <TopJournalSection />
        </div>

        <aside className="min-w-0 lg:sticky lg:top-6 lg:self-start">
          <TaskList
            refreshNonce={taskNonce}
            onProcessed={() => {
              void refresh();
            }}
          />
        </aside>
      </div>

      {health && (
        <footer className="mt-8 text-xs text-muted-foreground">
          backend v{health.version} · db {health.db} · mineru {health.mineru}
        </footer>
      )}
    </main>
  );
}
