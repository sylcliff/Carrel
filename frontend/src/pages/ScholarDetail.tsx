import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { PaperList } from "@/components/PaperList";
import {
  getScholar,
  type ScholarDetail as ScholarDetailT,
} from "@/api/client";
import { topicColorClass } from "@/lib/topicColor";

function Stat({ label, value }: { label: string; value: number | string | null | undefined }) {
  if (value === null || value === undefined || value === "") return null;
  return (
    <div className="rounded-md border bg-muted/20 px-3 py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}

export default function ScholarDetailPage() {
  const { key = "" } = useParams<{ key: string }>();
  const [data, setData] = useState<ScholarDetailT | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getScholar(key)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [key]);

  if (loading) {
    return (
      <main className="container max-w-screen-2xl py-6 text-sm text-muted-foreground">
        Loading…
      </main>
    );
  }
  if (error || !data) {
    return (
      <main className="container max-w-screen-2xl space-y-3 py-6">
        <Link to="/scholars" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-3.5 w-3.5" /> Scholars
        </Link>
        <p className="text-sm text-red-600">{error ?? "Scholar not found."}</p>
      </main>
    );
  }

  const { scholar, papers, profile } = data;
  const name = profile?.name || scholar.name;

  return (
    <main className="container max-w-screen-2xl space-y-5 py-6">
      <Link
        to="/scholars"
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" /> Scholars
      </Link>

      <Card>
        <CardContent className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center">
          <div
            className={`flex h-16 w-16 shrink-0 items-center justify-center rounded-full text-xl font-semibold ${topicColorClass(name)}`}
          >
            {name
              .trim()
              .split(/\s+/)
              .filter(Boolean)
              .slice(0, 2)
              .map((w) => w[0])
              .join("")
              .toUpperCase() || "?"}
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="text-xl font-semibold">{name}</h1>
            <p className="text-sm text-muted-foreground">
              {profile?.affiliation || scholar.affiliation || "Unknown affiliation"}
            </p>
            <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
              <span>
                <strong className="text-foreground">{papers.length}</strong>{" "}
                paper{papers.length === 1 ? "" : "s"} in your library
              </span>
              {(scholar.first_year || scholar.last_year) && (
                <span>
                  {scholar.first_year ?? "?"}–{scholar.last_year ?? "?"}
                </span>
              )}
              {scholar.total_citations > 0 && (
                <span>🏆 {scholar.total_citations} cites (in-library)</span>
              )}
              {!scholar.has_openalex && (
                <span className="italic">matched by name only</span>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {profile && (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="OpenAlex works" value={profile.works_count} />
          <Stat label="Total citations" value={profile.cited_by_count} />
          <Stat label="h-index" value={profile.h_index} />
          {profile.orcid && (
            <Stat
              label="ORCID"
              value={profile.orcid.replace("https://orcid.org/", "")}
            />
          )}
        </div>
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Papers in library</h2>
        <PaperList papers={papers} />
      </section>
    </main>
  );
}
