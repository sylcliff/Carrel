import { lazy, Suspense, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useParams, Link } from "react-router-dom";
import { ChevronDown, ChevronRight, Star, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import MarkdownReader from "@/components/MarkdownReader";
import SectionedMarkdown from "@/components/SectionedMarkdown";
import CitationsCard from "@/components/CitationsCard";
import ReferencesCard from "@/components/ReferencesCard";
import NotesCard from "@/components/NotesCard";
import PaperCardView from "@/components/PaperCard";
import { topicColorClass } from "@/lib/topicColor";
import { useApiMutation, useApiQueryWithFn } from "@/lib/useApiQuery";
import { queryKeys } from "@/lib/queryKeys";
import { useQueryClient } from "@tanstack/react-query";
import {
  addPaperTag,
  checkPublication,
  embedPaper,
  getHealth,
  getJob,
  getPaper,
  getPaperMarkdown,
  getPaperSections,
  listPaperTags,
  processPaper,
  removePaperTag,
  summarizePaper,
  toggleFavorite,
  type Job,
  type PaperDetail as PaperDetailT,
  type PaperSectionsResponse,
  type Tag,
} from "@/api/client";

const PaperChat = lazy(() =>
  import("@/components/PaperChat").then((m) => ({ default: m.PaperChat })),
);

type Props = { onProcessed?: () => void };
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
  const queryClient = useQueryClient();
  // Local UI state (not server state) — stays in useState.
  const [processing, setProcessing] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pdfOpen, setPdfOpen] = useState(false);
  const [textOpen, setTextOpen] = useState(true);
  const [now, setNow] = useState(() => Date.now());
  const [pdfVariant, setPdfVariant] = useState<"journal" | "arxiv">("journal");
  const [remoteEnabled, setRemoteEnabled] = useState(false);
  const [tagInput, setTagInput] = useState("");
  const timer = useRef<number | null>(null);

  // --- React Query: per-paper data (canonical) ----------------------------
  // invalidateQueries(["paper", id]) cascades to every nested variant.
  const paperQuery = useApiQueryWithFn<PaperDetailT>({
    key: id ? queryKeys.paper(id) : ["paper", "_missing_"],
    queryFn: () => getPaper(id!),
    enabled: Boolean(id),
  });
  const p = paperQuery.data ?? null;

  const markdownQuery = useApiQueryWithFn<{
    id: string;
    body: string | null;
    md_path: string | null;
  }>({
    key: id ? queryKeys.paperMarkdown(id) : ["paper", "_missing_", "markdown"],
    queryFn: () => getPaperMarkdown(id!),
    staleTime: Infinity,
    enabled: Boolean(id) && Boolean(p?.md_path),
  });
  const md = markdownQuery.data?.body ?? null;

  // Per-section split of the same markdown; surfaced in the Full-text
  // card as a stack of one MarkdownReader per heading (in document
  // order). Backend reuses the same ETag/L2 contract as /markdown, so
  // staleTime: Infinity matches the flat fetch.
  const sectionsQuery = useApiQueryWithFn<PaperSectionsResponse>({
    key: id ? queryKeys.paperSections(id) : ["paper", "_missing_", "sections"],
    queryFn: () => getPaperSections(id!),
    staleTime: Infinity,
    enabled: Boolean(id) && Boolean(p?.md_path),
  });
  const sections = sectionsQuery.data?.sections ?? null;

  const tagsQuery = useApiQueryWithFn<Tag[]>({
    key: id ? queryKeys.paperTags(id) : ["paper", "_missing_", "tags"],
    queryFn: () => listPaperTags(id!).catch(() => [] as Tag[]),
    enabled: Boolean(id),
  });
  const tags = tagsQuery.data ?? [];

  // --- Optimistic mutations ------------------------------------------------

  const favoriteMutation = useApiMutation<
    { next: boolean },
    { id: string; favorite: boolean }
  >({
    mutate: ({ next }) => toggleFavorite(id!, next),
    invalidate: [queryKeys.papersRoot()],
    onOptimistic: ({ next }, qc) => {
      if (!id) return;
      qc.setQueryData<PaperDetailT>(queryKeys.paper(id), (prev) =>
        prev ? { ...prev, favorite: next } : prev,
      );
    },
    onRollback: (_input, _error, qc) => {
      if (!id) return;
      qc.setQueryData<PaperDetailT>(queryKeys.paper(id), (prev) => {
        if (!prev) return prev;
        return { ...prev, favorite: !prev.favorite };
      });
    },
  });

  const addTagMutation = useApiMutation<{ raw: string; provisional: Tag }, Tag>({
    mutate: ({ raw }) => addPaperTag(id!, raw),
    invalidate: [queryKeys.tags()],
    onOptimistic: ({ provisional }, qc) => {
      if (!id) return;
      qc.setQueryData<Tag[]>(queryKeys.paperTags(id), (prev) =>
        prev ? [...prev, provisional] : [provisional],
      );
    },
    onRollback: ({ provisional }, _error, qc) => {
      if (!id) return;
      qc.setQueryData<Tag[]>(queryKeys.paperTags(id), (prev) =>
        prev ? prev.filter((t) => t.id !== provisional.id) : prev,
      );
    },
    mutationOptions: {
      onSuccess: (saved, { provisional }) => {
        if (!id) return;
        // Replace the provisional tag with the server-confirmed one.
        queryClient.setQueryData<Tag[]>(queryKeys.paperTags(id), (prev) =>
          prev
            ? prev.map((t) => (t.id === provisional.id ? saved : t))
            : [saved],
        );
      },
    },
  });

  const removeTagMutation = useApiMutation<
    { tag: Tag },
    { id: number; paper_id: string; detached: boolean }
  >({
    mutate: ({ tag }) => removePaperTag(id!, tag.id),
    onOptimistic: ({ tag }, qc) => {
      if (!id) return;
      qc.setQueryData<Tag[]>(queryKeys.paperTags(id), (prev) =>
        prev ? prev.filter((t) => t.id !== tag.id) : prev,
      );
    },
    onRollback: ({ tag }, _error, qc) => {
      if (!id) return;
      qc.setQueryData<Tag[]>(queryKeys.paperTags(id), (prev) =>
        prev ? [...prev, tag] : prev,
      );
    },
  });

  useEffect(() => {
    getHealth()
      .then((h) => setRemoteEnabled(h.remote))
      .catch(() => setRemoteEnabled(false));
  }, []);

  useEffect(() => {
    setPdfOpen(false);
  }, [id]);

  useEffect(() => {
    if (paperQuery.error) setErr(String(paperQuery.error));
  }, [paperQuery.error]);

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
          await paperQuery.refetch();
          await markdownQuery.refetch();
          await sectionsQuery.refetch();
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

  async function onSummarize(force = true) {
    if (!id) return;
    setProcessing(true);
    setErr(null);
    setJob(null);
    try {
      const started = await summarizePaper(id, true, force);
      const firstJob = started[0];
      if (!firstJob) throw new Error("No job returned");
      watchJob(firstJob);
    } catch (e) {
      setProcessing(false);
      setErr(String(e));
    }
  }

  async function onCheckPublication() {
    if (!id) return;
    setProcessing(true);
    setErr(null);
    setJob(null);
    try {
      const jobStarted = await checkPublication(id, true, Boolean(p?.journal_doi));
      watchJob(jobStarted);
    } catch (e) {
      setProcessing(false);
      setErr(String(e));
    }
  }

  useEffect(
    () => () => {
      if (timer.current) window.clearInterval(timer.current);
    },
    [],
  );

  async function onToggleFavorite() {
    if (!id) return;
    const next = !(p?.favorite ?? false);
    try {
      await favoriteMutation.mutateAsync({ next });
    } catch (e) {
      setErr(`Could not update favorite: ${String(e)}`);
    }
  }

  async function onAddTag(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== "Enter" || !id) return;
    const raw = tagInput.trim();
    if (!raw) return;
    if (tags.some((t) => t.name.toLowerCase() === raw.toLowerCase())) {
      setTagInput("");
      return;
    }
    setTagInput("");
    const provisional: Tag = { id: -Date.now(), name: raw };
    addTagMutation.mutate({ raw, provisional });
  }

  async function onRemoveTag(tag: Tag) {
    if (!id) return;
    if (tag.id < 0) return;
    removeTagMutation.mutate({ tag });
  }

  // A load() shim used by child cards (References / Citations) that want
  // to refresh the parent detail after a write.
  async function load() {
    await Promise.all([
      paperQuery.refetch(),
      tagsQuery.refetch(),
      markdownQuery.refetch(),
      sectionsQuery.refetch(),
    ]);
  }

  if (err && !p)
    return <main className="container py-8 text-sm text-red-600">{err}</main>;
  if (!p)
    return (
      <main className="container py-8 text-sm text-muted-foreground">Loading…</main>
    );

  const pdfHref = p.pdf_url || (p.arxiv_id ? `https://arxiv.org/pdf/${p.arxiv_id}.pdf` : "");
  const canParse =
    (Boolean(pdfHref) || (remoteEnabled && Boolean(p.doi || p.arxiv_id || p.journal_doi))) &&
    p.status !== "parsed" &&
    p.status !== "summarized" &&
    p.status !== "ready";
  const variants = p.pdf_files || {};
  const hasJournalVariant = Boolean(variants.journal);
  const hasArxivVariant = Boolean(variants.arxiv);
  const hasPdfVariants = hasJournalVariant && hasArxivVariant;
  const activePdfPath = hasPdfVariants
    ? variants[pdfVariant] || p.pdf_path
    : p.pdf_path;
  const showInstitutionalCard =
    !pdfHref && !p.pdf_path && remoteEnabled && Boolean(p.doi || p.arxiv_id || p.journal_doi);
  const canEmbed =
    p.status === "parsed" ||
    p.status === "summarized" ||
    (p.status === "failed" && Boolean(p.md_path));
  const canSummarize = Boolean(p.md_path);
  const hasSummary = Boolean(p.tldr_en || p.tldr_zh || p.summary_zh);
  const stage = (job?.stats?.stage as string | undefined) ?? "";
  const detail = (job?.stats?.detail as string | undefined) ?? job?.message ?? "";
  const running = Boolean(processing || (job && !TERMINAL.has(job.status)));
  const favBusy = favoriteMutation.isPending;

  return (
    <main className="w-full space-y-6 px-6 py-8 xl:px-0">
      <div className="grid gap-6 xl:gap-4 xl:grid-cols-[max(24rem,calc((100vw-98rem)/2))_minmax(0,1fr)_max(24rem,calc((100vw-98rem)/2))]">
        <aside className="min-w-0 xl:col-start-1 xl:sticky xl:top-4 xl:self-start xl:max-h-[calc(100vh-2rem)] xl:overflow-y-auto space-y-4">
          <PaperCardView paperId={p.id} hasMarkdown={Boolean(p.md_path)} />
          <NotesCard paperId={p.id} initialMarkdown={p.notes_markdown} />
        </aside>

        <div className="min-w-0 space-y-6 xl:col-start-2">
          <div className="mx-auto w-full max-w-screen-2xl space-y-6">
            <div>
              <Link to="/library" className="text-sm text-muted-foreground hover:underline">
                ← Back
              </Link>
            </div>

            <header className="space-y-2 text-center">
              <h1 className="text-2xl font-bold">{p.title}</h1>
              <div className="text-sm text-muted-foreground">
                {p.author_list && p.author_list.length > 0 ? (
                  p.author_list.map((a, i) => {
                    const key = a.openalex_author_id?.trim()
                      ? a.openalex_author_id.trim()
                      : `name:${a.name.trim()}`;
                    return (
                      <span key={`${a.name}-${i}`}>
                        {i > 0 && ", "}
                        <Link
                          to={`/scholars/${encodeURIComponent(key)}`}
                          className="hover:text-foreground hover:underline"
                        >
                          {a.name}
                        </Link>
                      </span>
                    );
                  })
                ) : (
                  p.authors.join(", ") || "—"
                )}
              </div>
              <div className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                {p.venue && <span>📰 {p.venue}</span>}
                {p.journal_citation && (
                  <span title="Volume / issue / pages (from OpenAlex biblio)">
                    📖 {p.journal_citation}
                  </span>
                )}
                {p.publication_date && <span>📅 {p.publication_date}</span>}
                {(() => {
                  const journalDoi = p.journal_doi || "";
                  const fallbackDoi = !journalDoi && p.doi ? p.doi : "";
                  const primaryDoi = journalDoi || fallbackDoi;
                  if (!primaryDoi) return null;
                  const bare = primaryDoi.replace(/^https?:\/\/(dx\.)?doi\.org\//i, "");
                  const href = /^https?:\/\//i.test(primaryDoi)
                    ? primaryDoi
                    : `https://doi.org/${bare}`;
                  return (
                    <a
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="underline-offset-2 hover:text-foreground hover:underline"
                      title={journalDoi ? "Journal DOI" : "DOI"}
                    >
                      {journalDoi ? "📕 Journal DOI: " : "DOI: "}
                      {bare}
                    </a>
                  );
                })()}
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
                {p.arxiv_id && (
                  <a
                    href={`https://doi.org/10.48550/arXiv.${p.arxiv_id}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline-offset-2 hover:text-foreground hover:underline"
                    title="arXiv DOI"
                  >
                    arXiv DOI
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
            <Button
              variant="outline"
              size="icon"
              onClick={onToggleFavorite}
              disabled={favBusy}
              aria-pressed={p.favorite}
              title={p.favorite ? "Remove from favorites" : "Add to favorites"}
            >
              <Star
                className={
                  p.favorite
                    ? "h-4 w-4 fill-yellow-400 text-yellow-500"
                    : "h-4 w-4"
                }
              />
            </Button>
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
            {canSummarize && (
              <Button
                onClick={() => onSummarize(true)}
                disabled={running}
                variant="outline"
                title="Regenerate the bilingual TL;DR, Chinese summary, and keywords"
              >
                {running ? "Summarizing…" : hasSummary ? "Regenerate summary" : "Generate summary"}
              </Button>
            )}
            {pdfHref && (
              <Button
                variant="outline"
                onClick={() => window.open(pdfHref, "_blank", "noopener")}
              >
                Open original PDF
              </Button>
            )}
            {p.arxiv_id && (
              <Button
                variant="outline"
                onClick={onCheckPublication}
                disabled={running}
                title="Check whether this arXiv preprint has been published in a journal, and fetch the journal PDF via institutional access"
              >
                {running ? "Checking…" : p.journal_doi ? "Re-check journal version" : "Check for journal version"}
              </Button>
            )}
            {p.pdf_path && (
              <Button
                variant="outline"
                onClick={() => setPdfOpen((v) => !v)}
                aria-pressed={pdfOpen}
              >
                {pdfOpen ? "Close PDF" : "Open saved PDF"}
              </Button>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {tags.map((t) => (
              <span
                key={t.id}
                className="inline-flex items-center gap-1 rounded-full border bg-muted/40 px-2 py-0.5 text-xs"
              >
                {t.name}
                <button
                  type="button"
                  onClick={() => onRemoveTag(t)}
                  aria-label={`Remove tag ${t.name}`}
                  className="text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
            <input
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              onKeyDown={onAddTag}
              placeholder="Add tag…"
              className="h-7 w-36 rounded-md border border-input bg-background px-2 text-xs focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>

          {p.topics && p.topics.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Topics:</span>
              {p.topics.map((t) => (
                <Link
                  key={t}
                  to={`/library?topic=${encodeURIComponent(t)}`}
                  title={`View papers in ${t}`}
                  className={`rounded-full px-2 py-0.5 text-xs font-medium hover:opacity-80 ${topicColorClass(t)}`}
                >
                  {t}
                </Link>
              ))}
            </div>
          )}

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
                {job.kind === "embed"
                  ? "Embedded successfully."
                  : job.kind === "summarize"
                    ? "Summary generated."
                    : job.kind === "publication_check"
                      ? "Journal version check complete."
                      : "Parsed successfully."}
              </CardContent>
            </Card>
          )}

          {hasSummary && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Summary</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm leading-relaxed">
                {p.tldr_en && (
                  <p>
                    <span className="mr-1 font-medium text-muted-foreground">EN:</span>
                    {p.tldr_en}
                  </p>
                )}
                {p.tldr_zh && (
                  <p>
                    <span className="mr-1 font-medium text-muted-foreground">中文:</span>
                    {p.tldr_zh}
                  </p>
                )}
                {p.summary_zh && (
                  <p className="whitespace-pre-line text-foreground/90">{p.summary_zh}</p>
                )}
                {p.keywords && p.keywords.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 pt-1">
                    {p.keywords.map((k) => (
                      <span
                        key={k}
                        className="rounded-full border bg-muted/40 px-2 py-0.5 text-xs"
                      >
                        {k}
                      </span>
                    ))}
                  </div>
                )}
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

          {!pdfHref && (
            <Card>
              <CardContent className="space-y-3 pt-5 text-sm text-muted-foreground">
                {showInstitutionalCard ? (
                  <>
                    <p>
                      No open-access PDF is available. A PDF can be fetched through your
                      institutional access server.
                    </p>
                    <Button onClick={onParse} disabled={running} size="sm">
                      {running ? "Fetching…" : "Download via institutional access"}
                    </Button>
                  </>
                ) : (
                  <p>
                    No open-access PDF is available for this paper — only metadata and the
                    abstract are stored. Use “Open original PDF” if a publisher link exists.
                  </p>
                )}
              </CardContent>
            </Card>
          )}

          {pdfOpen && activePdfPath ? (
            <Card className="overflow-hidden">
              <CardHeader className="flex-row items-center justify-between space-y-0 py-3">
                <CardTitle className="text-base">PDF</CardTitle>
                {hasPdfVariants && (
                  <div className="inline-flex rounded-md border bg-muted p-0.5 text-xs" role="group" aria-label="PDF variant">
                    <button
                      type="button"
                      onClick={() => setPdfVariant("journal")}
                      className={`rounded px-2.5 py-1 ${pdfVariant === "journal" ? "bg-background shadow-sm font-medium" : "text-muted-foreground"}`}
                    >
                      Journal
                    </button>
                    <button
                      type="button"
                      onClick={() => setPdfVariant("arxiv")}
                      className={`rounded px-2.5 py-1 ${pdfVariant === "arxiv" ? "bg-background shadow-sm font-medium" : "text-muted-foreground"}`}
                    >
                      arXiv
                    </button>
                  </div>
                )}
              </CardHeader>
              <CardContent className="p-0">
                <iframe
                  title={`${p.title} — PDF`}
                  src={`/storage/${activePdfPath}`}
                  className="h-[calc(100vh-200px)] w-full border-0"
                />
              </CardContent>
            </Card>
          ) : null}
          {md ? (
            <Card>
              <CardHeader className="flex-row items-center justify-between space-y-0">
                <button
                  type="button"
                  onClick={() => setTextOpen((v) => !v)}
                  className="flex items-center gap-2 text-left"
                >
                  {textOpen ? (
                    <ChevronDown className="h-4 w-4 text-muted-foreground" />
                  ) : (
                    <ChevronRight className="h-4 w-4 text-muted-foreground" />
                  )}
                  <CardTitle className="text-base">Full text</CardTitle>
                </button>
              </CardHeader>
              {textOpen && (
                <CardContent>
                  {sections && sections.length > 0 ? (
                    <SectionedMarkdown sections={sections} mdPath={p.md_path} />
                  ) : (
                    <MarkdownReader body={md} mdPath={p.md_path} />
                  )}
                </CardContent>
              )}
            </Card>
          ) : (
            <Card>
              <CardHeader className="flex-row items-center justify-between space-y-0">
                <button
                  type="button"
                  onClick={() => setTextOpen((v) => !v)}
                  className="flex items-center gap-2 text-left"
                >
                  {textOpen ? (
                    <ChevronDown className="h-4 w-4 text-muted-foreground" />
                  ) : (
                    <ChevronRight className="h-4 w-4 text-muted-foreground" />
                  )}
                  <CardTitle className="text-base">Abstract</CardTitle>
                </button>
              </CardHeader>
              {textOpen && (
                <CardContent className="text-sm leading-relaxed">
                  {p.abstract || (
                    <span className="text-muted-foreground">No abstract available.</span>
                  )}
                </CardContent>
              )}
            </Card>
          )}

          <ReferencesCard paper={p} onChanged={load} />
          <CitationsCard paper={p} onChanged={load} />
          </div>
        </div>
      </div>

      <div className="static xl:fixed xl:top-14 xl:bottom-0 xl:right-0 xl:z-30 xl:w-[max(24rem,calc((100vw-98rem)/2))] xl:border-l xl:bg-background xl:shadow-lg">
        <Suspense fallback={<div className="h-[70vh] w-full animate-pulse bg-muted xl:h-full" />}>
          <PaperChat paperId={p.id} hasMarkdown={!!p.md_path} />
        </Suspense>
      </div>
    </main>
  );
}
