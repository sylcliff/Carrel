import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import MarkdownReader from "@/components/MarkdownReader";
import CitationsCard from "@/components/CitationsCard";
import {
  embedPaper,
  getJob,
  getPaper,
  getPaperMarkdown,
  processPaper,
  type Job,
  type PaperDetail as PaperDetailT,
} from "@/api/client";

type Props = {
  onProcessed?: () => void;
};

const TERMINAL = new Set(["done", "failed"]);

function elapsed(startedAt: string | null, now: number): string {
  if (!startedAt) return "";
  const secs = Math.max(0, Math.floor((now - new Date(startedAt).getTime()) / 1000));
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

export default function PaperDetail({ onProcessed }: Props) {
  const { id } = useParams<{ id: string }>();
  const [p, setP] = useState<PaperDetailT | null>(null);
  const [md, setMd] = useState<string | null>(null);
  const [processing, setProcessing] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const timer = useRef<number | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    const paper = await getPaper(id);
    setP(paper);
    if (paper.md_path) {
      try {
        const r = await getPaperMarkdown(id);
        setMd(r.body);
      } catch {
        setMd(null);
      }
    } else {
      setMd(null);
    }
  }, [id]);

  useEffect(() => {
    load().catch((e) => setErr(String(e)));
  }, [load]);

  // Tick every second while processing so the elapsed counter updates.
  useEffect(() => {
    if (!processing) return;
    const id2 = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id2);
  }, [processing]);

  function watchJob(firstJob: Job) {
    setJob(firstJob);
    timer.current = window.setInterval(async () => {
      try {
        const j = await getJob(firstJob.id);
        setJob(j);
        if (TERMINAL.has(j.status)) {
          if (timer.current) window.clearInterval(timer.current);
          setProcessing(false);
          if (j.status === "failed") {
            setErr(j.message || "Job failed");
          }
          await load();
          onProcessed?.();
        }
      } catch (e) {
        if (timer.current) window.clearInterval(timer.current);
        setProcessing(false);
        setErr(String(e));
      }
    }, 1500);
  }

  async function onParse() {
    if (!id) return;
    setProcessing(true);
    setErr(null);
    setJob(null);
    try {
      const started = await processPaper(id, true);
      const firstJob = started[0];
      if (!firstJob) throw new Error("No job returned");
      watchJob(firstJob);
    } catch (e) {
      setProcessing(false);
      setErr(String(e));
    }
  }

  async function onEmbed() {
    if (!id) return;
    setProcessing(true);
    setErr(null);
    setJob(null);
    try {
      const started = await embedPaper(id, true);
      const firstJob = started[0];
      if (!firstJob) throw new Error("No job returned");
      watchJob(firstJob);
    } catch (e) {
      setProcessing(false);
      setErr(String(e));
    }
  }

  useEffect(
    () => () => {
      if (timer.current) window.clearInterval(timer.current);
    },
    []
  );

  if (err && !p)
    return <main className="container py-8 text-sm text-red-600">{err}</main>;
  if (!p)
    return (
      <main className="container py-8 text-sm text-muted-foreground">Loading…</main>
    );

  const canParse =
    Boolean(p.pdf_url) && p.status !== "parsed" && p.status !== "summarized" && p.status !== "ready";
  // Embed is available when the paper is parsed/summarized but not yet ready,
  // or when a previous embed failed.
  const canEmbed =
    p.status === "parsed" || p.status === "summarized" ||
    (p.status === "failed" && Boolean(p.md_path));
  const stage = (job?.stats?.stage as string | undefined) ?? "";
  const detail = (job?.stats?.detail as string | undefined) ?? job?.message ?? "";
  const running = Boolean(processing || (job && !TERMINAL.has(job.status)));

  return (
    <main className="container max-w-3xl space-y-6 py-8">
      <div>
        <Link to="/" className="text-sm text-muted-foreground hover:underline">
          ← Back
        </Link>
      </div>

      <header className="space-y-2">
        <h1 className="text-2xl font-bold">{p.title}</h1>
        <div className="text-sm text-muted-foreground">
          {p.authors.join(", ") || "—"}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          {p.venue && <span>📰 {p.venue}</span>}
          {p.publication_date && <span>📅 {p.publication_date}</span>}
          {p.doi && (
            <a
              href={p.doi}
              target="_blank"
              rel="noopener noreferrer"
              className="underline-offset-2 hover:text-foreground hover:underline"
            >
              DOI: {p.doi.replace(/^https?:\/\/(dx\.)?doi\.org\//i, "")}
            </a>
          )}
          {p.arxiv_id && (
            <a
              href={`https://arxiv.org/abs/${p.arxiv_id}`}
              target="_blank"
              rel="noopener noreferrer"
              className="underline-offset-2 hover:text-foreground hover:underline"
            >
              arXiv:{p.arxiv_id}
            </a>
          )}
          {p.citation_count !== null && p.citation_count !== undefined && (
            <span>🏆 {p.citation_count.toLocaleString()} cited</span>
          )}
          <span>status: {p.status}</span>
          <span>oa: {p.oa_status}</span>
        </div>
      </header>

      <div className="flex flex-wrap gap-2">
        {canParse && (
          <Button onClick={onParse} disabled={running}>
            {running
              ? "Processing…"
              : p.status === "failed"
                ? "Retry parse"
                : "Download & parse"}
          </Button>
        )}
        {canEmbed && (
          <Button onClick={onEmbed} disabled={running} variant="outline">
            {running ? "Embedding…" : "Chunk & embed"}
          </Button>
        )}
        {p.pdf_url && (
          <Button
            variant="outline"
            onClick={() => window.open(p.pdf_url!, "_blank", "noopener")}
          >
            Open original PDF
          </Button>
        )}
        {p.pdf_path && (
          <Button
            variant="outline"
            onClick={() => window.open(`/storage/${p.pdf_path}`, "_blank", "noopener")}
          >
            Open saved PDF
          </Button>
        )}
      </div>



      {running && (
        <Card>
          <CardContent className="flex items-center gap-3 pt-5 text-sm">
            <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-foreground" />
            <span className="flex-1">
              <span className="font-medium">{detail || "Working…"}</span>
              {stage && stage !== "queued" && stage !== "done" && (
                <span className="ml-2 text-xs uppercase tracking-wide text-muted-foreground">
                  {stage}
                </span>
              )}
            </span>
            {job?.started_at && (
              <span className="tabular-nums text-muted-foreground">
                {elapsed(job.started_at, now)}
              </span>
            )}
          </CardContent>
        </Card>
      )}

      {job?.status === "done" && !err && (
        <Card>
          <CardContent className="pt-5 text-sm text-green-700">
            {job.kind === "embed" ? "Embedded successfully." : "Parsed successfully."}
          </CardContent>
        </Card>
      )}

      {p.error && (
        <Card className="border-red-300">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm text-red-700">Processing error</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-red-600">{p.error}</CardContent>
        </Card>
      )}

      {!p.pdf_url && (
        <Card>
          <CardContent className="pt-5 text-sm text-muted-foreground">
            No open-access PDF is available for this paper — only metadata and the abstract
            are stored. Use “Open original PDF” if a publisher link exists.
          </CardContent>
        </Card>
      )}

      {md ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Full text</CardTitle>
          </CardHeader>
          <CardContent>
            <MarkdownReader body={md} mdPath={p.md_path} />
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Abstract</CardTitle>
          </CardHeader>
          <CardContent className="text-sm leading-relaxed">
            {p.abstract || (
              <span className="text-muted-foreground">No abstract available.</span>
            )}
          </CardContent>
        </Card>
      )}

      <CitationsCard paper={p} onChanged={load} />
    </main>
  );
}
