import { Link } from "react-router-dom";
import { Star } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { StatusDot } from "@/components/StatusDot";
import { topicColorClass } from "@/lib/topicColor";
import type { PaperSummary } from "@/api/client";

/**
 * Compact, read-only list of papers. Used by the Scholar detail page and any
 * other view that just needs to link to papers. Library has its own richer row
 * (filters, delete) and does not use this.
 */
export function PaperList({ papers }: { papers: PaperSummary[] }) {
  if (papers.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center text-sm text-muted-foreground">
          No papers.
        </CardContent>
      </Card>
    );
  }
  return (
    <div className="grid gap-2">
      {papers.map((p) => (
        <Card key={p.id}>
          <CardContent className="flex items-start gap-3 p-4">
            <div className="flex items-center gap-1 pt-1.5">
              {p.favorite && (
                <Star className="h-3.5 w-3.5 fill-yellow-400 text-yellow-500" />
              )}
              <StatusDot s={p.status} />
            </div>
            <div className="min-w-0 flex-1">
              <Link
                to={`/papers/${encodeURIComponent(p.id)}`}
                className="font-medium hover:underline"
              >
                {p.title}
              </Link>
              <div className="text-xs text-muted-foreground">
                {p.authors.slice(0, 4).join(", ")}
                {p.authors.length > 4 ? " et al." : ""} · {p.venue ?? p.source}
                {p.publication_date && <> · {p.publication_date}</>}
              </div>
              {p.topics.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {p.topics.map((t) => (
                    <span
                      key={t}
                      className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${topicColorClass(t)}`}
                    >
                      {t}
                    </span>
                  ))}
                </div>
              )}
            </div>
            {p.citation_count !== null && p.citation_count !== undefined && (
              <div className="shrink-0 pt-1 text-xs text-muted-foreground">
                🏆 {p.citation_count}
              </div>
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
