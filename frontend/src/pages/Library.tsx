import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusDot } from "@/components/StatusDot";
import { deletePaper, listPapers, type PaperSummary } from "@/api/client";

export default function Library() {
  const [papers, setPapers] = useState<PaperSummary[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    listPapers({ limit: 200 })
      .then(setPapers)
      .catch((e) => setErr(String(e)));
  }, []);

  async function handleDelete(p: PaperSummary) {
    const confirmed = window.confirm(
      `Delete "${p.title}"?\n\nThis removes the paper, its parsed text, embeddings, and downloaded PDF from the library. This cannot be undone.`,
    );
    if (!confirmed) return;
    setDeletingId(p.id);
    setErr(null);
    try {
      await deletePaper(p.id);
      setPapers((prev) => prev.filter((x) => x.id !== p.id));
    } catch (e) {
      setErr(`Failed to delete ${p.id}: ${String(e)}`);
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <main className="container max-w-screen-2xl space-y-4 py-8">
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
              <div className="flex shrink-0 items-center gap-3 text-xs text-muted-foreground">
                {p.citation_count !== null && p.citation_count !== undefined && (
                  <span title="Citations (Semantic Scholar)">🏆 {p.citation_count}</span>
                )}
                <span>{p.status}</span>
                <Button
                  variant="outline"
                  size="sm"
                  className="text-red-600 hover:bg-red-50 hover:text-red-700"
                  onClick={() => handleDelete(p)}
                  disabled={deletingId === p.id}
                  title="Delete paper and its files"
                >
                  {deletingId === p.id ? "Deleting…" : "Delete"}
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </main>
  );
}
