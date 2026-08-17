import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { StatusDot } from "@/components/StatusDot";
import { listPapers, type PaperSummary } from "@/api/client";

export default function Library() {
  const [papers, setPapers] = useState<PaperSummary[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    listPapers({ limit: 200 })
      .then(setPapers)
      .catch((e) => setErr(String(e)));
  }, []);

  return (
    <main className="container space-y-4 py-8">
      <h1 className="text-2xl font-bold">Library</h1>
      <p className="text-sm text-muted-foreground">
        {papers.length} paper(s) in the database.
      </p>
      {err && <p className="text-sm text-red-600">{err}</p>}
      <div className="grid gap-2">
        {papers.map((p, i) => (
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
              <div className="text-xs text-muted-foreground">{p.status}</div>
            </CardContent>
          </Card>
        ))}
      </div>
    </main>
  );
}
