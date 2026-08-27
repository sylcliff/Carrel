import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Globe, RefreshCw } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import MarkdownReader from "@/components/MarkdownReader";
import { PaperList } from "@/components/PaperList";
import {
  enrichScholarPage,
  getScholar,
  getScholarWorks,
  getJob,
  getScholarSyncStatus,
  importPaper,
  recompileWikiPage,
  refreshScholarWorks,
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
  const [enrichJob, setEnrichJob] = useState<Job | null>(null);

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

  async function enrichProfile() {
    if (!data?.wiki_page) return;
    setError(null);
    try {
      const first = await enrichScholarPage(data.wiki_page.id);
      setEnrichJob(first);
      const timer = window.setInterval(async () => {
        try {
          const next = await getJob(first.id);
          setEnrichJob(next);
          if (next.status === "done" || next.status === "failed") {
            window.clearInterval(timer);
            if (next.status === "done") setData(await getScholar(key));
            else setError(next.message || "Research failed.");
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
            <div className="flex items-center gap-2">
              <Button
                variant="default"
                size="sm"
                onClick={enrichProfile}
                disabled={!!enrichJob && enrichJob.status !== "done" && enrichJob.status !== "failed"}
                title="Run an LLM agent that searches the web and appends a 'Web research' note to this page's user section."
              >
                <Globe className={`mr-2 h-3.5 w-3.5 ${enrichJob && enrichJob.status !== "done" && enrichJob.status !== "failed" ? "animate-spin" : ""}`} /> Research &amp; enrich
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={recompileProfile}
                disabled={!!wikiJob && wikiJob.status !== "done" && wikiJob.status !== "failed"}
              >
                <RefreshCw className={`mr-2 h-3.5 w-3.5 ${wikiJob && wikiJob.status !== "done" && wikiJob.status !== "failed" ? "animate-spin" : ""}`} /> Recompile profile
              </Button>
            </div>
          </div>
          {wikiJob && <p className="text-xs text-muted-foreground">Recompile · {wikiJob.status}{wikiJob.message ? ` · ${wikiJob.message}` : ""}</p>}
          {enrichJob && <p className="text-xs text-muted-foreground">Enrich · {enrichJob.status}{enrichJob.message ? ` · ${enrichJob.message}` : ""}</p>}
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
// "Import" (or "In library" link) and a "Load all" button that drains the
// remaining pages in a single click. Default page size is 50; for an author
// with a few hundred works a single click is enough to surface everything.
// ---------------------------------------------------------------------------

const PAGE_SIZE = 50;

function PublishedArticles({
  scholarKey,
  hasOpenAlex,
}: {
  scholarKey: string;
  hasOpenAlex: boolean;
}) {
  const [items, setItems] = useState<ScholarWork[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  // True while a "Load all" click is auto-paginating through every remaining
  // page. Distinct from the initial ``loading`` so we can show a per-page
  // "Loaded N of M" progress hint vs. the first-page "Loading…" placeholder.
  const [loadingAll, setLoadingAll] = useState(false);
  // Per-openalex_id, so the same paper imported twice only shows a spinner
  // on the actual click — the previous click is already done.
  const [importing, setImporting] = useState<Set<string>>(new Set());
  // While the backend is running its first OpenAlex cursor walk, poll
  // /sync_status and re-fetch the page when the sync finishes.
  const [syncStatus, setSyncStatus] = useState<string | null>(null);
  // ``worksJob`` mirrors ``wikiJob`` on the profile section: a Job we
  // create when the user clicks "Refresh from OpenAlex", polled via
  // ``getJob`` until done/failed.
  const [worksJob, setWorksJob] = useState<Job | null>(null);
  const seqRef = useRef(0);

  // Reset everything when the URL key changes (navigating between scholars
  // without unmounting the page).
  useEffect(() => {
    seqRef.current += 1;
    const seq = seqRef.current;
    setItems([]);
    setCursor(null);
    setTotal(null);
    setError(null);
    setDone(false);
    setLoadingAll(false);
    setSyncStatus(null);
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
    const reload = () =>
      getScholarWorks(scholarKey, { limit: PAGE_SIZE, signal: ctrl.signal })
        .then((res) => {
          if (seq !== seqRef.current) return;
          setSyncStatus(res.status);
          setItems(res.items);
          setCursor(res.next_cursor);
          setTotal(res.total ?? null);
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

    reload();

    // If the first call says the cache is loading, start polling the
    // sync-status endpoint until it transitions out of ``loading`` (or
    // 30s elapse) and re-fetch the page. The same interval is reused
    // for failed/missing → re-fetch after a backfill.
    let pollTimer: number | null = null;
    const startPolling = () => {
      if (pollTimer !== null) return;
      pollTimer = window.setInterval(async () => {
        if (seq !== seqRef.current) {
          window.clearInterval(pollTimer!);
          return;
        }
        try {
          const s = await getScholarSyncStatus(scholarKey);
          if (seq !== seqRef.current) return;
          setSyncStatus(s.status);
          if (s.status !== "loading") {
            window.clearInterval(pollTimer!);
            pollTimer = null;
            reload();
          }
        } catch {
          // transient — try again next tick
        }
      }, 2000);
    };

    // Watch the first response: if it was ``loading``, begin polling.
    // Use a small effect to react to syncStatus changes after the first
    // response settles.
    const statusWatcher = window.setInterval(() => {
      // No-op: we read syncStatus via the closure below.
    }, 999_999);
    const intervalId = statusWatcher;

    // React to syncStatus updates: start polling as soon as we see
    // ``loading`` for this scholar.
    let started = false;
    const checkStart = () => {
      if (started) return;
      // Read the latest syncStatus via a ref-stored closure.
      // We use a microtask to wait for the first reload to populate
      // syncStatus, then start polling.
      queueMicrotask(() => {
        if (seq !== seqRef.current) return;
        // peek: if state has settled to loading, start polling
        // (the first reload promise will have set syncStatus by now
        // in the common case)
        setSyncStatus((prev) => {
          if (prev === "loading") startPolling();
          return prev;
        });
        started = true;
      });
    };
    checkStart();
    void intervalId; // not used; satisfies the unused var lint

    return () => {
      ctrl.abort();
      window.clearInterval(intervalId);
      if (pollTimer !== null) window.clearInterval(pollTimer);
    };
  }, [scholarKey, hasOpenAlex]);

  // If syncStatus flips to ``loading`` after the initial fetch (e.g.
  // a manual refresh kicked off a sync), start polling.
  useEffect(() => {
    if (syncStatus !== "loading") return;
    let cancelled = false;
    let pollTimer: number | null = null;
    const tick = async () => {
      if (cancelled) return;
      try {
        const s = await getScholarSyncStatus(scholarKey);
        if (cancelled) return;
        setSyncStatus(s.status);
        if (s.status === "loading") {
          pollTimer = window.setTimeout(tick, 2000);
        } else {
          // re-fetch on the success/fail edge
          try {
            const res = await getScholarWorks(scholarKey, { limit: PAGE_SIZE });
            if (cancelled) return;
            setItems(res.items);
            setCursor(res.next_cursor);
            setTotal(res.total ?? null);
            setDone(res.next_cursor === null);
            setError(null);
          } catch (e) {
            if (cancelled) return;
            setError(e instanceof Error ? e.message : String(e));
          }
        }
      } catch {
        if (cancelled) return;
        pollTimer = window.setTimeout(tick, 2000);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      if (pollTimer !== null) window.clearTimeout(pollTimer);
    };
  }, [syncStatus, scholarKey]);

  async function handleRefresh() {
    if (worksJob && worksJob.status !== "done" && worksJob.status !== "failed") {
      return;
    }
    setError(null);
    try {
      const first = await refreshScholarWorks(scholarKey);
      setWorksJob(first);
      setSyncStatus("loading");
      const timer = window.setInterval(async () => {
        try {
          const next = await getJob(first.id);
          setWorksJob(next);
          if (next.status === "done" || next.status === "failed") {
            window.clearInterval(timer);
            if (next.status === "done") {
              const res = await getScholarWorks(scholarKey, { limit: PAGE_SIZE });
              setItems(res.items);
              setCursor(res.next_cursor);
              setTotal(res.total ?? null);
              setDone(res.next_cursor === null);
              setSyncStatus(res.status);
            } else {
              setError(next.message || "Refresh failed.");
              setSyncStatus("failed");
            }
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

  // Drain the remaining pages in one go. Used by the "Load all" button. Each
  // call updates ``items`` + ``cursor`` incrementally so the user sees a
  // growing list rather than waiting for every page to come back at once.
  const loadAll = useCallback(async () => {
    if (done || loadingAll || loading) return;
    setLoadingAll(true);
    setError(null);
    let nextCursor = cursor;
    // Local accumulator merged into state at the end so we don't fight React
    // batching on each page. We still set items on each step so a long
    // chain produces a smooth scroll-in rather than a blank → final jump.
    let accumulated = items;
    let stop = false;
    try {
      while (nextCursor && !stop) {
        const res = await getScholarWorks(scholarKey, {
          cursor: nextCursor,
          limit: PAGE_SIZE,
        });
        accumulated = [...accumulated, ...res.items];
        setItems(accumulated);
        setTotal(res.total ?? total);
        nextCursor = res.next_cursor;
        if (!res.next_cursor) {
          stop = true;
        }
      }
      setCursor(null);
      setDone(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      // Stash whatever we have so the user can still see partial results.
      setItems(accumulated);
      setCursor(nextCursor);
    } finally {
      setLoadingAll(false);
    }
  }, [cursor, done, loading, loadingAll, items, scholarKey, total]);

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
      <div className="space-y-2">
        <p className="text-sm text-muted-foreground">Loading published works…</p>
        {syncStatus === "loading" && (
          <p className="text-xs text-muted-foreground">
            First-time fetch from OpenAlex in progress — this can take a few
            seconds for authors with many works.
          </p>
        )}
      </div>
    );
  }

  if (syncStatus === "loading" && items.length === 0) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-muted-foreground">
          Fetching works from OpenAlex…
        </p>
        {worksJob?.message && (
          <p className="text-xs text-muted-foreground">{worksJob.message}</p>
        )}
      </div>
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
      <div className="space-y-2">
        <p className="rounded border border-dashed border-border/70 px-3 py-4 text-center text-xs text-muted-foreground">
          No published works found on OpenAlex.
        </p>
        <div className="flex justify-end">
          <Button
            variant="outline"
            size="sm"
            onClick={handleRefresh}
            disabled={
              !!worksJob && worksJob.status !== "done" && worksJob.status !== "failed"
            }
          >
            <RefreshCw
              className={`mr-2 h-3.5 w-3.5 ${
                worksJob && worksJob.status !== "done" && worksJob.status !== "failed"
                  ? "animate-spin"
                  : ""
              }`}
            />
            Refresh from OpenAlex
          </Button>
        </div>
      </div>
    );
  }

  const remaining = total != null ? Math.max(total - items.length, 0) : null;
  const counter =
    total != null
      ? `Showing ${items.length} of ${total}`
      : `Showing ${items.length}`;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted-foreground">{counter}</p>
        <Button
          variant="outline"
          size="sm"
          onClick={handleRefresh}
          disabled={
            !!worksJob && worksJob.status !== "done" && worksJob.status !== "failed"
          }
        >
          <RefreshCw
            className={`mr-2 h-3.5 w-3.5 ${
              worksJob && worksJob.status !== "done" && worksJob.status !== "failed"
                ? "animate-spin"
                : ""
            }`}
          />
          Refresh from OpenAlex
        </Button>
      </div>
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
        <div className="flex flex-col items-center gap-1 pt-1">
          <Button
            variant="outline"
            size="sm"
            onClick={loadAll}
            disabled={loadingAll}
          >
            {loadingAll
              ? `Loading more… ${items.length}${
                  total != null ? ` of ${total}` : ""
                }`
              : remaining != null && remaining > 0
                ? `Load all ${remaining} more`
                : "Load more"}
          </Button>
          {loadingAll && total != null && (
            <p className="text-xs text-muted-foreground">
              {items.length} of {total} loaded…
            </p>
          )}
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
