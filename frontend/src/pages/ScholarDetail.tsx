import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import MarkdownReader from "@/components/MarkdownReader";
import { PaperList } from "@/components/PaperList";
import {
  getScholar,
  getJob,
  recompileWikiPage,
  type Job,
  type ScholarDetail as ScholarDetailT,
} from "@/api/client";
import { topicColorClass } from "@/lib/topicColor";

function Stat({
  label,
  value,
  href,
}: {
  label: string;
  value: number | string | null | undefined;
  href?: string;
}) {
  if (value === null || value === undefined || value === "") return null;
  const body = (
    <>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div
        className={`text-sm font-semibold tabular-nums ${
          href ? "text-primary hover:underline" : ""
        }`}
      >
        {value}
      </div>
    </>
  );
  const cls = "rounded-md border bg-muted/20 px-3 py-2";
  return href ? (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={`${cls} block transition-colors hover:bg-muted/40`}
    >
      {body}
    </a>
  ) : (
    <div className={cls}>{body}</div>
  );
}

export default function ScholarDetailPage() {
  const { key = "" } = useParams<{ key: string }>();
  const [data, setData] = useState<ScholarDetailT | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [wikiJob, setWikiJob] = useState<Job | null>(null);

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

  async function recompileProfile() {
    if (!data?.wiki_page) return;
    setError(null);
    try {
      const first = await recompileWikiPage(data.wiki_page.id);
      setWikiJob(first);
      const timer = window.setInterval(async () => {
        try {
          const next = await getJob(first.id);
          setWikiJob(next);
          if (next.status === "done" || next.status === "failed") {
            window.clearInterval(timer);
            if (next.status === "done") setData(await getScholar(key));
            else setError(next.message || "Profile recompile failed.");
          }
        } catch (e) {
          window.clearInterval(timer);
          setError(e instanceof Error ? e.message : String(e));
        }
      }, 1500);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

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
  // ORCID may arrive as a full URL or a bare 0000-... ID.
  const orcidUrl =
    profile?.orcid && /^https?:\/\//.test(profile.orcid)
      ? profile.orcid
      : profile?.orcid
        ? `https://orcid.org/${profile.orcid}`
        : null;

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
          {orcidUrl && (
            <Stat
              label="ORCID"
              value={orcidUrl.replace("https://orcid.org/", "")}
              href={orcidUrl}
            />
          )}
        </div>
      )}

      {data.wiki_page && (
        <section className="space-y-3">
          <div className="flex items-center justify-between gap-4">
            <div><h2 className="text-lg font-semibold">Research profile</h2><p className="text-xs text-muted-foreground">{Math.round(data.wiki_page.confidence * 100)}% confidence · {data.wiki_page.evidence_count} evidence</p></div>
            <Button variant="outline" size="sm" onClick={recompileProfile} disabled={!!wikiJob && wikiJob.status !== "done" && wikiJob.status !== "failed"}><RefreshCw className={`mr-2 h-3.5 w-3.5 ${wikiJob && wikiJob.status !== "done" && wikiJob.status !== "failed" ? "animate-spin" : ""}`} /> Recompile profile</Button>
          </div>
          {wikiJob && <p className="text-xs text-muted-foreground">{wikiJob.status}{wikiJob.message ? ` · ${wikiJob.message}` : ""}</p>}
          <Card><CardContent className="p-5"><MarkdownReader body={data.wiki_page.body} mdPath={data.wiki_page.path} internal /></CardContent></Card>
        </section>
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Papers in library</h2>
        <PaperList papers={papers} />
      </section>
    </main>
  );
}
