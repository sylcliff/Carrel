import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { compileWiki, getJob, listWikiPages, type Job, type WikiPageSummary } from "@/api/client";
import { WikiCompileStepper } from "@/components/WikiCompileStepper";

// Chat pulls in assistant-ui + markdown plugins; load it only on the wiki page.
const WikiChat = lazy(() =>
  import("@/components/WikiChat").then((m) => ({ default: m.WikiChat })),
);

const SECTIONS = [
  { kind: "concept", title: "Concepts", empty: "No concept pages yet." },
  { kind: "question", title: "Questions", empty: "No question pages yet." },
] as const;
const TERMINAL = new Set(["done", "failed"]);

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

  const pagesExist = !loading && pages.length > 0;

  return (
    <main className="container max-w-screen-2xl py-6">
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_400px]">
        <div className="min-w-0 space-y-6">
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
          {job && <WikiCompileStepper job={job} />}
          {error && <p className="text-sm text-red-600">{error}</p>}
          {loading ? <p className="text-sm text-muted-foreground">Loading…</p> : (
            <div className="grid gap-5 lg:grid-cols-2">
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
                          <div className="flex items-center justify-between gap-2">
                            <div className="font-medium">{page.title}</div>
                            {page.stub && (
                              <span
                                className="shrink-0 rounded-full bg-slate-200 px-2 py-0.5 text-xs text-slate-700"
                                title="Need ≥ 3 backing papers to LLM-compile"
                              >
                                stub
                              </span>
                            )}
                          </div>
                          {page.summary && <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">{page.summary}</p>}
                        </CardContent></Card>
                      </Link>
                    )) : <p className="rounded-lg border border-dashed p-5 text-sm text-muted-foreground">{section.empty}</p>}
                  </section>
                );
              })}
            </div>
          )}
        </div>

        <aside className="min-w-0 lg:sticky lg:top-14 lg:bottom-0 lg:self-start">
          <Suspense fallback={<div className="h-[70vh] w-full animate-pulse rounded-md bg-muted lg:h-[calc(100vh-3.5rem)]" />}>
            <WikiChat pagesExist={pagesExist} />
          </Suspense>
        </aside>
      </div>
    </main>
  );
}
