import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  backfillAuthors,
  deleteScholarAlias,
  getDedupSnapshot,
  listJobs,
  listScholars,
  mergeScholar,
  rejectScholarPair,
  runDedup,
  type DedupAlias,
  type DedupSnapshot,
  type DedupSuggestion,
  type ScholarSummary,
} from "@/api/client";
import { topicColorClass } from "@/lib/topicColor";

/** Build a /scholars route key from an author record. */
export function scholarKey(openalexAuthorId: string, name: string): string {
  const id = (openalexAuthorId || "").trim();
  if (id) return id;
  return `name:${name.trim()}`;
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export default function Scholars() {
  const [scholars, setScholars] = useState<ScholarSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [backfillRunning, setBackfillRunning] = useState(false);
  const [dedupRunning, setDedupRunning] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [showDedup, setShowDedup] = useState(false);
  const [snapshot, setSnapshot] = useState<DedupSnapshot | null>(null);
  const [dedupLoading, setDedupLoading] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  // Banner shown after a scan finishes — survives navigating away and back
  // because it is seeded from the last completed Job row on mount.
  const [dedupResult, setDedupResult] = useState<{
    auto_merged: number;
    suggested: number;
    skipped_rejected: number;
  } | null>(null);
  // Same idea for backfill: the batch is N per-paper jobs, and a
  // summary helps the user tell apart "ran but OpenAlex had no record
  // for these" from "actually filled the gaps".
  const [backfillResult, setBackfillResult] = useState<{
    total: number;
    filled: number;
    skipped: number;
    failed: number;
    throttled: number;
    pending: number;
  } | null>(null);
  const pollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const rows = await listScholars();
      setScholars(rows);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadSnapshot = useCallback(async () => {
    setDedupLoading(true);
    // The snapshot is served from cache and should be instant; cap the wait so
    // a request that was orphaned by a server reload can't spin forever.
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => ctrl.abort(), 20000);
    try {
      const snap = await getDedupSnapshot(ctrl.signal);
      setSnapshot(snap);
      setError(null);
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        setError("Loading duplicates timed out — the server may be rescoring. Try again in a moment.");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      window.clearTimeout(timer);
      setDedupLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (showDedup && !snapshot) loadSnapshot();
  }, [showDedup, snapshot, loadSnapshot]);

  // On mount — and whenever the panel reopens — ask the server whether a dedup
  // or backfill Job is already queued/running, so navigating away mid-scan
  // doesn't strand the UI in an idle state. Also seed the result banner from
  // the last completed scan.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [dedupJobs, backfillJobs] = await Promise.all([
          listJobs({ kind: "scholar_dedup", limit: 5 }),
          listJobs({ kind: "authors_backfill", limit: 5 }),
        ]);
        if (cancelled) return;
        if (dedupJobs.some((j) => ["queued", "running"].includes(j.status))) {
          setDedupRunning(true);
        } else {
          const last = dedupJobs.find((j) => j.status === "done");
          const stats = (last?.stats ?? {}) as {
            auto_merged?: number;
            suggested?: number;
            skipped_rejected?: number;
          };
          // Only resurrect the banner for a scan that finished recently; an old
          // job row would report stale counts (the suggestion cache doesn't
          // survive a server restart).
          const recent =
            last?.finished_at &&
            Date.now() - new Date(last.finished_at).getTime() < 10 * 60 * 1000;
          if (
            recent &&
            (stats.auto_merged !== undefined ||
              stats.suggested !== undefined)
          ) {
            setDedupResult({
              auto_merged: stats.auto_merged ?? 0,
              suggested: stats.suggested ?? 0,
              skipped_rejected: stats.skipped_rejected ?? 0,
            });
          }
        }
        if (
          backfillJobs.some((j) => ["queued", "running"].includes(j.status))
        ) {
          setBackfillRunning(true);
        } else {
          // Resurrect the most recent backfill summary so navigating
          // away and back doesn't lose it. The window matches the
          // dedup one: 10 minutes — old Job rows report stale counts.
          const last = backfillJobs.find((j) => j.status === "done");
          const recent =
            last?.finished_at &&
            Date.now() - new Date(last.finished_at).getTime() < 10 * 60 * 1000;
          if (recent) {
            const summary = backfillJobs.reduce(
              (acc, j) => {
                if (j.status === "failed") {
                  const result = (j.stats as { result?: string } | null)?.result;
                  if (result === "throttled") acc.throttled += 1;
                  else acc.failed += 1;
                } else if (j.status === "done") {
                  const result = (j.stats as { result?: string } | null)
                    ?.result;
                  if (result === "filled") acc.filled += 1;
                  else if (result === "throttled") acc.throttled += 1;
                  else if (result === "failed") acc.failed += 1;
                  else acc.skipped += 1;
                }
                return acc;
              },
              { total: backfillJobs.length, filled: 0, skipped: 0, failed: 0, throttled: 0, pending: 0 },
            );
            setBackfillResult(summary);
          }
        }
      } catch {
        // Non-fatal: the user can still click scan manually.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Poll jobs while either batch is running. Both kinds are polled together so
  // a dedup started from another tab or before navigation is tracked here.
  const anyRunning = backfillRunning || dedupRunning;
  useEffect(() => {
    if (!anyRunning) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const [dedupJobs, backfillJobs] = await Promise.all([
          listJobs({ kind: "scholar_dedup", limit: 5 }),
          // Backfill creates one Job per paper (up to `limit`), so 5 is
          // too small — bump to cover the full batch we typically queue.
          listJobs({ kind: "authors_backfill", limit: 500 }),
        ]);
        if (cancelled) return;

        const dedupActive = dedupJobs.filter((j) =>
          ["queued", "running"].includes(j.status),
        );
        const backfillActive = backfillJobs.filter((j) =>
          ["queued", "running"].includes(j.status),
        );

        const parts: string[] = [];
        if (dedupActive.length > 0) {
          const detail =
            (dedupJobs[0]?.stats as { detail?: string } | null)?.detail ??
            null;
          parts.push(
            `Scanning duplicates${detail ? ` — ${detail}` : ""}`,
          );
        } else if (dedupRunning) {
          parts.push("Finishing dedup…");
        }
        if (backfillActive.length > 0 || backfillRunning) {
          // Aggregate by status + by per-job result (filled/skipped/failed)
          // so the user sees real progress, not just a per-paper "looking
          // up" hint that doesn't change visibly.
          const stats = backfillJobs.reduce(
            (acc, j) => {
              if (j.status === "queued" || j.status === "running") {
                acc.pending += 1;
                if (j.status === "running") acc.running += 1;
              } else if (j.status === "failed") {
                // Throttled jobs live in status='failed' (see _abort_remaining
                // in the backend); we have to peek at stats.result to
                // distinguish them from real lookup failures.
                const result = (j.stats as { result?: string } | null)?.result;
                if (result === "throttled") acc.throttled += 1;
                else acc.failed += 1;
              } else if (j.status === "done") {
                const result = (j.stats as { result?: string } | null)
                  ?.result;
                if (result === "filled") acc.filled += 1;
                else if (result === "skipped") acc.skipped += 1;
                else if (result === "throttled") acc.throttled += 1;
                else if (result === "failed") acc.failed += 1;
                else acc.done += 1;
              }
              return acc;
            },
            { filled: 0, skipped: 0, failed: 0, throttled: 0, done: 0, running: 0, pending: 0 },
          );
          const total = backfillJobs.length;
          const finished = total - stats.pending;
          const head = `Resolving authors — ${finished}/${total} done`;
          const counts: string[] = [];
          if (stats.filled) counts.push(`${stats.filled} filled`);
          if (stats.skipped) counts.push(`${stats.skipped} skipped`);
          if (stats.failed) counts.push(`${stats.failed} failed`);
          if (stats.throttled)
            counts.push(`${stats.throttled} throttled (rate-limited)`);
          const tail = counts.length ? ` (${counts.join(", ")})` : "";
          const runningDetail =
            stats.running > 0
              ? (backfillJobs.find((j) => j.status === "running")?.stats as
                  | { detail?: string }
                  | null)?.detail
              : null;
          const suffix = runningDetail ? ` · current: ${runningDetail}` : "";
          parts.push(`${head}${tail}${suffix}`);
        }
        setProgress(parts.length ? parts.join(" · ") : null);

        // Detect transition to done for each kind independently.
        if (dedupRunning && dedupActive.length === 0) {
          setDedupRunning(false);
          const last = dedupJobs.find((j) => j.status === "done");
          const failed = dedupJobs.find((j) => j.status === "failed");
          if (failed) {
            setError(`Dedup failed: ${failed.message || "unknown error"}`);
          } else if (last) {
            const stats = (last.stats ?? {}) as {
              auto_merged?: number;
              suggested?: number;
              skipped_rejected?: number;
            };
            setDedupResult({
              auto_merged: stats.auto_merged ?? 0,
              suggested: stats.suggested ?? 0,
              skipped_rejected: stats.skipped_rejected ?? 0,
            });
          }
          if (showDedup) loadSnapshot();
        }
        if (backfillRunning && backfillActive.length === 0) {
          setBackfillRunning(false);
          // Capture the final per-paper outcome so the user can tell a
          // "300 looked up, 12 filled, 288 OA-had-no-record" run from a
          // successful one. Cleared on the next click of Resolve authors.
          const summary = backfillJobs.reduce(
            (acc, j) => {
              if (j.status === "queued" || j.status === "running") {
                acc.pending += 1;
              } else if (j.status === "failed") {
                // _abort_remaining writes status='failed' + result='throttled'
                // together; without peeking at the result stat, the
                // 500 throttled jobs from a rate-limited batch would
                // all show as plain failures.
                const result = (j.stats as { result?: string } | null)?.result;
                if (result === "throttled") acc.throttled += 1;
                else acc.failed += 1;
              } else if (j.status === "done") {
                const result = (j.stats as { result?: string } | null)
                  ?.result;
                if (result === "filled") acc.filled += 1;
                else if (result === "skipped") acc.skipped += 1;
                else if (result === "throttled") acc.throttled += 1;
                else if (result === "failed") acc.failed += 1;
                else acc.skipped += 1;  // legacy jobs without result
              }
              return acc;
            },
            { total: backfillJobs.length, filled: 0, skipped: 0, failed: 0, throttled: 0, pending: 0 },
          );
          setBackfillResult(summary);
        }
        if (dedupActive.length === 0 && backfillActive.length === 0) {
          setProgress(null);
          refresh();
        }
      } catch {
        // Transient poll failure; keep polling.
      }
    };
    tick();
    pollRef.current = window.setInterval(tick, 2000);
    return () => {
      cancelled = true;
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [anyRunning, dedupRunning, backfillRunning, refresh, loadSnapshot, showDedup]);

  async function resolveAll() {
    // Clear any prior summary so a new batch doesn't briefly show
    // the old fill/skip counts while the new one is still queueing.
    setBackfillResult(null);
    setBackfillRunning(true);
    setProgress("Starting…");
    try {
      await backfillAuthors({ limit: 300, background: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBackfillRunning(false);
      setProgress(null);
    }
  }

  async function runDedupNow() {
    setDedupRunning(true);
    setDedupResult(null);
    setProgress("Scanning duplicates…");
    try {
      await runDedup({ autoApply: true, background: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setDedupRunning(false);
      setProgress(null);
    }
  }

  async function acceptSuggestion(s: DedupSuggestion) {
    // The suggestion is unordered; pick the A-ID with more papers as canonical.
    const [aliasAid, canonicalAid] = (s.paper_counts[s.a] ?? 0) >=
      (s.paper_counts[s.b] ?? 0)
      ? [s.b, s.a]
      : [s.a, s.b];
    const key = `merge-${s.a}-${s.b}`;
    setBusyKey(key);
    try {
      await mergeScholar({
        alias_aid: aliasAid,
        canonical_aid: canonicalAid,
        display_name: s.display_name,
      });
      await Promise.all([loadSnapshot(), refresh()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  async function rejectSuggestion(s: DedupSuggestion) {
    const key = `reject-${s.a}-${s.b}`;
    setBusyKey(key);
    try {
      await rejectScholarPair({
        a: s.a,
        b: s.b,
        display_name: s.display_name,
      });
      await loadSnapshot();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  async function removeAlias(row: DedupAlias) {
    const key = `del-${row.alias_aid}-${row.canonical_aid}`;
    setBusyKey(key);
    try {
      await deleteScholarAlias(row.alias_aid, row.canonical_aid);
      await Promise.all([loadSnapshot(), refresh()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return scholars;
    return scholars.filter((s) => s.name.toLowerCase().includes(needle));
  }, [scholars, q]);

  const nameOnlyCount = useMemo(
    () => scholars.filter((s) => !s.has_openalex).length,
    [scholars],
  );

  const suggestionCount = snapshot?.suggestions.length ?? 0;
  const appliedCount = snapshot?.applied.length ?? 0;

  return (
    <main className="container max-w-screen-2xl space-y-4 py-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="flex items-center gap-2 text-2xl font-bold">
          Scholars
          {!loading && (
            <span
              className="rounded-full bg-muted px-2.5 py-0.5 text-sm font-normal text-muted-foreground"
              title={
                q.trim()
                  ? `${filtered.length} of ${scholars.length} match “${q.trim()}”`
                  : `${scholars.length} scholar${scholars.length === 1 ? "" : "s"}`
              }
            >
              {q.trim()
                ? `${filtered.length} / ${scholars.length}`
                : scholars.length}
            </span>
          )}
        </h1>
        <div className="flex items-center gap-3">
          <Button
            size="sm"
            variant="outline"
            onClick={() => setShowDedup((v) => !v)}
            title="Review duplicate OpenAlex author profiles"
          >
            {showDedup ? "Hide duplicates" : "Duplicates"}
            {suggestionCount > 0 && (
              <span className="ml-2 rounded-full bg-amber-500/15 px-2 py-0.5 text-xs text-amber-700 dark:text-amber-400">
                {suggestionCount}
              </span>
            )}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={runDedupNow}
            disabled={dedupRunning}
            title="Score same-named authors and auto-merge high-confidence duplicates"
          >
            {dedupRunning ? "Scanning…" : "Scan duplicates"}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={resolveAll}
            disabled={backfillRunning}
            title="Look up missing OpenAlex author IDs from each paper's DOI/arXiv ID"
          >
            {backfillRunning ? "Resolving…" : "Resolve authors"}
          </Button>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter by name…"
            className="h-9 w-64 rounded-md border border-input bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
      </div>
      <p className="text-sm text-muted-foreground">
        Authors in your library, ranked by how many of their papers you have.
        {!loading && ` ${scholars.length} scholar(s).`}
        {!loading && nameOnlyCount > 0 && (
          <> {nameOnlyCount} matched by name only — run{" "}
          <strong>Resolve authors</strong> to look up their OpenAlex IDs.</>
        )}
        {!loading && appliedCount > 0 && (
          <> {appliedCount} duplicate profile(s) merged via the dedup panel.</>
        )}
      </p>
      {progress && <p className="text-sm text-muted-foreground">{progress}</p>}
      {error && (
        <p className="rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      )}
      {dedupResult && !dedupRunning && (
        <div
          role="status"
          className="flex flex-wrap items-center justify-between gap-2 rounded border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-800 dark:text-emerald-300"
        >
          <span>
            <strong>Scan complete.</strong>{" "}
            {dedupResult.auto_merged > 0
              ? `Auto-merged ${dedupResult.auto_merged} duplicate profile${dedupResult.auto_merged === 1 ? "" : "s"}.`
              : "No high-confidence duplicates to auto-merge."}{" "}
            {dedupResult.suggested > 0
              ? `${dedupResult.suggested} pair${dedupResult.suggested === 1 ? "" : "s"} need your review below.`
              : "No pairs need review."}
            {dedupResult.skipped_rejected > 0 &&
              ` (${dedupResult.skipped_rejected} previously rejected.)`}
          </span>
          {dedupResult.suggested > 0 && !showDedup && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 text-xs"
              onClick={() => setShowDedup(true)}
            >
              Review
            </Button>
          )}
          <button
            type="button"
            onClick={() => setDedupResult(null)}
            className="text-emerald-700/70 hover:text-emerald-700 dark:text-emerald-400/70 dark:hover:text-emerald-300"
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      )}
      {backfillResult && !backfillRunning && backfillResult.throttled > 0 && (
        <div
          role="status"
          className="flex flex-wrap items-center justify-between gap-2 rounded border border-rose-500/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-800 dark:text-rose-300"
        >
          <span>
            <strong>OpenAlex is rate-limiting us.</strong>{" "}
            {backfillResult.throttled} of {backfillResult.total} paper
            {backfillResult.total === 1 ? "" : "s"} were aborted because the
            free-tier budget (1000 calls/IP-day) is exhausted. Free tier
            resets at next UTC midnight — re-run <em>Resolve authors</em>{" "}
            after that.
            {backfillResult.filled > 0 && (
              <>
                {" "}
                <strong>{backfillResult.filled}</strong> were filled before
                the cap was hit.
              </>
            )}
          </span>
          <button
            type="button"
            onClick={() => setBackfillResult(null)}
            className="text-rose-700/70 hover:text-rose-700 dark:text-rose-400/70 dark:hover:text-rose-300"
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      )}
      {backfillResult && !backfillRunning && backfillResult.throttled === 0 && (
        <div
          role="status"
          className="flex flex-wrap items-center justify-between gap-2 rounded border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-800 dark:text-sky-300"
        >
          <span>
            <strong>Resolve authors finished.</strong>{" "}
            Looked up {backfillResult.total} paper
            {backfillResult.total === 1 ? "" : "s"}.
            {backfillResult.filled > 0 && (
              <>
                {" "}
                Filled OpenAlex IDs on{" "}
                <strong>{backfillResult.filled}</strong>.
              </>
            )}
            {backfillResult.skipped > 0 && (
              <>
                {" "}
                {backfillResult.skipped} had no record in OpenAlex (skipped).
              </>
            )}
            {backfillResult.failed > 0 && (
              <>
                {" "}
                <span className="text-rose-700 dark:text-rose-400">
                  {backfillResult.failed} failed
                </span>
                .
              </>
            )}
            {backfillResult.filled === 0 &&
              backfillResult.failed === 0 && (
                <> Nothing to update.</>
              )}
          </span>
          <button
            type="button"
            onClick={() => setBackfillResult(null)}
            className="text-sky-700/70 hover:text-sky-700 dark:text-sky-400/70 dark:hover:text-sky-300"
            aria-label="Dismiss"
          >
            ×
          </button>
        </div>
      )}

      {showDedup && (
        <Card>
          <CardContent className="space-y-4 p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold">Duplicate authors</h2>
                <p className="text-xs text-muted-foreground">
                  OpenAlex often splits one person into multiple profiles.
                  Auto-merged pairs are applied silently; review the rest here.
                </p>
              </div>
              <Button
                size="sm"
                variant="outline"
                onClick={loadSnapshot}
                disabled={dedupLoading}
              >
                Refresh
              </Button>
            </div>

            {snapshot?.applied && snapshot.applied.length > 0 && (
              <section>
                <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
                  Applied merges ({snapshot.applied.length})
                </h3>
                <ul className="space-y-1.5">
                  {snapshot.applied.map((row) => (
                    <li
                      key={`${row.alias_aid}-${row.canonical_aid}`}
                      className="flex flex-wrap items-center justify-between gap-2 rounded border border-border/60 bg-muted/30 px-3 py-1.5 text-xs"
                    >
                      <span className="break-all">
                        <strong>{row.display_name || row.alias_aid}</strong>
                        {" — "}
                        <code>{row.alias_aid}</code>
                        {" → "}
                        <code>{row.canonical_aid}</code>
                        {row.source === "auto" && (
                          <span className="ml-1 rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-700 dark:text-emerald-400">
                            auto {Math.round(row.confidence * 100)}%
                          </span>
                        )}
                        {row.source === "user" && (
                          <span className="ml-1 rounded bg-blue-500/15 px-1.5 py-0.5 text-blue-700 dark:text-blue-400">
                            confirmed
                          </span>
                        )}
                      </span>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 text-xs"
                        onClick={() => removeAlias(row)}
                        disabled={busyKey === `del-${row.alias_aid}-${row.canonical_aid}`}
                      >
                        Undo
                      </Button>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section>
              <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
                Suggested reviews ({snapshot?.suggestions.length ?? 0})
              </h3>
              {dedupLoading && !snapshot && (
                <p className="text-sm text-muted-foreground">Loading…</p>
              )}
              {snapshot && snapshot.suggestions.length === 0 && (
                <p className="rounded border border-dashed border-border/70 px-3 py-4 text-center text-xs text-muted-foreground">
                  No unresolved duplicates. Run <strong>Scan duplicates</strong> to
                  re-check after importing new papers.
                </p>
              )}
              <ul className="space-y-2">
                {snapshot?.suggestions.map((s) => {
                  const k = `${s.a}-${s.b}`;
                  return (
                    <li
                      key={k}
                      className="rounded border border-border/60 px-3 py-2 text-xs"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="text-sm font-medium">
                            {s.display_name || "Unknown"}
                          </p>
                          <div className="mt-1 grid gap-x-6 gap-y-0.5 sm:grid-cols-2">
                            <AidLine aid={s.a} s={s} />
                            <AidLine aid={s.b} s={s} />
                          </div>
                          {s.reasons.length > 0 && (
                            <p className="mt-1 text-muted-foreground">
                              {s.reasons.join(" · ")}
                            </p>
                          )}
                          <p className="mt-0.5 text-muted-foreground">
                            score {Math.round(s.score * 100)}% · coauthors{" "}
                            {Math.round(s.coauthor * 100)}% · affiliation{" "}
                            {Math.round(s.affiliation * 100)}%
                          </p>
                        </div>
                        <div className="flex shrink-0 gap-2">
                          <Button
                            size="sm"
                            onClick={() => acceptSuggestion(s)}
                            disabled={busyKey === `merge-${s.a}-${s.b}`}
                          >
                            Merge
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => rejectSuggestion(s)}
                            disabled={busyKey === `reject-${s.a}-${s.b}`}
                          >
                            Different people
                          </Button>
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </section>

            {snapshot && snapshot.rejected.length > 0 && (
              <details className="text-xs text-muted-foreground">
                <summary className="cursor-pointer">
                  Rejected pairs ({snapshot.rejected.length})
                </summary>
                <ul className="mt-1 space-y-1">
                  {snapshot.rejected.map((r) => (
                    <li key={`${r.alias_aid}-${r.canonical_aid}`}>
                      <code>{r.alias_aid}</code> ≠{" "}
                      <code>{r.canonical_aid}</code>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="ml-2 h-6 text-xs"
                        onClick={() => removeAlias(r)}
                        disabled={
                          busyKey === `del-${r.alias_aid}-${r.canonical_aid}`
                        }
                      >
                        Clear
                      </Button>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </CardContent>
        </Card>
      )}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading scholars…</p>
      ) : filtered.length === 0 ? (
        <Card>
          <CardContent className="p-6 text-sm text-muted-foreground">
            No scholars found. Import some papers first.
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filtered.map((s) => (
            <Link
              key={s.key}
              to={`/scholars/${encodeURIComponent(s.key)}`}
              className="block"
            >
              <Card className="h-full transition-colors hover:bg-muted/30">
                <CardContent className="flex gap-3 p-4">
                  <div
                    className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-sm font-semibold ${topicColorClass(s.name)}`}
                  >
                    {initials(s.name)}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start justify-between gap-2">
                      <h2 className="truncate font-medium leading-snug">
                        {s.name}
                      </h2>
                      <span
                        className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-xs font-medium"
                        title="Papers in your library"
                      >
                        {s.paper_count}
                      </span>
                    </div>
                    {s.affiliation && (
                      <p
                        className="truncate text-xs text-muted-foreground"
                        title={s.affiliation}
                      >
                        {s.affiliation}
                      </p>
                    )}
                    <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                      {(s.first_year || s.last_year) && (
                        <span>
                          {s.first_year ?? "?"}–{s.last_year ?? "?"}
                        </span>
                      )}
                      {s.total_citations > 0 && (
                        <span>🏆 {s.total_citations} cites</span>
                      )}
                      {!s.has_openalex && (
                        <span className="italic">name-only</span>
                      )}
                    </div>
                  </div>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </main>
  );
}

function AidLine({ aid, s }: { aid: string; s: DedupSuggestion }) {
  const affil = s.affiliations[aid];
  const npapers = s.paper_counts[aid] ?? 0;
  return (
    <div className="min-w-0">
      <code className="text-[11px]">{aid}</code>
      <span className="ml-2 text-muted-foreground">{npapers} paper(s)</span>
      {affil && (
        <p className="truncate text-muted-foreground" title={affil}>
          {affil}
        </p>
      )}
    </div>
  );
}
