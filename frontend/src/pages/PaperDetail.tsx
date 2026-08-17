import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { getPaper, type PaperDetail as PaperDetailT } from "@/api/client";

export default function PaperDetail() {
  const { id } = useParams<{ id: string }>();
  const [p, setP] = useState<PaperDetailT | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    getPaper(id).then(setP).catch((e) => setErr(String(e)));
  }, [id]);

  if (err) return <main className="container py-8 text-sm text-red-600">{err}</main>;
  if (!p) return <main className="container py-8 text-sm text-muted-foreground">Loading…</main>;

  return (
    <main className="container max-w-3xl space-y-6 py-8">
      <div>
        <Link to="/" className="text-sm text-muted-foreground hover:underline">← Back</Link>
      </div>
      <header className="space-y-2">
        <h1 className="text-2xl font-bold">{p.title}</h1>
        <div className="text-sm text-muted-foreground">
          {p.authors.join(", ") || "—"}
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {p.venue && <span>📰 {p.venue}</span>}
          {p.publication_date && <span>📅 {p.publication_date}</span>}
          {p.doi && <span>DOI: {p.doi}</span>}
          {p.arxiv_id && <span>arXiv: {p.arxiv_id}</span>}
          <span>status: {p.status}</span>
          <span>oa: {p.oa_status}</span>
        </div>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Abstract</CardTitle>
        </CardHeader>
        <CardContent className="text-sm leading-relaxed">
          {p.abstract || <span className="text-muted-foreground">No abstract available.</span>}
        </CardContent>
      </Card>

      {p.pdf_url && (
        <div>
          <Button onClick={() => window.open(p.pdf_url!, "_blank", "noopener")}>
            Open PDF
          </Button>
        </div>
      )}

      {p.error && (
        <Card className="border-red-300">
          <CardContent className="pt-5 text-sm text-red-600">{p.error}</CardContent>
        </Card>
      )}
    </main>
  );
}
