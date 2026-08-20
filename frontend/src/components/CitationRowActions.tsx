import { useState } from "react";

import { Check, FileDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Link } from "react-router-dom";
import { importPaper, APIError, type CitationItem } from "@/api/client";

/** Pull a human-readable reason out of a thrown import error. */
function errorMessage(e: unknown): string {
  if (e instanceof APIError) {
    // FastAPI errors come back as {"detail": "..."} (string) or a structured
    // list; pull out whatever we can show.
    try {
      const parsed = JSON.parse(e.message);
      if (typeof parsed?.detail === "string") return parsed.detail;
      if (Array.isArray(parsed?.detail)) {
        return parsed.detail
          .map((d: { msg?: string }) => d?.msg)
          .filter(Boolean)
          .join("; ");
      }
    } catch {
      // fall through to raw text
    }
    if (e.status === 404) return "Not found on OpenAlex/S2";
    if (e.status === 429) return "Rate limited — retry in a moment";
    if (e.status === 422) return e.message || "Not importable";
    return e.message || `HTTP ${e.status}`;
  }
  return e instanceof Error ? e.message : String(e);
}

type Props = {
  item: CitationItem;
  /**
   * Called after a successful import with the new Carrel paper id so the parent
   * list can mark the row `in_library` in place (without resetting pagination).
   */
  onImported?: (paperId: string) => void;
};

/**
 * Trailing control for a citation/reference row.
 *  - in library  → "in library" badge (the title itself is a link)
 *  - not in lib  → Import button; after success becomes an "open →" link
 *
 * Shared by CitationsCard (cited-by) and ReferencesCard (bibliography).
 */
export default function CitationRowActions({ item, onImported }: Props) {
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [err, setErr] = useState<string | null>(null);
  const [importedId, setImportedId] = useState<string | null>(null);

  if (item.in_library) {
    return (
      <span
        className="ml-2 inline-flex shrink-0 items-center gap-0.5 rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
        title="Already in your library"
      >
        <Check className="h-3 w-3" /> in library
      </span>
    );
  }

  if (state === "done" && importedId) {
    return (
      <span className="ml-2 inline-flex shrink-0 items-center gap-2">
        <span className="inline-flex items-center gap-0.5 rounded-full bg-green-100 px-2 py-0.5 text-[11px] text-green-700 dark:bg-green-950 dark:text-green-400">
          <Check className="h-3 w-3" /> added
        </span>
        <Link
          to={`/papers/${encodeURIComponent(importedId)}`}
          className="text-xs text-primary hover:underline"
        >
          open →
        </Link>
      </span>
    );
  }

  async function onImport() {
    setState("busy");
    setErr(null);
    try {
      const out = await importPaper({
        openalex_id: item.openalex_id ?? undefined,
        doi: item.doi ?? undefined,
        arxiv_id: item.arxiv_id ?? undefined,
        s2: item.s2_paper_id ?? undefined,
        title: item.title ?? undefined,
      });
      setImportedId(out.id);
      setState("done");
      onImported?.(out.id);
    } catch (e) {
      setErr(errorMessage(e));
      setState("idle");
    }
  }

  return (
    <span className="ml-2 inline-flex shrink-0 items-center">
      <Button
        size="sm"
        variant="outline"
        onClick={onImport}
        disabled={state === "busy"}
        title={err ?? "Add this paper to your library"}
        className="h-7 px-2 text-xs"
      >
        <FileDown className="mr-1 h-3.5 w-3.5" />
        {state === "busy" ? "Importing…" : "Import"}
      </Button>
      {err ? (
        <span
          className="ml-2 max-w-[260px] truncate text-xs text-red-600"
          title={err}
        >
          {err}
        </span>
      ) : null}
    </span>
  );
}
