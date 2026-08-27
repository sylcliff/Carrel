import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Edit3,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  resetUsagePrompt,
  updateUsagePrompt,
  type UsageBucket,
  type UsagePrompt,
  type UsagePromptOverride,
} from "@/api/client";

interface PromptEditorProps {
  prompt: UsagePrompt;
  usage: UsageBucket | null;
  /** Called after a successful save / reset so the parent can refresh the list. */
  onChanged?: (next: UsagePrompt) => void;
  /** When true, render a compact view (used inside the pipeline sidebar). */
  compact?: boolean;
  /** Number formatter for the usage token/call badge. */
  fmt?: (n: number) => string;
}

const DEFAULT_FMT = (n: number) => n.toLocaleString();

/**
 * Shared, editable prompt block used by both the /agent catalog and the
 * /agent/{pipelineId} sidebar. Display is read-only by default; clicking
 * "Edit" reveals side-by-side textareas for the system prompt and user
 * template, with save/reset/revert buttons. Placeholder validation
 * warnings surface in a non-blocking list below the editor.
 *
 * Persistence is per-feature: each call site keys on its `feature` string
 * (e.g. "summarize", "paper_chat"), and the backend keeps the override
 * with a 60s in-process TTL (synchronously invalidated on PUT / DELETE).
 */
export function PromptEditor({
  prompt,
  usage,
  onChanged,
  compact = false,
  fmt = DEFAULT_FMT,
}: PromptEditorProps) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [systemDraft, setSystemDraft] = useState(prompt.system);
  const [userDraft, setUserDraft] = useState(prompt.user_template);
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  // When the parent's prompt changes (refresh after save, switch pipelines,
  // etc.), resync the drafts. Only when not actively editing — the user
  // shouldn't have their in-flight edits clobbered by a background refresh.
  useEffect(() => {
    if (!editing) {
      setSystemDraft(prompt.system);
      setUserDraft(prompt.user_template);
      setWarnings([]);
      setError(null);
    }
  }, [prompt.system, prompt.user_template, editing]);

  const dirty =
    systemDraft !== prompt.system || userDraft !== prompt.user_template;

  function startEdit() {
    setSystemDraft(prompt.system);
    setUserDraft(prompt.user_template);
    setWarnings([]);
    setError(null);
    setEditing(true);
  }

  function cancelEdit() {
    setSystemDraft(prompt.system);
    setUserDraft(prompt.user_template);
    setWarnings([]);
    setError(null);
    setEditing(false);
  }

  async function save() {
    if (saving) return;
    setSaving(true);
    setError(null);
    setWarnings([]);
    const body: UsagePromptOverride = { system: systemDraft, user_template: userDraft };
    try {
      const result = await updateUsagePrompt(prompt.feature, body);
      setWarnings(result.warnings);
      onChanged?.({
        ...prompt,
        system: systemDraft,
        user_template: userDraft,
        overridden: true,
        override_updated_at: result.override.updated_at,
      });
      setEditing(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function reset() {
    if (resetting) return;
    if (!confirm(`Reset "${prompt.label}" back to the default prompt?`)) return;
    setResetting(true);
    setError(null);
    try {
      await resetUsagePrompt(prompt.feature);
      onChanged?.({
        ...prompt,
        system: prompt.system_default,
        user_template: prompt.user_template_default,
        overridden: false,
        override_updated_at: null,
      });
      setSystemDraft(prompt.system_default);
      setUserDraft(prompt.user_template_default);
      setWarnings([]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setResetting(false);
    }
  }

  const placeholderList = prompt.placeholders ?? [];

  return (
    <div className="rounded-md border">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-muted/50"
        aria-expanded={open}
      >
        <div className="mt-0.5 text-muted-foreground">
          {open ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-sm font-medium">{prompt.label}</span>
            <span
              className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
              title="Token usage feature name"
            >
              {prompt.feature}
            </span>
            {prompt.overridden && (
              <span
                className="rounded bg-amber-100 px-1.5 py-0.5 font-mono text-[10px] text-amber-800 dark:bg-amber-900/50 dark:text-amber-200"
                title={`Overridden on ${prompt.override_updated_at ?? "unknown"}`}
              >
                modified
              </span>
            )}
            {usage && (
              <span
                className="ml-auto rounded bg-primary/10 px-1.5 py-0.5 font-mono text-[10px] text-primary tabular-nums"
                title={`${usage.calls} LLM call(s) recorded in the last 30 days`}
              >
                {fmt(usage.total_tokens)} tok · {usage.calls} call
                {usage.calls === 1 ? "" : "s"}
              </span>
            )}
          </div>
          {!compact && (
            <div
              className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground"
              title={prompt.source}
            >
              {prompt.source}
            </div>
          )}
          {!compact && prompt.notes && (
            <div className="mt-1 text-[11px] text-muted-foreground">{prompt.notes}</div>
          )}
        </div>
      </button>

      {open && (
        <div className="space-y-3 border-t px-3 py-3">
          {prompt.danger && (
            <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-2 text-[11px] text-amber-900 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-200">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <div>
                <strong>Wide blast radius.</strong> Changes here affect every
                user-driven chat/agent invocation using this prompt. Edit with
                care, and verify in a real conversation before trusting the
                override in production.
              </div>
            </div>
          )}

          {placeholderList.length > 0 && (
            <div className="text-[10px] text-muted-foreground">
              Placeholders this template expects:{" "}
              {placeholderList.map((p) => (
                <code
                  key={p}
                  className="mx-0.5 rounded bg-muted px-1 py-0.5 font-mono text-[10px]"
                >
                  {`{${p}}`}
                </code>
              ))}
            </div>
          )}

          {editing ? (
            <>
              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    System prompt
                  </div>
                  <button
                    type="button"
                    onClick={() => setSystemDraft(prompt.system_default)}
                    disabled={systemDraft === prompt.system_default}
                    className="text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                    title="Reset this field to its default value"
                  >
                    <RotateCcw className="mr-0.5 inline h-3 w-3" />
                    default
                  </button>
                </div>
                <textarea
                  value={systemDraft}
                  onChange={(e) => setSystemDraft(e.target.value)}
                  className="min-h-[10rem] w-full rounded-md border border-input bg-background p-2 font-mono text-[11px] leading-relaxed focus:outline-none focus:ring-2 focus:ring-ring"
                  spellCheck={false}
                />
              </div>

              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    User prompt (template)
                  </div>
                  <button
                    type="button"
                    onClick={() => setUserDraft(prompt.user_template_default)}
                    disabled={userDraft === prompt.user_template_default}
                    className="text-[10px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                    title="Reset this field to its default value"
                  >
                    <RotateCcw className="mr-0.5 inline h-3 w-3" />
                    default
                  </button>
                </div>
                <textarea
                  value={userDraft}
                  onChange={(e) => setUserDraft(e.target.value)}
                  className="min-h-[8rem] w-full rounded-md border border-input bg-background p-2 font-mono text-[11px] leading-relaxed focus:outline-none focus:ring-2 focus:ring-ring"
                  spellCheck={false}
                />
              </div>

              {warnings.length > 0 && (
                <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-[11px] text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
                  <div className="mb-1 font-medium">Saved with warnings:</div>
                  <ul className="ml-4 list-disc space-y-0.5">
                    {warnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                </div>
              )}

              {error && (
                <div className="rounded-md border border-rose-200 bg-rose-50 p-2 text-[11px] text-rose-900 dark:border-rose-800 dark:bg-rose-950/30 dark:text-rose-200">
                  {error}
                </div>
              )}

              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" onClick={() => void save()} disabled={saving || !dirty}>
                  {saving ? "Saving…" : "Save"}
                </Button>
                <Button variant="outline" size="sm" onClick={cancelEdit} disabled={saving}>
                  <X className="mr-1 h-3.5 w-3.5" />
                  Cancel
                </Button>
                {prompt.overridden && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void reset()}
                    disabled={resetting || saving}
                    title="Drop the override and use the default for both fields"
                  >
                    <Trash2 className="mr-1 h-3.5 w-3.5" />
                    {resetting ? "Resetting…" : "Reset to default"}
                  </Button>
                )}
                <span className="ml-auto text-[10px] text-muted-foreground">
                  {dirty ? "unsaved changes" : "no changes"}
                </span>
              </div>
            </>
          ) : (
            <>
              <PromptBlock text={prompt.system} label="System prompt" />
              <PromptBlock text={prompt.user_template} label="User prompt (template)" />
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <Button variant="outline" size="sm" onClick={startEdit}>
                  <Edit3 className="mr-1 h-3.5 w-3.5" />
                  Edit
                </Button>
                {prompt.overridden && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void reset()}
                    disabled={resetting}
                    title="Drop the override and use the default for both fields"
                  >
                    <Trash2 className="mr-1 h-3.5 w-3.5" />
                    {resetting ? "Resetting…" : "Reset to default"}
                  </Button>
                )}
                <span className="ml-auto text-[10px] text-muted-foreground">
                  {prompt.overridden
                    ? `last edited ${prompt.override_updated_at ?? "—"}`
                    : "using default"}
                </span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function PromptBlock({ text, label }: { text: string; label: string }) {
  return (
    <div className="space-y-1">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <pre className="max-h-80 overflow-auto rounded-md border bg-muted/30 p-3 text-[11px] leading-relaxed text-foreground whitespace-pre-wrap break-words">
        {text}
      </pre>
    </div>
  );
}
