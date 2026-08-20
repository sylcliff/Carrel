import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, FileText } from "lucide-react";
import { saveNotes } from "@/api/client";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import MarkdownReader from "./MarkdownReader";

type SaveState = "idle" | "saving" | "saved" | "error";

type Props = {
  paperId: string;
  initialMarkdown: string | null;
  onSaved?: (markdown: string, updatedAt: string) => void;
};

const AUTOSAVE_MS = 800;

export default function NotesCard({ paperId, initialMarkdown, onSaved }: Props) {
  const [draft, setDraft] = useState(initialMarkdown ?? "");
  const [state, setState] = useState<SaveState>("idle");
  const [open, setOpen] = useState(true);
  const [preview, setPreview] = useState(false);

  const lastSaved = useRef(initialMarkdown ?? "");
  const seq = useRef(0);
  const timer = useRef<number | null>(null);
  const draftRef = useRef(draft);
  draftRef.current = draft;

  const flush = useCallback(async () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
    if (draftRef.current === lastSaved.current) return;
    const mySeq = ++seq.current;
    setState("saving");
    try {
      const out = await saveNotes(paperId, draftRef.current);
      if (mySeq !== seq.current) return; // a newer save superseded this one
      lastSaved.current = draftRef.current;
      setState("saved");
      onSaved?.(out.notes_markdown ?? "", out.updated_at);
      window.setTimeout(() => {
        if (seq.current === mySeq) setState("idle");
      }, 2000);
    } catch {
      if (mySeq !== seq.current) return;
      setState("error");
    }
  }, [paperId, onSaved]);

  // Debounced autosave whenever the draft changes.
  useEffect(() => {
    if (draft === lastSaved.current) return;
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      void flush();
    }, AUTOSAVE_MS);
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [draft, flush]);

  // Save on unmount / tab hide so the last keystrokes aren't lost.
  useEffect(() => {
    const onHide = () => {
      if (document.visibilityState === "hidden") void flush();
    };
    window.addEventListener("beforeunload", onHide);
    document.addEventListener("visibilitychange", onHide);
    return () => {
      window.removeEventListener("beforeunload", onHide);
      document.removeEventListener("visibilitychange", onHide);
      void flush();
    };
  }, [flush]);

  const statusLabel =
    state === "saving"
      ? "Saving…"
      : state === "saved"
        ? "Saved"
        : state === "error"
          ? "Save failed — will retry"
          : null;

  return (
    <Card>
      <CardHeader className="cursor-pointer select-none" onClick={() => setOpen((v) => !v)}>
        <CardTitle className="flex items-center gap-2 text-base">
          <FileText className="h-4 w-4" />
          Notes
          {statusLabel && (
            <span
              className={`ml-2 text-xs font-normal ${
                state === "error" ? "text-red-600" : "text-muted-foreground"
              }`}
            >
              {statusLabel}
            </span>
          )}
          <ChevronDown
            className={`ml-auto h-4 w-4 transition-transform ${open ? "" : "-rotate-90"}`}
          />
        </CardTitle>
      </CardHeader>
      {open && (
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2">
            <div className="inline-flex rounded-md border">
              <button
                type="button"
                onClick={() => setPreview(false)}
                className={`rounded-l-md px-2 py-1 text-xs ${
                  !preview ? "bg-muted font-medium" : "text-muted-foreground"
                }`}
              >
                Edit
              </button>
              <button
                type="button"
                onClick={() => setPreview(true)}
                className={`rounded-r-md px-2 py-1 text-xs ${
                  preview ? "bg-muted font-medium" : "text-muted-foreground"
                }`}
              >
                Preview
              </button>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void flush()}
              disabled={state === "saving" || draft === lastSaved.current}
            >
              Save now
            </Button>
          </div>

          {preview ? (
            draft.trim() ? (
              <MarkdownReader body={draft} mdPath={null} />
            ) : (
              <p className="text-sm text-muted-foreground">Nothing to preview.</p>
            )
          ) : (
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Your notes on this paper (Markdown supported)…"
              className="min-h-[20rem] w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm leading-relaxed focus:outline-none focus:ring-2 focus:ring-ring"
            />
          )}
        </CardContent>
      )}
    </Card>
  );
}
