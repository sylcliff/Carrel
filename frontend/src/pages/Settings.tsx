import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Lock, RotateCcw, Save, X } from "lucide-react";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  getSettings,
  listSubscriptions,
  updateSettings,
  type EnvEntry,
  type SerialisedSection,
  type Settings as SettingsData,
  type Subscription,
} from "@/api/client";
import { humanizeCron } from "@/lib/cron";

// -------- Field rendering --------

type FieldKind = "text" | "int" | "float" | "bool" | "list-str" | "cron" | "select";

interface SelectOption {
  value: string;
  label: string;
}

interface FieldSpec {
  label: string;
  kind: FieldKind;
  hint?: string;
  options?: SelectOption[]; // only used when kind === "select"
}

type SectionSpec = {
  title: string;
  description: string;
  fields: Record<string, FieldSpec>;
};

const SECTION_SPECS: Record<string, SectionSpec> = {
  llm: {
    title: "LLM (summarize / chat / dedup judge)",
    description: "Models, temperature, and RAG chat knobs.",
    fields: {
      summarize_provider:        { label: "Summarize provider",       kind: "text" },
      summarize_model:           { label: "Summarize model",          kind: "text" },
      fallback_provider:         { label: "Fallback provider",        kind: "text" },
      fallback_model:            { label: "Fallback model",           kind: "text" },
      temperature:               { label: "Temperature",              kind: "float" },
      request_timeout_seconds:   { label: "Request timeout (s)",      kind: "int" },
      output_language: {
        label: "Output language (LLM)",
        kind: "select",
        options: [
          { value: "zh", label: "中文 (zh)" },
          { value: "en", label: "English (en)" },
        ],
        hint: "Language for LLM-generated paper card and summary fields. Default 中文.",
      },
      max_input_chars:           { label: "Max input chars",          kind: "int" },
      chat_model:                { label: "Chat model",               kind: "text" },
      chat_fallback_model:       { label: "Chat fallback model",      kind: "text" },
      chat_temperature:          { label: "Chat temperature",         kind: "float" },
      rag_top_k:                 { label: "RAG top-k",                kind: "int" },
      chat_history_limit:        { label: "Chat history limit",       kind: "int" },
      chat_fulltext_chars:       { label: "Chat fulltext chars",      kind: "int" },
      paper_dedup_judge_model:   { label: "Paper dedup judge model",  kind: "text" },
      paper_dedup_judge_fallback:{ label: "Paper dedup judge fallback", kind: "text" },
      paper_dedup_judge_prompt_version: { label: "Dedup judge prompt version", kind: "int" },
      paper_dedup_judge_max_calls_per_run: { label: "Dedup judge max calls / run", kind: "int" },
    },
  },
  embeddings: {
    title: "Embeddings",
    description: "Vector embedding provider for semantic search.",
    fields: {
      provider:                { label: "Provider",           kind: "text" },
      model:                   { label: "Model",              kind: "text" },
      dim:                     { label: "Dim",                kind: "int" },
      request_timeout_seconds: { label: "Request timeout (s)", kind: "int" },
      batch_size:              { label: "Batch size",         kind: "int" },
    },
  },
  mineru: {
    title: "MinerU (PDF parser)",
    description: "Local MinerU HTTP endpoint and parse options.",
    fields: {
      base_url:                { label: "Base URL",           kind: "text" },
      request_timeout_seconds: { label: "Request timeout (s)", kind: "int", hint: "Parsing can take minutes" },
      backend:                 { label: "Backend",            kind: "text", hint: "pipeline | vlm-engine | hybrid-engine" },
      parse_method:            { label: "Parse method",       kind: "text", hint: "auto | txt | ocr" },
      lang_list:               { label: "Language codes",     kind: "list-str", hint: "comma-separated, e.g. en,ch" },
      formula_enable:          { label: "Enable formulas",    kind: "bool" },
      table_enable:            { label: "Enable tables",      kind: "bool" },
    },
  },
  openalex: {
    title: "OpenAlex",
    description: "Polite-pool email + rate limits for the OpenAlex API.",
    fields: {
      mailto:                  { label: "Polite-pool email",  kind: "text" },
      api_key:                 { label: "API key",            kind: "text", hint: "optional; raises rate limit" },
      request_timeout_seconds: { label: "Request timeout (s)", kind: "int" },
      max_retries:             { label: "Max retries",        kind: "int" },
      search_enabled:          { label: "Search enabled",     kind: "bool" },
      search_per_page:         { label: "Search per page",    kind: "int" },
    },
  },
  semantic_scholar: {
    title: "Semantic Scholar",
    description: "S2 API client settings; api_key raises the rate limit.",
    fields: {
      base_url:                { label: "Base URL",           kind: "text" },
      api_key:                 { label: "API key",            kind: "text", hint: "optional S2_API_KEY; raises rate limit to 1 RPS" },
      request_timeout_seconds: { label: "Request timeout (s)", kind: "int" },
      max_retries:             { label: "Max retries",        kind: "int" },
      rate_limit_per_second:   { label: "Rate limit (req/s)", kind: "float", hint: "null = auto" },
      citations_limit:         { label: "Citations list cap", kind: "int" },
      fetch_on_sync:           { label: "Fetch on sync",      kind: "bool" },
      references_backfill_batch: { label: "References backfill batch", kind: "int" },
      citations_refresh_batch: { label: "Citations refresh batch", kind: "int" },
      search_enabled:          { label: "Search enabled",     kind: "bool" },
      search_per_page:         { label: "Search per page",    kind: "int" },
    },
  },
  arxiv: {
    title: "arXiv",
    description: "arXiv search & sync settings. arXiv asks for ≥3s between calls.",
    fields: {
      request_timeout_seconds:       { label: "Request timeout (s)", kind: "int" },
      max_retries:                   { label: "Max retries",         kind: "int" },
      max_results_per_query:         { label: "Max results / query", kind: "int" },
      delay_between_requests_seconds:{ label: "Delay between requests (s)", kind: "float" },
      search_enabled:                { label: "Search enabled",      kind: "bool" },
      search_per_page:               { label: "Search per page",     kind: "int" },
    },
  },
  crossref: {
    title: "Crossref",
    description:
      "Crossref REST API client. The polite-pool contact (mailto) is sent " +
      "in the User-Agent and bumps the free-tier rate limit from ~10 to " +
      "~50 req/s. Restart required for changes to take effect.",
    fields: {
      base_url:                { label: "Base URL",            kind: "text" },
      mailto:                  { label: "Polite-pool email",   kind: "text", hint: "User-Agent mailto; required for the polite pool" },
      request_timeout_seconds: { label: "Request timeout (s)", kind: "int" },
      max_retries:             { label: "Max retries",         kind: "int" },
      search_enabled:          { label: "Search enabled",      kind: "bool" },
      search_per_page:         { label: "Search per page",     kind: "int" },
    },
  },
  download: {
    title: "PDF download",
    description: "Network limits for the PDF downloader.",
    fields: {
      request_timeout_seconds: { label: "Request timeout (s)", kind: "int" },
      max_bytes:               { label: "Max bytes",           kind: "int", hint: "PDFs larger than this are skipped" },
      user_agent:              { label: "User agent",          kind: "text" },
    },
  },
  chunking: {
    title: "Chunking",
    description: "Tokens per chunk + overlap used by the embed step.",
    fields: {
      target_tokens:  { label: "Target tokens",  kind: "int" },
      overlap_tokens: { label: "Overlap tokens", kind: "int" },
      min_tokens:     { label: "Min tokens",     kind: "int" },
    },
  },
  schedule: {
    title: "Scheduler",
    description: "Cron jobs for sync, remote fill, publication check, wiki compile.",
    fields: {
      enabled:                       { label: "Master switch",                       kind: "bool" },
      sync_cron:                     { label: "Daily sync cron",                      kind: "cron" },
      remote_fill_enabled:           { label: "Remote fill enabled",                  kind: "bool" },
      remote_fill_cron:              { label: "Remote fill cron",                     kind: "cron" },
      publication_check_enabled:     { label: "Publication check enabled",            kind: "bool" },
      publication_check_cron:        { label: "Publication check cron",               kind: "cron" },
      wiki_compile_enabled:          { label: "Wiki compile enabled",                 kind: "bool" },
      wiki_compile_cron:             { label: "Wiki compile cron",                    kind: "cron" },
    },
  },
  storage: {
    title: "Storage",
    description: "Where Carrel writes papers, attachments, wiki pages.",
    fields: {
      root:               { label: "Root path",          kind: "text" },
      papers_subdir:      { label: "Papers subdir",      kind: "text" },
      attachments_subdir: { label: "Attachments subdir", kind: "text" },
      wiki_subdir:        { label: "Wiki subdir",        kind: "text" },
    },
  },
  http: {
    title: "HTTP server",
    description: "Bind address & port for the FastAPI server.",
    fields: {
      host: { label: "Host", kind: "text" },
      port: { label: "Port", kind: "int" },
    },
  },
  cors: {
    title: "CORS",
    description: "Origins allowed to call the API from a browser.",
    fields: {
      origins: { label: "Origins", kind: "list-str", hint: "comma-separated" },
    },
  },
};

// Order the cards render in. Restart-required ones come last so the user
// sees the safe-to-edit sections first.
const SECTION_ORDER = [
  "llm", "embeddings", "mineru", "openalex", "semantic_scholar",
  "arxiv", "crossref", "download", "chunking", "schedule",
  "storage", "http", "cors",
];

// -------- Helpers --------

function asBool(v: unknown): boolean {
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return v !== 0;
  if (typeof v === "string") return v === "true" || v === "1";
  return false;
}

function asString(v: unknown): string {
  if (v === null || v === undefined) return "";
  return String(v);
}

function isMasked(v: unknown): boolean {
  return v === "***";
}

function parseFieldDraft(kind: FieldKind, raw: string): unknown {
  switch (kind) {
    case "bool":
      return raw === "true";
    case "int": {
      const n = parseInt(raw, 10);
      return Number.isFinite(n) ? n : null;
    }
    case "float": {
      const n = parseFloat(raw);
      return Number.isFinite(n) ? n : null;
    }
    case "list-str":
      return raw
        .split(",")
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
    case "select":
    case "cron":
    case "text":
    default:
      return raw;
  }
}

function draftToInput(kind: FieldKind, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (isMasked(value)) return "";
  if (kind === "bool") return asBool(value) ? "true" : "false";
  if (kind === "list-str" && Array.isArray(value)) return value.join(", ");
  return asString(value);
}

// -------- Section card --------

interface SectionCardProps {
  name: string;
  data: SerialisedSection;
  onSaved: (next: SerialisedSection) => void;
}

function SectionCard({ name, data, onSaved }: SectionCardProps) {
  const spec = SECTION_SPECS[name];
  if (!spec) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{name}</CardTitle>
        </CardHeader>
        <CardContent>
          <pre className="overflow-x-auto rounded bg-muted/50 p-3 text-xs">
{JSON.stringify(data.values, null, 2)}
          </pre>
        </CardContent>
      </Card>
    );
  }

  const initial: Record<string, string> = {};
  for (const [field] of Object.entries(spec.fields)) {
    initial[field] = draftToInput(spec.fields[field].kind, data.values[field]);
  }
  const [draft, setDraft] = useState<Record<string, string>>(initial);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    if (!editing) {
      const next: Record<string, string> = {};
      for (const [field] of Object.entries(spec.fields)) {
        next[field] = draftToInput(spec.fields[field].kind, data.values[field]);
      }
      setDraft(next);
    }
  }, [data, editing, spec]);

  function onCancel() {
    const reset: Record<string, string> = {};
    for (const [field] of Object.entries(spec.fields)) {
      reset[field] = draftToInput(spec.fields[field].kind, data.values[field]);
    }
    setDraft(reset);
    setEditing(false);
    setErr(null);
  }
  async function onSave() {
    setSaving(true);
    setErr(null);
    try {
      const body: Record<string, unknown> = {};
      for (const [field, specEntry] of Object.entries(spec.fields)) {
        if (isMasked(data.values[field])) continue;
        const parsed = parseFieldDraft(specEntry.kind, draft[field] ?? "");
        if (parsed === data.values[field]) continue;
        body[field] = parsed;
      }
      if (Object.keys(body).length === 0) {
        setEditing(false);
        setSaving(false);
        return;
      }
      const next = await updateSettings({ [name]: body });
      onSaved(next.sections[name]);
      setEditing(false);
      setSavedAt(Date.now());
      setTimeout(() => setSavedAt(null), 2000);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
        <div className="space-y-1">
          <CardTitle className="text-base">{spec.title}</CardTitle>
          <p className="text-sm text-muted-foreground">{spec.description}</p>
          <div className="flex flex-wrap gap-2 pt-1">
            {data.requires_restart && (
              <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-900 dark:bg-amber-900/30 dark:text-amber-200">
                <AlertTriangle className="h-3 w-3" />
                Restart required
              </span>
            )}
            {Object.keys(data.env_overrides).length > 0 && (
              <TooltipProvider delayDuration={150}>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="inline-flex items-center gap-1 rounded-full bg-blue-100 px-2 py-0.5 text-xs text-blue-900 dark:bg-blue-900/30 dark:text-blue-200">
                      <Lock className="h-3 w-3" />
                      {Object.keys(data.env_overrides).length === 1
                        ? `1 field overridden by .env`
                        : `${Object.keys(data.env_overrides).length} fields overridden by .env`}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-md">
                    <div className="space-y-1">
                      <div className="font-medium">Overridden by .env</div>
                      <ul className="space-y-0.5 text-xs">
                        {Object.entries(data.env_overrides).map(([field, ov]) => (
                          <li key={field}>
                            <code className="font-mono">{field}</code>
                            {" ← "}
                            <code className="font-mono">{ov.env_var}</code>
                            {ov.env_value ? ` = ${ov.env_value}` : " (set, value hidden)"}
                          </li>
                        ))}
                      </ul>
                    </div>
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}
          </div>
        </div>
        <div className="flex shrink-0 gap-2">
          {editing ? (
            <>
              <Button size="sm" variant="ghost" onClick={onCancel} disabled={saving}>
                <X className="mr-1 h-4 w-4" />
                Cancel
              </Button>
              <Button size="sm" onClick={onSave} disabled={saving}>
                <Save className="mr-1 h-4 w-4" />
                {saving ? "Saving…" : "Save"}
              </Button>
            </>
          ) : (
            <Button size="sm" variant="outline" onClick={() => setEditing(true)}>
              <RotateCcw className="mr-1 h-4 w-4" />
              Edit
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {err && (
          <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-800/50 dark:bg-red-900/20 dark:text-red-200">
            {err}
          </div>
        )}
        {savedAt && data.requires_restart && !err && (
          <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800/50 dark:bg-amber-900/20 dark:text-amber-200">
            Saved to <code>data/config.yaml</code>. Restart Carrel to apply.
          </div>
        )}
        {savedAt && !err && !data.requires_restart && (
          <div className="rounded-md border border-green-300 bg-green-50 px-3 py-2 text-sm text-green-800 dark:border-green-800/50 dark:bg-green-900/20 dark:text-green-200">
            Saved.
          </div>
        )}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {Object.entries(spec.fields).map(([field, fieldSpec]) => {
            const masked = isMasked(data.values[field]);
            const envOverride = data.env_overrides[field];
            const inputId = `settings-${name}-${field}`;
            const disabled = !editing || !!envOverride;
            return (
              <div key={field} className="space-y-1">
                <label
                  htmlFor={inputId}
                  className="flex items-center gap-2 text-sm font-medium"
                >
                  {fieldSpec.label}
                  {envOverride && (
                    <TooltipProvider delayDuration={150}>
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span className="inline-flex items-center gap-1 rounded-full bg-blue-100 px-1.5 py-0.5 text-xs text-blue-900 dark:bg-blue-900/30 dark:text-blue-200">
                            <Lock className="h-3 w-3" />
                            from {envOverride.env_var}
                          </span>
                        </TooltipTrigger>
                        <TooltipContent>
                          This value comes from the env var{" "}
                          <code>{envOverride.env_var}</code>
                          {envOverride.env_value
                            ? <> (<code>{envOverride.env_value}</code>)</>
                            : " (value hidden — secret)"}
                          . Edit <code>.env</code> to change.
                        </TooltipContent>
                      </Tooltip>
                    </TooltipProvider>
                  )}
                </label>
                {envOverride && envOverride.env_value && (
                  <p className="text-xs text-muted-foreground font-mono">
                    via {envOverride.env_var} = {envOverride.env_value}
                  </p>
                )}
                {masked ? (
                  <div className="flex items-center gap-2">
                    <code className="rounded bg-muted px-2 py-1 text-sm">***</code>
                    <span className="text-xs text-muted-foreground">set (not shown)</span>
                  </div>
                ) : fieldSpec.kind === "bool" ? (
                  <select
                    id={inputId}
                    className="w-full rounded-md border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                    value={editing ? (draft[field] ?? "false") : asBool(data.values[field]) ? "true" : "false"}
                    onChange={(e) => setDraft((d) => ({ ...d, [field]: e.target.value }))}
                    disabled={disabled}
                  >
                    <option value="true">true</option>
                    <option value="false">false</option>
                  </select>
                ) : fieldSpec.kind === "select" ? (
                  <select
                    id={inputId}
                    className="w-full rounded-md border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                    value={editing ? (draft[field] ?? "") : asString(data.values[field] ?? "")}
                    onChange={(e) => setDraft((d) => ({ ...d, [field]: e.target.value }))}
                    disabled={disabled}
                  >
                    {(fieldSpec.options ?? []).map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                ) : fieldSpec.kind === "cron" ? (
                  <div className="space-y-1">
                    <input
                      id={inputId}
                      type="text"
                      className="w-full rounded-md border bg-background px-2 py-1.5 font-mono text-sm disabled:opacity-60"
                      value={editing ? (draft[field] ?? "") : asString(data.values[field] ?? "")}
                      onChange={(e) => setDraft((d) => ({ ...d, [field]: e.target.value }))}
                      disabled={disabled}
                    />
                    <p className="text-xs text-muted-foreground">
                      {humanizeCron(asString(data.values[field] ?? "")) || "—"}
                    </p>
                  </div>
                ) : fieldSpec.kind === "int" ? (
                  <input
                    id={inputId}
                    type="number"
                    step={1}
                    className="w-full rounded-md border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                    value={editing ? (draft[field] ?? "") : asString(data.values[field])}
                    onChange={(e) => setDraft((d) => ({ ...d, [field]: e.target.value }))}
                    disabled={disabled}
                  />
                ) : fieldSpec.kind === "float" ? (
                  <input
                    id={inputId}
                    type="number"
                    step={0.1}
                    className="w-full rounded-md border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                    value={editing ? (draft[field] ?? "") : asString(data.values[field])}
                    onChange={(e) => setDraft((d) => ({ ...d, [field]: e.target.value }))}
                    disabled={disabled}
                  />
                ) : fieldSpec.kind === "list-str" ? (
                  <input
                    id={inputId}
                    type="text"
                    placeholder="comma-separated"
                    className="w-full rounded-md border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                    value={editing ? (draft[field] ?? "") : (Array.isArray(data.values[field]) ? (data.values[field] as unknown[]).join(", ") : asString(data.values[field] ?? ""))}
                    onChange={(e) => setDraft((d) => ({ ...d, [field]: e.target.value }))}
                    disabled={disabled}
                  />
                ) : (
                  <input
                    id={inputId}
                    type="text"
                    className="w-full rounded-md border bg-background px-2 py-1.5 text-sm disabled:opacity-60"
                    value={editing ? (draft[field] ?? "") : asString(data.values[field] ?? "")}
                    onChange={(e) => setDraft((d) => ({ ...d, [field]: e.target.value }))}
                    disabled={disabled}
                  />
                )}
                {fieldSpec.hint && (
                  <p className="text-xs text-muted-foreground">{fieldSpec.hint}</p>
                )}
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

// -------- Environment (.env) summary --------

function EnvironmentCard({ env }: { env: EnvEntry[] }) {
  const [showSecrets, setShowSecrets] = useState(false);
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
        <div className="space-y-1">
          <CardTitle className="text-base">Environment (.env)</CardTitle>
          <p className="text-sm text-muted-foreground">
            Read-only summary. Set values in <code>.env</code>; the running server picks them up at startup.
          </p>
        </div>
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={showSecrets}
            onChange={(e) => setShowSecrets(e.target.checked)}
          />
          Show secret status
        </label>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">Key</th>
                <th className="px-3 py-2 text-left">Name</th>
                <th className="px-3 py-2 text-left">Status</th>
                <th className="px-3 py-2 text-left">Value</th>
              </tr>
            </thead>
            <tbody>
              {env.map((e) => (
                <tr key={e.name} className="border-t">
                  <td className="px-3 py-2 font-mono text-xs">{e.name}</td>
                  <td className="px-3 py-2">{e.label}</td>
                  <td className="px-3 py-2">
                    {e.is_set ? (
                      <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2 py-0.5 text-xs text-green-800 dark:bg-green-900/30 dark:text-green-200">
                        set
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                        not set
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">
                    {e.is_secret ? (
                      showSecrets ? "(hidden)" : "—"
                    ) : e.value !== null && e.value !== undefined ? (
                      e.value
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

// -------- Subscriptions summary --------

function SubscriptionsSummaryCard({ items }: { items: Subscription[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Subscriptions</CardTitle>
      </CardHeader>
      <CardContent className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {items.length} active subscription{items.length === 1 ? "" : "s"}.
        </p>
        <Link
          to="/subscriptions"
          className={buttonVariants({ size: "sm", variant: "outline" })}
        >
          Manage in Subscriptions →
        </Link>
      </CardContent>
    </Card>
  );
}

// -------- Page --------

export default function Settings() {
  const [data, setData] = useState<SettingsData | null>(null);
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [settings, subsRes] = await Promise.all([
          getSettings(),
          listSubscriptions().catch(() => [] as Subscription[]),
        ]);
        if (!cancelled) {
          setData(settings);
          setSubs(subsRes);
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const sections = useMemo(() => {
    if (!data) return [] as Array<[string, SerialisedSection]>;
    return SECTION_ORDER
      .filter((name) => name in data.sections)
      .map((name) => [name, data.sections[name]] as [string, SerialisedSection]);
  }, [data]);

  if (loading) {
    return (
      <main className="container max-w-screen-2xl py-8">
        <p className="text-sm text-muted-foreground">Loading settings…</p>
      </main>
    );
  }
  if (err || !data) {
    return (
      <main className="container max-w-screen-2xl py-8">
        <h1 className="text-2xl font-semibold">Settings</h1>
        <div className="mt-4 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800">
          Failed to load settings: {err ?? "unknown error"}
        </div>
      </main>
    );
  }

  return (
    <main className="container max-w-screen-2xl space-y-6 py-8">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Edits write to <code className="rounded bg-muted px-1.5 py-0.5">{data.yaml_path}</code> and apply
          where noted. Restart-required sections persist to disk but do not affect the running process.
        </p>
        {data.restart_required_sections.length > 0 && (
          <p className="text-xs text-amber-700 dark:text-amber-300">
            Restart-required sections: {data.restart_required_sections.join(", ")}.
          </p>
        )}
      </div>

      {sections.map(([name, section]) => (
        <SectionCard
          key={name}
          name={name}
          data={section}
          onSaved={(next) =>
            setData((prev) => (prev ? { ...prev, sections: { ...prev.sections, [name]: next } } : prev))
          }
        />
      ))}

      <SubscriptionsSummaryCard items={subs} />
      <EnvironmentCard env={data.env} />
    </main>
  );
}
