import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import MarkdownReader from "@/components/MarkdownReader";
import { PaperList } from "@/components/PaperList";
import {
  getScholar,
  getScholarWorks,
  getJob,
  importPaper,
  recompileWikiPage,
  type Job,
  type ScholarDetail as ScholarDetailT,
  type ScholarWork,
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

      <section className="space-y-3">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Published articles</h2>
            <p className="text-xs text-muted-foreground">
              All works indexed on OpenAlex for this author — newest first.
            </p>
          </div>
        </div>
        <PublishedArticles key={key} scholarKey={key} hasOpenAlex={data.scholar.has_openalex} />
      </section>
    </main>
  );
}

// ---------------------------------------------------------------------------
// PublishedArticles — paginated OpenAlex works for a scholar, with per-row
// "Import" (or "In library" link) and a "Load more" button.
// ---------------------------------------------------------------------------

function PublishedArticles({
  scholarKey,
  hasOpenAlex,
}: {
  scholarKey: string;
  hasOpenAlex: boolean;
}) {
  const [items, setItems] = useState<ScholarWork[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  // Per-openalex_id, so the same paper imported twice only shows a spinner
  // on the actual click — the previous click is already done.
  const [importing, setImporting] = useState<Set<string>>(new Set());
  const seqRef = useRef(0);

  // Reset everything when the URL key changes (navigating between scholars
  // without unmounting the page).
  useEffect(() => {
    seqRef.current += 1;
    const seq = seqRef.current;
    setItems([]);
    setCursor(null);
    setError(null);
    setDone(false);
    setLoading(true);

    if (!hasOpenAlex) {
      // Name-only author — the backend returns 422, so don't even try.
      setError(
        "This author has no OpenAlex ID yet, so their published works can't be listed. Run 'Resolve authors' on the Scholars page first.",
      );
      setLoading(false);
      setDone(true);
      return;
    }

    const ctrl = new AbortController();
    getScholarWorks(scholarKey, { signal: ctrl.signal })
      .then((res) => {
        if (seq !== seqRef.current) return;
        setItems(res.items);
        setCursor(res.next_cursor);
        setDone(res.next_cursor === null);
      })
      .catch((e) => {
        if (seq !== seqRef.current) return;
        if ((e as Error).name === "AbortError") return;
        setError(e instanceof Error ? e.message : String(e));
        setDone(true);
      })
      .finally(() => {
        if (seq === seqRef.current) setLoading(false);
      });
    return () => ctrl.abort();
  }, [scholarKey, hasOpenAlex]);

  const loadMore = useCallback(async () => {
    if (!cursor || loading) return;
    setLoading(true);
    setError(null);
    try {
      const res = await getScholarWorks(scholarKey, { cursor });
      setItems((prev) => [...prev, ...res.items]);
      setCursor(res.next_cursor);
      setDone(res.next_cursor === null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [cursor, loading, scholarKey]);

  async function handleImport(w: ScholarWork) {
    if (importing.has(w.openalex_id) || w.in_library) return;
    setImporting((prev) => new Set(prev).add(w.openalex_id));
    try {
      // Reuse the same POST /import that Search / citations use: it accepts
      // any of openalex_id / doi / arxiv_id, dedups, and moves an existing
      // inbox row into the library. The OA id is the strongest key.
      await importPaper({ openalex_id: w.openalex_id, title: w.title });
      // Mark the row in-place rather than re-fetching the whole page.
      setItems((prev) =>
        prev.map((it) =>
          it.openalex_id === w.openalex_id
            ? { ...it, in_library: true }
            : it,
        ),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setImporting((prev) => {
        const next = new Set(prev);
        next.delete(w.openalex_id);
        return next;
      });
    }
  }

  if (loading && items.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">Loading published works…</p>
    );
  }

  if (error && items.length === 0) {
    return (
      <p className="rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-400">
        {error}
      </p>
    );
  }

  if (items.length === 0) {
    return (
      <p className="rounded border border-dashed border-border/70 px-3 py-4 text-center text-xs text-muted-foreground">
        No published works found on OpenAlex.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <ul className="space-y-1.5">
        {items.map((w) => (
          <li
            key={w.openalex_id}
            className="rounded border border-border/60 px-3 py-2"
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                {w.in_library && w.library_id ? (
                  <Link
                    to={`/papers/${encodeURIComponent(w.library_id)}`}
                    className="text-sm font-medium hover:underline"
                  >
                    {w.title}
                  </Link>
                ) : (
                  <span className="text-sm font-medium">{w.title}</span>
                )}
                <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                  {w.year && <span>{w.year}</span>}
                  {w.venue && <span>{w.venue}</span>}
                  {w.cited_by_count != null && w.cited_by_count > 0 && (
                    <span>🏆 {w.cited_by_count} cites</span>
                  )}
                  {w.is_oa && (
                    <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-700 dark:text-emerald-400">
                      OA
                    </span>
                  )}
                  {w.doi && (
                    <a
                      href={w.doi}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="hover:underline"
                    >
                      doi
                    </a>
                  )}
                  {w.arxiv_id && (
                    <a
                      href={`https://arxiv.org/abs/${w.arxiv_id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="hover:underline"
                    >
                      arXiv
                    </a>
                  )}
                </div>
              </div>
              <div className="shrink-0">
                {w.in_library ? (
                  <span
                    className="inline-flex items-center rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:text-emerald-400"
                    title="Already in your library"
                  >
                    In library
                  </span>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => handleImport(w)}
                    disabled={importing.has(w.openalex_id)}
                  >
                    {importing.has(w.openalex_id) ? "Importing…" : "Import"}
                  </Button>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>
      {!done && (
        <div className="pt-1 text-center">
          <Button
            variant="outline"
            size="sm"
            onClick={loadMore}
            disabled={loading}
          >
            {loading ? "Loading…" : "Load more"}
          </Button>
        </div>
      )}
      {error && items.length > 0 && (
        <p className="rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}
