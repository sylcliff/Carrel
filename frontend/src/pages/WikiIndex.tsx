import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { compileWiki, getJob, listWikiPages, type Job, type WikiPageSummary } from "@/api/client";

const SECTIONS = [
  { kind: "scholar", title: "Scholars", empty: "No scholar pages yet." },
  { kind: "concept", title: "Concepts", empty: "No concept pages yet." },
  { kind: "question", title: "Questions", empty: "No question pages yet." },
] as const;
const TERMINAL = new Set(["done", "failed"]);

function progressText(job: Job | null) {
  if (!job) return null;
  const nested = job.stats?.progress;
  const progress = job.progress ?? (typeof nested === "number" ? nested : null);
  return [job.message, progress !== null ? `${Math.round(progress <= 1 ? progress * 100 : progress)}%` : null]
    .filter(Boolean)
    .join(" · ");
}

export default function WikiIndex() {
  const [pages, setPages] = useState<WikiPageSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const timer = useRef<number | null>(null);

  const load = async () => {
    setPages(await listWikiPages());
  };

  useEffect(() => {
    load().catch((e) => setError(e instanceof Error ? e.message : String(e))).finally(() => setLoading(false));
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, []);

  const compile = async () => {
    setError(null);
    try {
      const first = await compileWiki({ limit: 20, background: true });
      setJob(first);
      timer.current = window.setInterval(async () => {
        try {
          const next = await getJob(first.id);
          setJob(next);
          if (TERMINAL.has(next.status)) {
            if (timer.current) window.clearInterval(timer.current);
            if (next.status === "done") await load();
            else setError(next.message || "Wiki compilation failed.");
          }
        } catch (e) {
          if (timer.current) window.clearInterval(timer.current);
          setError(e instanceof Error ? e.message : String(e));
        }
      }, 1500);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <main className="container max-w-screen-2xl space-y-6 py-6">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold"><BookOpen className="h-5 w-5" /> Wiki</h1>
          <p className="text-sm text-muted-foreground">Compiled research profiles and knowledge pages.</p>
        </div>
        <Button onClick={compile} disabled={!!job && !TERMINAL.has(job.status)}>
          <RefreshCw className={`mr-2 h-4 w-4 ${job && !TERMINAL.has(job.status) ? "animate-spin" : ""}`} />
          Compile wiki
        </Button>
      </div>
      {job && <p className="text-sm text-muted-foreground">{job.status}{progressText(job) ? ` · ${progressText(job)}` : ""}</p>}
      {error && <p className="text-sm text-red-600">{error}</p>}
      {loading ? <p className="text-sm text-muted-foreground">Loading…</p> : (
        <div className="grid gap-5 lg:grid-cols-3">
          {SECTIONS.map((section) => {
            const rows = pages.filter((page) => page.kind === section.kind);
            return (
              <section key={section.kind} className="space-y-3">
                <div className="flex items-baseline justify-between">
                  <h2 className="text-lg font-semibold">{section.title}</h2>
                  <Link to={`/wiki/${section.kind}`} className="text-xs text-muted-foreground hover:text-foreground">{rows.length} pages</Link>
                </div>
                {rows.length ? rows.map((page) => (
                  <Link key={page.id} to={`/wiki/${page.kind}/${page.slug}`} className="block">
                    <Card className="transition-colors hover:bg-muted/30"><CardContent className="p-4">
                      <div className="font-medium">{page.title}</div>
                      {page.summary && <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">{page.summary}</p>}
                    </CardContent></Card>
                  </Link>
                )) : <p className="rounded-lg border border-dashed p-5 text-sm text-muted-foreground">{section.empty}</p>}
              </section>
            );
          })}
        </div>
      )}
    </main>
  );
}
