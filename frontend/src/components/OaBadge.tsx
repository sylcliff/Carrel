import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

/**
 * Open-access badge. Distinguishes "full text available in Carrel" from
 * "metadata/abstract only" so users don't mistake closed-access papers for a
 * broken pipeline (PLAN §9.5).
 *
 * For PaperSummary rows we don't have hasMd/hasPdf directly — callers pass
 * `status` and we infer: parsed/summarized/ready => md present; pdf_ready =>
 * pdf present. For PaperDetail, pass hasMd/hasPdf explicitly.
 */
export function OaBadge({
  oaStatus,
  status,
  hasMd,
  hasPdf,
}: {
  oaStatus: string | null;
  status?: string;
  hasMd?: boolean;
  hasPdf?: boolean;
}) {
  const md = hasMd ?? (status === "ready" || status === "parsed" || status === "summarized");
  const pdf = hasPdf ?? (status === "pdf_ready" || md);
  const readable = md || pdf;
  const open = (oaStatus ?? "none") === "oa";

  // Closed / no PDF at all -> metadata only.
  if (!open && !pdf) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="rounded border border-muted-foreground/30 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            abstract only
          </span>
        </TooltipTrigger>
        <TooltipContent>
          No open-access PDF found — title/abstract stored only.
        </TooltipContent>
      </Tooltip>
    );
  }

  // OA but pipeline hasn't produced readable content yet -> in progress.
  if (open && !readable) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="rounded border border-yellow-500/40 bg-yellow-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-yellow-700">
            OA · pending
          </span>
        </TooltipTrigger>
        <TooltipContent>Open-access PDF queued for download/parsing.</TooltipContent>
      </Tooltip>
    );
  }

  // Has markdown -> full text ready.
  if (hasMd) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="rounded border border-green-500/40 bg-green-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-green-700">
            full text
          </span>
        </TooltipTrigger>
        <TooltipContent>Markdown ready — click to read.</TooltipContent>
      </Tooltip>
    );
  }

  // PDF downloaded but not parsed yet.
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="rounded border border-blue-500/40 bg-blue-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-blue-700">
          PDF
        </span>
      </TooltipTrigger>
      <TooltipContent>PDF downloaded, parsing pending.</TooltipContent>
    </Tooltip>
  );
}
