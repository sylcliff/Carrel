import { useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  getSchedule,
  runScheduledJob,
  updateSchedule,
  type ScheduledJob,
  type SchedulerStatus,
} from "@/api/client";
import { humanizeCron } from "@/lib/cron";

function fmtLocal(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString();
}

function lastRunDuration(start: string | null, end: string | null): string {
  if (!start) return "—";
  const endTime = end ? new Date(end) : new Date();
  const ms = endTime.getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m${s % 60}s` : `${Math.floor(m / 60)}h${m % 60}m`;
}

function statusDot(status: string | null): string {
  if (status === "done") return "bg-green-500";
  if (status === "failed") return "bg-red-500";
  if (status === "running" || status === "queued") return "bg-yellow-400 animate-pulse";
  return "bg-muted-foreground/30";
}

// Map a backend job id to the two YAML fields that control it.
const FIELD_MAP: Record<string, { enabled: string; cron: string }> = {
  daily_sync: { enabled: "enabled", cron: "sync_cron" },
  remote_fill: { enabled: "remote_fill_enabled", cron: "remote_fill_cron" },
  publication_check: {
    enabled: "publication_check_enabled",
    cron: "publication_check_cron",
  },
};

// Per-job summary builder — turns the Job.stats dict written by each pipeline
// into a compact "fetched X, new Y, failed Z" line for the Last run column.
function summarizeStats(job: ScheduledJob): string[] {
  const s = (job.last_stats ?? {}) as Record<string, unknown>;
  const out: string[] = [];
  const num = (k: string) => (typeof s[k] === "number" ? s[k] as number : null);

  // Single-paper runs (POST /process, /publication/check, ...) put a human
  // stage/detail in stats rather than batch counts. Show that if no batch
  // counters are present.
  const singleDetail =
    typeof s.detail === "string"
      ? s.detail
      : typeof s.stage === "string"
        ? s.stage
        : null;

  switch (job.id) {
    case "daily_sync": {
      const fetched = num("fetched");
      const found = num("new_discovered") ?? num("new");
      const updated = num("updated");
      const skipped = num("skipped");
      const enriched = num("citations_enriched");
      const citeFailed = num("citations_failed");
      const refsBackfilled = num("references_backfilled");
      const citeRefresh = num("citations_refresh_candidates");
      if (fetched !== null) {
        out.push(`fetched ${fetched}`);
        if (found !== null) out.push(`new ${found}`);
        if (updated !== null) out.push(`updated ${updated}`);
        if (skipped !== null && skipped > 0) out.push(`skipped ${skipped}`);
        if (
          (refsBackfilled !== null && refsBackfilled > 0) ||
          (citeRefresh !== null && citeRefresh > 0) ||
          (enriched !== null && enriched > 0)
        ) {
          out.push(
            `citations enriched ${enriched ?? 0}` +
              (refsBackfilled !== null && refsBackfilled > 0
                ? ` (refs backfill ${refsBackfilled})`
                : "") +
              (citeRefresh !== null && citeRefresh > 0
                ? ` (stale refresh ${citeRefresh})`
                : ""),
          );
        }
        if (citeFailed !== null && citeFailed > 0) out.push(`citations failed ${citeFailed}`);
      } else if (singleDetail) {
        out.push(singleDetail);
      }
      break;
    }
    case "remote_fill": {
      const candidates = num("candidates");
      const parsed = num("parsed");
      const failed = num("failed");
      if (candidates !== null) {
        out.push(`candidates ${candidates}`);
        if (parsed !== null) out.push(`downloaded/parsed ${parsed}`);
        if (failed !== null && failed > 0) out.push(`failed ${failed}`);
      } else if (typeof s.reason === "string") {
        out.push(s.reason);
      } else if (singleDetail) {
        out.push(singleDetail);
      }
      break;
    }
    case "publication_check": {
      const candidates = num("candidates");
      const found = num("found");
      const failed = num("failed");
      if (candidates !== null) {
        out.push(`checked ${candidates}`);
        if (found !== null && found > 0) out.push(`published found ${found}`);
        if (failed !== null && failed > 0) out.push(`failed ${failed}`);
      } else if (singleDetail) {
        out.push(singleDetail);
      }
      break;
    }
  }
  return out;
}

export default function ScheduledJobsCard() {
  const [status, setStatus] = useState<SchedulerStatus | null>(null);
  // Only the cron text is a local draft — the enable checkbox flips the
  // server-side value immediately (no separate Save needed).
  const [cronDrafts, setCronDrafts] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [savingId, setSavingId] = useState<string | null>(null);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const s = await getSchedule();
      setStatus(s);
      setCronDrafts(
        Object.fromEntries(s.jobs.map((j) => [j.id, j.cron])),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // Poll next_run_at periodically. The list of jobs and their last-run
    // state rarely changes, so a 30s tick is plenty. The page-level
    // /sync/jobs polling on the parent picks up new runs faster.
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);

  function isCronDirty(job: ScheduledJob): boolean {
    const d = cronDrafts[job.id];
    if (d === undefined) return false;
    return d.trim() !== job.cron.trim();
  }

  async function toggleEnabled(job: ScheduledJob, next: boolean) {
    const fields = FIELD_MAP[job.id];
    if (!fields) return;
    setTogglingId(job.id);
    setError(null);
    try {
      const s = await updateSchedule({ [fields.enabled]: next });
      setStatus(s);
      // Keep the cron draft in sync with the server value (it shouldn't
      // change from a toggle, but stay defensive).
      setCronDrafts(Object.fromEntries(s.jobs.map((j) => [j.id, j.cron])));
    } catch (e) {
      setError(String(e));
    } finally {
      setTogglingId(null);
    }
  }

  async function saveCron(job: ScheduledJob) {
    const fields = FIELD_MAP[job.id];
    const draft = cronDrafts[job.id];
    if (!fields || draft === undefined) return;
    const cron = draft.trim();
    if (!cron) {
      setError("Cron expression cannot be empty");
      return;
    }
    setSavingId(job.id);
    setError(null);
    try {
      const s = await updateSchedule({ [fields.cron]: cron });
      setStatus(s);
      setCronDrafts(Object.fromEntries(s.jobs.map((j) => [j.id, j.cron])));
    } catch (e) {
      setError(String(e));
    } finally {
      setSavingId(null);
    }
  }

  async function runNow(job: ScheduledJob) {
    if (runningId) return;
    setRunningId(job.id);
    setError(null);
    try {
      await runScheduledJob(job.id);
      // The job body runs in a background thread on the server and writes a
      // new Job row; refresh immediately so the Last run cell flips to
      // "running", then keep polling faster while it's active. The parent
      // SyncStatus page already polls /sync/jobs every 2s during activity.
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setRunningId(null);
    }
  }

  const jobs = status?.jobs ?? [];

  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex items-center justify-between gap-4 border-b px-4 py-3">
          <div>
            <h2 className="text-base font-semibold">Scheduled jobs</h2>
            <p className="text-xs text-muted-foreground">
              Cron-managed background tasks. Toggling a switch takes effect
              immediately; editing the cron string requires Save. Changes
              write to <code className="font-mono">data/config.yaml</code>.
            </p>
          </div>
          <span
            className={`text-xs px-2 py-1 rounded-md ${
              status?.enabled
                ? "bg-green-100 text-green-700"
                : "bg-muted text-muted-foreground"
            }`}
          >
            {status?.enabled ? "scheduler running" : "scheduler idle"}
          </span>
        </div>

        {error && (
          <div className="border-b border-red-200 bg-red-50 px-4 py-2 text-xs text-red-700">
            {error}
          </div>
        )}

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-4 py-2 text-left">Task</th>
                <th className="px-4 py-2 text-left">Enabled</th>
                <th className="px-4 py-2 text-left">Cron</th>
                <th className="px-4 py-2 text-left">Next run</th>
                <th className="px-4 py-2 text-left">Last run</th>
                <th className="px-4 py-2 text-right w-44">Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && jobs.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-muted-foreground">
                    Loading…
                  </td>
                </tr>
              )}
              {jobs.map((job) => {
                const cronDraft = cronDrafts[job.id] ?? job.cron;
                const cronDirty = isCronDirty(job);
                const depMissing =
                  job.requires !== null && !job.requirement_satisfied;
                const busy = togglingId === job.id;
                const stats = summarizeStats(job);
                return (
                  <tr key={job.id} className="border-b last:border-0 align-top">
                    <td className="px-4 py-3 w-1/4">
                      <div className="font-medium">{job.label}</div>
                      <div className="font-mono text-[11px] text-muted-foreground">
                        {job.id}
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground leading-snug">
                        {job.description}
                      </p>
                      {depMissing && (
                        <div className="mt-1 text-[11px] text-amber-600">
                          requires{" "}
                          <code className="font-mono">{job.requires}</code> —
                          not configured
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <label className="inline-flex items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={job.enabled}
                          disabled={busy || togglingId !== null}
                          onChange={(e) => toggleEnabled(job, e.target.checked)}
                          className="h-4 w-4"
                        />
                      </label>
                    </td>
                    <td className="px-4 py-3">
                      <input
                        type="text"
                        value={cronDraft}
                        spellCheck={false}
                        onChange={(e) =>
                          setCronDrafts((d) => ({
                            ...d,
                            [job.id]: e.target.value,
                          }))
                        }
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && cronDirty) saveCron(job);
                        }}
                        className="w-40 rounded-md border bg-background px-2 py-1 font-mono text-xs"
                        placeholder="* * * * *"
                      />
                      {(() => {
                        const h = humanizeCron(cronDraft);
                        if (!h) return null;
                        return (
                          <div
                            className={`mt-1 text-[11px] ${
                              h === "custom schedule"
                                ? "text-amber-600"
                                : "text-muted-foreground"
                            }`}
                          >
                            {h}
                          </div>
                        );
                      })()}
                    </td>
                    <td className="px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">
                      {job.enabled && job.running
                        ? fmtLocal(job.next_run_at)
                        : "—"}
                    </td>
                    <td className="px-4 py-3 text-xs">
                      {job.last_status ? (
                        <>
                          <div className="flex items-center gap-2">
                            <span
                              className={`inline-block h-2 w-2 rounded-full ${statusDot(
                                job.last_status,
                              )}`}
                            />
                            <span className="capitalize">{job.last_status}</span>
                            <span className="text-muted-foreground">
                              · {lastRunDuration(
                                job.last_started_at,
                                job.last_finished_at,
                              )}
                            </span>
                          </div>
                          <div className="mt-0.5 text-muted-foreground">
                            {fmtLocal(job.last_finished_at ?? job.last_started_at)}
                          </div>
                          {stats.length > 0 && (
                            <div className="mt-0.5 text-muted-foreground">
                              {stats.join(" · ")}
                            </div>
                          )}
                          {job.last_status === "failed" && job.last_message && (
                            <div
                              className="mt-0.5 max-w-xs break-words text-red-600"
                              title={job.last_message}
                            >
                              {job.last_message}
                            </div>
                          )}
                        </>
                      ) : (
                        <span className="text-muted-foreground">never</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={
                            runningId === job.id ||
                            (job.requires !== null &&
                              !job.requirement_satisfied)
                          }
                          onClick={() => runNow(job)}
                          title={
                            job.requires !== null &&
                            !job.requirement_satisfied
                              ? `Requires ${job.requires} to be configured`
                              : "Run this task once now"
                          }
                        >
                          {runningId === job.id ? "Running…" : "Run now"}
                        </Button>
                        <Button
                          size="sm"
                          variant={cronDirty ? "default" : "outline"}
                          disabled={
                            !cronDirty ||
                            savingId === job.id ||
                            togglingId === job.id
                          }
                          onClick={() => saveCron(job)}
                        >
                          {savingId === job.id ? "Saving…" : "Save"}
                        </Button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="border-t px-4 py-2 text-[11px] text-muted-foreground">
          5-field crontab: <code className="font-mono">minute hour day month weekday</code>{" "}
          (server local time). Examples:{" "}
          <code className="font-mono">0 8 * * *</code> daily 08:00 ·{" "}
          <code className="font-mono">0 10 * * 1</code> Mondays 10:00 ·{" "}
          <code className="font-mono">*/30 * * * *</code> every 30 minutes.
          Press Enter in the cron field to save.
        </div>
      </CardContent>
    </Card>
  );
}
