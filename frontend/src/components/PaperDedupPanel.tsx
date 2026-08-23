import { useCallback, useEffect, useRef, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  deletePaperAlias,
  getPaperDedupSnapshot,
  listJobs,
  mergePaper,
  rejectPaperPair,
  runPaperDedup,
  type PaperDedupAlias,
  type PaperDedupSnapshot,
  type PaperDedupSuggestion,
} from "@/api/client";

export default function PaperDedupPanel() {
  const [snapshot, setSnapshot] = useState<PaperDedupSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [result, setResult] = useState<{
    auto_merged: number;
    suggested: number;
    skipped_rejected: number;
  } | null>(null);
  const pollRef = useRef<number | null>(null);

  const loadSnapshot = useCallback(async () => {
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => ctrl.abort(), 20000);
    try {
      const snap = await getPaperDedupSnapshot(ctrl.signal);
      setSnapshot(snap);
      setErr(null);
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        setErr("Loading duplicates timed out — the server may be rescoring. Try again in a moment.");
      } else {
        setErr(e instanceof Error ? e.message : String(e));
      }
    } finally {
      window.clearTimeout(timer);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSnapshot();
  }, [loadSnapshot]);

  // On mount, check whether a paper_dedup Job is queued/running so navigating
  // away mid-scan doesn't strand the UI in an idle state.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const jobs = await listJobs({ kind: "paper_dedup", limit: 5 });
        if (cancelled) return;
        if (jobs.some((j) => ["queued", "running"].includes(j.status))) {
          setScanning(true);
        } else {
          const last = jobs.find((j) => j.status === "done");
          if (last?.stats) {
            const s = last.stats as {
              auto_merged?: number;
              suggested?: number;
              skipped_rejected?: number;
            };
            if (s.auto_merged != null) {
              setResult({
                auto_merged: s.auto_merged,
                suggested: s.suggested ?? 0,
                skipped_rejected: s.skipped_rejected ?? 0,
              });
            }
          }
        }
      } catch {
        // best-effort
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Poll while a scan is running so the banner updates when the job lands.
  useEffect(() => {
    if (!scanning) {
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }
    const tick = async () => {
      try {
        const jobs = await listJobs({ kind: "paper_dedup", limit: 5 });
        const done = jobs.find((j) => j.status === "done");
        if (done?.stats) {
          const s = done.stats as {
            auto_merged?: number;
            suggested?: number;
            skipped_rejected?: number;
          };
          setResult({
            auto_merged: s.auto_merged ?? 0,
            suggested: s.suggested ?? 0,
            skipped_rejected: s.skipped_rejected ?? 0,
          });
          setScanning(false);
          await loadSnapshot();
        } else if (jobs.some((j) => j.status === "failed")) {
          setScanning(false);
        }
      } catch {
        // transient poll failure; keep polling
      }
    };
    tick();
    pollRef.current = window.setInterval(tick, 2000);
    return () => {
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [scanning, loadSnapshot]);

  async function scan() {
    setScanning(true);
    setResult(null);
    try {
      await runPaperDedup({ autoApply: true, background: true });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setScanning(false);
    }
  }

  async function acceptSuggestion(s: PaperDedupSuggestion) {
    // The pair is unordered. Pick the side with the longer title as the
    // canonical — same heuristic the auto-merge path uses for richer records.
    const [aliasId, canonicalId] =
      (s.title_a || "").length >= (s.title_b || "").length
        ? [s.b, s.a]
        : [s.a, s.b];
    const key = `merge-${s.a}-${s.b}`;
    setBusyKey(key);
    try {
      await mergePaper({
        alias_paper_id: aliasId,
        canonical_paper_id: canonicalId,
        display_label: s.title_a || s.title_b || null,
      });
      await loadSnapshot();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  async function rejectSuggestion(s: PaperDedupSuggestion) {
    const key = `reject-${s.a}-${s.b}`;
    setBusyKey(key);
    try {
      await rejectPaperPair({
        a: s.a,
        b: s.b,
        display_label: s.title_a || s.title_b || null,
      });
      await loadSnapshot();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  async function removeAlias(row: PaperDedupAlias) {
    const key = `del-${row.alias_paper_id}-${row.canonical_paper_id}`;
    setBusyKey(key);
    try {
      await deletePaperAlias(row.alias_paper_id, row.canonical_paper_id);
      await loadSnapshot();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyKey(null);
    }
  }

  const suggestionCount = snapshot?.suggestions.length ?? 0;
  const appliedCount = snapshot?.applied.length ?? 0;

  return (
    <Card>
      <CardContent className="space-y-4 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Duplicate papers</h2>
            <p className="text-xs text-muted-foreground">
              The same paper can land in the library under multiple ids (DOI,
              arXiv, s2, journal-doi bridge). High-confidence matches auto-merge;
              borderline pairs land here for review.
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={loadSnapshot}
              disabled={loading}
            >
              Refresh
            </Button>
            <Button size="sm" onClick={scan} disabled={scanning}>
              {scanning ? "Scanning…" : "Scan duplicates"}
            </Button>
          </div>
        </div>

        {err && (
          <p className="rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-400">
            {err}
          </p>
        )}

        {result && !scanning && (
          <div
            role="status"
            className="flex flex-wrap items-center justify-between gap-2 rounded border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-800 dark:text-emerald-300"
          >
            <span>
              <strong>Scan complete.</strong>{" "}
              {result.auto_merged > 0
                ? `Auto-merged ${result.auto_merged} duplicate paper${result.auto_merged === 1 ? "" : "s"}.`
                : "No high-confidence duplicates to auto-merge."}{" "}
              {result.suggested > 0
                ? `${result.suggested} pair${result.suggested === 1 ? "" : "s"} need your review below.`
                : "No pairs need review."}
              {result.skipped_rejected > 0 &&
                ` (${result.skipped_rejected} previously rejected.)`}
            </span>
            <button
              type="button"
              onClick={() => setResult(null)}
              className="text-emerald-700/70 hover:text-emerald-700 dark:text-emerald-400/70 dark:hover:text-emerald-300"
              aria-label="Dismiss"
            >
              ×
            </button>
          </div>
        )}

        {snapshot?.components && snapshot.components.length > 0 && (
          <section>
            <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
              Auto-merged components ({snapshot.components.length})
            </h3>
            <ul className="space-y-1.5">
              {snapshot.components.map((c) => (
                <li
                  key={c.canonical_id}
                  className="rounded border border-border/60 bg-muted/30 px-3 py-1.5 text-xs"
                >
                  <strong className="break-all">{c.display_label || c.canonical_id}</strong>{" "}
                  <code className="ml-1 text-[11px]">{c.canonical_id}</code>
                  {c.alias_ids.length > 0 && (
                    <span className="ml-1 text-muted-foreground">
                      ← {c.alias_ids.join(", ")}
                    </span>
                  )}
                  {c.reasons.length > 0 && (
                    <p className="mt-0.5 text-muted-foreground">
                      {c.reasons.join(" · ")}
                      {c.avg_score > 0 && (
                        <> · score {Math.round(c.avg_score * 100)}%</>
                      )}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        {snapshot?.applied && snapshot.applied.length > 0 && (
          <section>
            <h3 className="mb-2 text-sm font-semibold text-muted-foreground">
              Applied merges ({snapshot.applied.length})
            </h3>
            <ul className="space-y-1.5">
              {snapshot.applied.map((row) => (
                <li
                  key={`${row.alias_paper_id}-${row.canonical_paper_id}`}
                  className="flex flex-wrap items-center justify-between gap-2 rounded border border-border/60 bg-muted/30 px-3 py-1.5 text-xs"
                >
                  <span className="break-all">
                    <strong>{row.display_label || row.alias_paper_id}</strong>
                    {" — "}
                    <code>{row.alias_paper_id}</code>
                    {" → "}
                    <code>{row.canonical_paper_id}</code>
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
                    {row.source === "llm" && (
                      <span className="ml-1 rounded bg-purple-500/15 px-1.5 py-0.5 text-purple-700 dark:text-purple-400">
                        llm {Math.round(row.confidence * 100)}%
                      </span>
                    )}
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="h-7 text-xs"
                    onClick={() => removeAlias(row)}
                    disabled={
                      busyKey === `del-${row.alias_paper_id}-${row.canonical_paper_id}`
                    }
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
          {loading && !snapshot && (
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
                      <PairLine
                        label="A"
                        pid={s.a}
                        title={s.title_a}
                        year={s.year_a}
                        doi={s.doi_a}
                        arxiv={s.arxiv_id_a}
                        s2={s.s2_paper_id_a}
                      />
                      <PairLine
                        label="B"
                        pid={s.b}
                        title={s.title_b}
                        year={s.year_b}
                        doi={s.doi_b}
                        arxiv={s.arxiv_id_b}
                        s2={s.s2_paper_id_b}
                      />
                      {s.reasons.length > 0 && (
                        <p className="mt-1 text-muted-foreground">
                          {s.reasons.join(" · ")}
                        </p>
                      )}
                      <p className="mt-0.5 text-muted-foreground">
                        score {Math.round(s.score * 100)}% · title{" "}
                        {Math.round(s.title * 100)}% · authors{" "}
                        {Math.round(s.authors * 100)}%
                        {s.strong_anchors.length > 0 && (
                          <> · anchors: {s.strong_anchors.join(", ")}</>
                        )}
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
                        Different papers
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
                <li key={`${r.alias_paper_id}-${r.canonical_paper_id}`}>
                  <code>{r.alias_paper_id}</code> ≠{" "}
                  <code>{r.canonical_paper_id}</code>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="ml-2 h-6 text-xs"
                    onClick={() => removeAlias(r)}
                    disabled={
                      busyKey === `del-${r.alias_paper_id}-${r.canonical_paper_id}`
                    }
                  >
                    Clear
                  </Button>
                </li>
              ))}
            </ul>
          </details>
        )}

        {appliedCount === 0 && suggestionCount === 0 && !loading && (
          <p className="text-xs text-muted-foreground">
            {snapshot?.components?.length
              ? "All duplicates resolved via auto-merge."
              : "No duplicates to review."}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function PairLine({
  label,
  pid,
  title,
  year,
  doi,
  arxiv,
  s2,
}: {
  label: string;
  pid: string;
  title: string | null;
  year: number | null;
  doi: string | null;
  arxiv: string | null;
  s2: string | null;
}) {
  return (
    <div className="mt-0.5 flex flex-wrap items-baseline gap-x-2">
      <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px]">
        {label}
      </span>
      <span className="font-medium">{title || "(untitled)"}</span>
      {year != null && <span className="text-muted-foreground">{year}</span>}
      <code className="text-[11px] text-muted-foreground">{pid}</code>
      {(doi || arxiv || s2) && (
        <span className="text-muted-foreground">
          {doi && <span className="mr-2">DOI: {doi}</span>}
          {arxiv && <span className="mr-2">arXiv: {arxiv}</span>}
          {s2 && <span>s2: {s2}</span>}
        </span>
      )}
    </div>
  );
}
