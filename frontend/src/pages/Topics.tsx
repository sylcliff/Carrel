import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { TopicsLayout } from "@/components/TopicsLayout";
import {
  classifyTopics,
  listJobs,
  listTopics,
  type TopicWithCount,
} from "@/api/client";
import { topicColorClass } from "@/lib/topicColor";
import { cn } from "@/lib/utils";

export default function Topics() {
  const [topics, setTopics] = useState<TopicWithCount[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const t = await listTopics();
      setTopics(t);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // When a classification batch is running, poll the jobs endpoint for the
  // most recent "topics" job and surface its detail; refresh the topic list
  // once everything settles.
  useEffect(() => {
    if (!running) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const jobs = await listJobs({ kind: "topics", limit: 20 });
        if (cancelled) return;
        const active = jobs.filter((j) =>
          ["queued", "running"].includes(j.status),
        );
        const latest = jobs[0];
        const detail =
          (latest?.stats as { detail?: string } | null)?.detail ?? null;
        setProgress(
          active.length > 0
            ? `${active.length} in progress${detail ? ` — ${detail}` : ""}`
            : "Finishing…",
        );
        if (active.length === 0 && latest) {
          setRunning(false);
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
  }, [running, refresh]);

  async function classifyAll() {
    setRunning(true);
    setProgress("Starting…");
    try {
      await classifyTopics({ limit: 200, background: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRunning(false);
      setProgress(null);
    }
  }

  return (
    <TopicsLayout
      sidebarTop={
        <Button
          size="sm"
          className="w-full"
          onClick={classifyAll}
          disabled={running}
        >
          {running ? "Classifying…" : "Classify library"}
        </Button>
      }
    >
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold">Topics</h1>
        {progress && (
          <span className="text-sm text-muted-foreground">{progress}</span>
        )}
      </div>

      {error && <p className="mb-4 text-sm text-red-600">{error}</p>}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading topics…</p>
      ) : topics.length === 0 ? (
        <Card>
          <CardContent className="p-6 text-sm text-muted-foreground">
            No topics yet. Click <strong>Classify library</strong> to assign
            research themes to your papers with the LLM.
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {topics.map((t) => (
            <Card key={t.id} className="flex flex-col">
              <CardHeader className="pb-2">
                <div className="flex items-start justify-between gap-2">
                  <CardTitle className="text-base leading-snug">
                    <Link
                      to={`/library?topic=${encodeURIComponent(t.name)}`}
                      className="hover:underline"
                    >
                      {t.name}
                    </Link>
                  </CardTitle>
                  <span
                    className={cn(
                      "shrink-0 rounded-full px-2 py-0.5 text-xs font-medium",
                      topicColorClass(t.name),
                    )}
                  >
                    {t.paper_count}
                  </span>
                </div>
              </CardHeader>
              <CardContent className="flex-1 pt-0">
                {t.description && (
                  <p className="text-xs text-muted-foreground">
                    {t.description}
                  </p>
                )}
                <Link
                  to={`/library?topic=${encodeURIComponent(t.name)}`}
                  className="mt-3 inline-block text-xs text-primary hover:underline"
                >
                  View papers →
                </Link>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </TopicsLayout>
  );
}
