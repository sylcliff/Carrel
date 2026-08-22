import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { getJob, getWikiPageBySlug, recompileWikiPage, type Job, type WikiPageDetail as WikiDetail } from "@/api/client";
import MarkdownReader from "@/components/MarkdownReader";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

const VALID_KINDS = new Set(["concept", "scholar", "question"]);
const TERMINAL = new Set(["done", "failed"]);

export default function WikiPageDetail() {
  const { kind = "", slug = "" } = useParams<{ kind: string; slug: string }>();
  const [page, setPage] = useState<WikiDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const timer = useRef<number | null>(null);
  const valid = VALID_KINDS.has(kind);

  const load = useCallback(async () => {
    if (!valid) return;
    setPage(await getWikiPageBySlug(kind, slug));
  }, [kind, slug, valid]);

  useEffect(() => {
    setLoading(true); setError(null);
    load().catch((e) => setError(e instanceof Error ? e.message : String(e))).finally(() => setLoading(false));
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [load]);

  const recompile = async () => {
    if (!page) return;
    setError(null);
    try {
      const first = await recompileWikiPage(page.id);
      setJob(first);
      timer.current = window.setInterval(async () => {
        try {
          const next = await getJob(first.id);
          setJob(next);
          if (TERMINAL.has(next.status)) {
            if (timer.current) window.clearInterval(timer.current);
            if (next.status === "done") await load(); else setError(next.message || "Recompile failed.");
          }
        } catch (e) {
          if (timer.current) window.clearInterval(timer.current);
          setError(e instanceof Error ? e.message : String(e));
        }
      }, 1500);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  if (loading) return <main className="container max-w-screen-2xl py-6 text-sm text-muted-foreground">Loading…</main>;
  if (error && !page || !valid || !page) return <main className="container max-w-screen-2xl space-y-3 py-6"><Link to={valid ? `/wiki/${kind}` : "/wiki"} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"><ArrowLeft className="h-3.5 w-3.5" /> Wiki</Link><p className="text-sm text-red-600">{error ?? "Wiki page not found."}</p></main>;

  return (
    <main className="container max-w-screen-2xl space-y-5 py-6">
      <Link to={`/wiki/${kind}`} className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"><ArrowLeft className="h-3.5 w-3.5" /> {kind}</Link>
      <div className="flex items-start justify-between gap-4">
        <div><h1 className="text-2xl font-semibold">{page.title}</h1>{page.summary && <p className="mt-1 text-sm text-muted-foreground">{page.summary}</p>}</div>
        <Button variant="outline" onClick={recompile} disabled={!!job && !TERMINAL.has(job.status)}><RefreshCw className={`mr-2 h-4 w-4 ${job && !TERMINAL.has(job.status) ? "animate-spin" : ""}`} /> Recompile</Button>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground"><span>{Math.round(page.confidence * 100)}% confidence</span><span>{page.evidence_count} evidence</span>{page.compiled_at && <span>Compiled {new Date(page.compiled_at).toLocaleDateString()}</span>}<span className="break-all">{page.path}</span></div>
      {job && <p className="text-sm text-muted-foreground">{job.status}{job.message ? ` · ${job.message}` : ""}</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}
      <Card><CardContent className="p-5"><MarkdownReader body={page.body} mdPath={page.path} internal /></CardContent></Card>
      <div className="grid gap-5 lg:grid-cols-2">
        <section className="space-y-3"><h2 className="text-lg font-semibold">Sources</h2>{page.sources.length ? page.sources.map((source, index) => <Card key={`${source.paper_id}-${index}`}><CardContent className="space-y-1 p-4"><Link to={`/papers/${source.paper_id}`} className="text-sm font-medium text-primary hover:underline">{source.paper_title || source.paper_id}{source.year ? ` (${source.year})` : ""}</Link>{source.heading && <p className="text-xs text-muted-foreground">{source.heading}</p>}{source.quote && <blockquote className="border-l-2 pl-3 text-sm text-muted-foreground">{source.quote}</blockquote>}</CardContent></Card>) : <p className="text-sm text-muted-foreground">No sources listed.</p>}</section>
        <section className="space-y-3"><h2 className="text-lg font-semibold">Backlinks</h2>{page.backlinks.length ? <div className="space-y-2">{page.backlinks.map((backlink) => <Link key={backlink.id} to={`/wiki/${backlink.kind}/${backlink.slug}`} className="block rounded-md border p-3 text-sm hover:bg-muted/30">{backlink.title}</Link>)}</div> : <p className="text-sm text-muted-foreground">No backlinks yet.</p>}</section>
      </div>
    </main>
  );
}
