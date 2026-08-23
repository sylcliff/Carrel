import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, FileText, Link2 } from "lucide-react";
import { listWikiPages, type WikiPageSummary } from "@/api/client";
import { Card, CardContent } from "@/components/ui/card";

const LABELS: Record<string, string> = { concept: "Concepts", scholar: "Scholars", question: "Questions" };

export default function WikiPageList() {
  const { kind = "" } = useParams<{ kind: string }>();
  const [pages, setPages] = useState<WikiPageSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showRedirects, setShowRedirects] = useState(false);
  const valid = kind in LABELS;

  useEffect(() => {
    if (!valid) { setLoading(false); return; }
    setLoading(true);
    listWikiPages({ kind, includeRedirects: showRedirects })
      .then((rows) => setPages([...rows].sort((a, b) => b.evidence_count - a.evidence_count || b.confidence - a.confidence)))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [kind, valid, showRedirects]);

  return (
    <main className="container max-w-screen-2xl space-y-5 py-6">
      <Link to="/wiki" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"><ArrowLeft className="h-3.5 w-3.5" /> Wiki</Link>
      {!valid ? <p className="text-sm text-red-600">Unknown wiki page kind.</p> : (
        <>
          <div className="flex items-end justify-between gap-3">
            <div><h1 className="text-xl font-semibold">{LABELS[kind]}</h1><p className="text-sm text-muted-foreground">{pages.length} pages</p></div>
            <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                className="h-3.5 w-3.5"
                checked={showRedirects}
                onChange={(e) => setShowRedirects(e.target.checked)}
              />
              Show redirects
            </label>
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          {loading ? <p className="text-sm text-muted-foreground">Loading…</p> : pages.length === 0 ? <p className="rounded-lg border border-dashed p-5 text-sm text-muted-foreground">No {kind} pages yet.</p> : (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {pages.map((page) => {
                const isRedirect = page.redirects_to !== null;
                // D.7 stub pill: concept/question pages with < 3 backing papers.
                // Surfaces "what to backfill next" without re-parsing the file.
                const isStub = !isRedirect && page.stub;
                return <Link key={page.id} to={`/wiki/${kind}/${page.slug}`}>
                  <Card className={"h-full transition-colors hover:bg-muted/30" + (isRedirect ? " opacity-60" : "")}>
                    <CardContent className="space-y-3 p-5">
                      <div className="flex items-start justify-between gap-3">
                        <h2 className="font-semibold">{page.title}</h2>
                        {isRedirect ? (
                          <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-800">redirect</span>
                        ) : isStub ? (
                          <span
                            className="rounded-full bg-slate-200 px-2 py-0.5 text-xs text-slate-700"
                            title="Need ≥ 3 backing papers to LLM-compile"
                          >
                            stub · {page.evidence_count} evidence
                          </span>
                        ) : (
                          <span className="rounded-full bg-muted px-2 py-0.5 text-xs tabular-nums">{Math.round(page.confidence * 100)}%</span>
                        )}
                      </div>
                      {page.summary && <p className="line-clamp-3 text-sm text-muted-foreground">{page.summary}</p>}
                      {isRedirect && page.redirects_to && <p className="text-xs text-amber-700">→ {page.redirects_to}</p>}
                      <div className="flex flex-wrap gap-1">{page.tags.map((tag) => <span key={tag} className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">{tag}</span>)}</div>
                      <div className="flex gap-4 text-xs text-muted-foreground"><span className="inline-flex items-center gap-1"><FileText className="h-3.5 w-3.5" /> {page.evidence_count} evidence</span><span className="inline-flex items-center gap-1"><Link2 className="h-3.5 w-3.5" /> {page.links_in_count} links</span></div>
                    </CardContent>
                  </Card>
                </Link>;
              })}
            </div>
          )}
        </>
      )}
    </main>
  );
}
