import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { listPapers, type PaperSummary } from "@/api/client";
import { OaBadge } from "@/components/OaBadge";

const JOURNALS = ["Nature", "Cell", "Science"] as const;

interface Bucket {
  journal: (typeof JOURNALS)[number];
  papers: PaperSummary[];
}

function isTopJournal(venue: string | null): (typeof JOURNALS)[number] | null {
  if (!venue) return null;
  const v = venue.trim();
  // Exact match only — "Nature Communications" / "Nature Genetics" shouldn't
  // show up in the Nature slot.
  if (v === "Nature" || v === "Cell" || v === "Science") return v;
  return null;
}

export function TopJournalSection() {
  const [buckets, setBuckets] = useState<Bucket[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // One broad query per journal. `venue` is a case-insensitive
        // substring match; we refine to exact-name match client-side because
        // Nature has ~200 sister journals that would otherwise flood in.
        const results = await Promise.all(
          JOURNALS.map((j) => listPapers({ venue: j, limit: 200 })),
        );
        if (cancelled) return;
        const grouped: Bucket[] = JOURNALS.map((journal, i) => ({
          journal,
          papers: results[i]
            .filter((p) => isTopJournal(p.venue) === journal)
            .slice(0, 6),
        }));
        setBuckets(grouped);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const total = buckets.reduce((n, b) => n + b.papers.length, 0);
  if (!loading && total === 0) return null;

  return (
    <section>
      <h2 className="mb-3 text-lg font-semibold">Top journals</h2>
      <div className="grid gap-4 md:grid-cols-3">
        {buckets.map((b) => (
          <Card key={b.journal}>
            <CardContent className="p-4">
              <div className="mb-2 flex items-center justify-between">
                <h3 className="font-semibold">{b.journal}</h3>
                <span className="text-xs text-muted-foreground">{b.papers.length}</span>
              </div>
              {loading ? (
                <p className="text-xs text-muted-foreground">Loading…</p>
              ) : b.papers.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  Nothing yet.{" "}
                  <Link to="/subscriptions" className="underline">
                    Subscribe
                  </Link>{" "}
                  and sync.
                </p>
              ) : (
                <ul className="space-y-2">
                  {b.papers.map((p) => (
                    <li key={p.id} className="min-w-0">
                      <Link
                        to={`/papers/${encodeURIComponent(p.id)}`}
                        className="line-clamp-2 text-sm hover:underline"
                        title={p.title}
                      >
                        {p.title}
                      </Link>
                      <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                        {p.publication_date && <span>{p.publication_date}</span>}
                        <OaBadge oaStatus={p.oa_status} status={p.status} />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}
