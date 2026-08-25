import { useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Wrench } from "lucide-react";
import rehypeWikiLinks from "./rehypeWikiLinks";

import {
  ChatPanel,
  type ChatPanelConfig,
  type ToolEvent,
} from "./ChatPanel";
import {
  getWikiChatMessages,
  saveWikiChatMessages,
  type WikiSource,
} from "@/api/client";

// ---------------------------------------------------------------------------
// WikiChat — thin wrapper over the shared ChatPanel.
//
// Everything chat-shaped (assistant-ui thread, SSE, persistence, math + raw
// HTML rendering) is owned by ``ChatPanel``. The wiki variant is different
// from the per-paper variant in exactly four ways:
//   1. Endpoints — wiki chat is a single global transcript, not per-paper.
//   2. Source chips — wiki answers link to wiki pages, paper answers just
//      show chapter headings as plain chips.
//   3. Extra rehype plugin — ``rehypeWikiLinks`` rewrites ``concepts/foo.md``
//      references inside the answer into in-app routes.
//   4. Responsive card sizing — the wiki surface goes edge-to-edge at ``lg:``,
//      while the per-paper right column only does so at ``xl:``.
// ---------------------------------------------------------------------------

const WIKI_CHAT_CONFIG: ChatPanelConfig<WikiSource> = {
  scopeKey: "wiki",
  logLabel: "wiki chat",
  enabled: true, // overridden per-render via the panel's `enabled` prop
  chatEndpoint: "/api/wiki/chat",
  messagesEndpoint: "/api/wiki/chat/messages",
  loadMessages: getWikiChatMessages,
  saveMessages: saveWikiChatMessages,
  extraRehypePlugins: [rehypeWikiLinks],
  renderSources: (sources) => (
    <>
      {sources.map((s) => (
        <Link
          key={`${s.kind}-${s.slug}`}
          to={`/wiki/${s.kind}/${s.slug}`}
          title={`${s.kind}:${s.slug}`}
          className="inline-flex max-w-full truncate rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-muted/60 hover:text-foreground"
        >
          {s.title || `${s.kind}:${s.slug}`}
        </Link>
      ))}
    </>
  ),
  title: "Chat with the wiki",
  emptyText: {
    enabled: "Ask anything about the wiki. Answers cite the pages used.",
    disabled: "Compile the wiki first, then ask questions about it.",
  },
  placeholder: {
    enabled: "Ask about the wiki…",
    disabled: "Compile the wiki to enable chat…",
  },
  cardClassName: "flex h-[70vh] flex-col xl:h-full xl:rounded-none xl:border-0 xl:shadow-none",
};

export interface WikiChatProps {
  pagesExist: boolean;
}

export function WikiChat({ pagesExist }: WikiChatProps) {
  // Tool events from the most recent run. Reset to [] on each new
  // run start (the chat panel always passes a fresh empty list before
  // the first tool frame arrives, so we treat empty as "nothing to show").
  const [toolEvents, setToolEvents] = useState<ToolEvent[]>([]);
  const handleToolEvents = useCallback((events: ToolEvent[]) => {
    setToolEvents(events);
  }, []);
  // Memoize so the history-load effect only re-runs when `pagesExist`
  // actually flips (e.g. the user compiles the wiki mid-session).
  const config = useMemo<ChatPanelConfig<WikiSource>>(
    () => ({
      ...WIKI_CHAT_CONFIG,
      enabled: pagesExist,
      onToolEvents: handleToolEvents,
    }),
    [pagesExist, handleToolEvents],
  );
  return (
    <div className="flex h-full flex-col gap-2">
      <ChatPanel config={config} />
      <ToolEventsStrip events={toolEvents} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// ToolEventsStrip — collapsible strip below the chat card that surfaces
// the most recent run's tool calls. Hidden when no tools were used so
// the wiki-chat surface looks identical to before for the common case.
// ---------------------------------------------------------------------------

function shortToolName(name: string): string {
  // The server prefixes ``<server>__`` to avoid name collisions across
  // MCP servers; the human-facing label is just the tool portion.
  const idx = name.indexOf("__");
  return idx >= 0 ? name.slice(idx + 2) : name;
}

function formatArgs(args: Record<string, unknown>): string {
  try {
    const json = JSON.stringify(args);
    return json.length > 80 ? json.slice(0, 77) + "…" : json;
  } catch {
    return "";
  }
}

function ToolEventsStrip({ events }: { events: ToolEvent[] }) {
  if (events.length === 0) return null;
  return (
    <details className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
      <summary className="flex cursor-pointer items-center gap-1.5 select-none">
        <Wrench className="h-3.5 w-3.5" />
        <span>
          Used {events.length} tool{events.length === 1 ? "" : "s"} for this answer
        </span>
      </summary>
      <ul className="mt-2 space-y-1.5">
        {events.map((evt, i) => (
          <li
            key={`${evt.name}-${i}`}
            className="rounded border border-border/60 bg-background/60 px-2 py-1.5"
          >
            <div className="flex items-center gap-1.5">
              <code className="font-mono text-[11px] text-foreground">
                {shortToolName(evt.name)}
              </code>
              <code className="truncate font-mono text-[11px] text-muted-foreground">
                ({formatArgs(evt.args)})
              </code>
              {evt.isError && (
                <span className="ml-auto rounded bg-destructive/15 px-1.5 py-0.5 text-[10px] font-medium text-destructive">
                  error
                </span>
              )}
            </div>
            <pre className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap break-all font-mono text-[11px] text-muted-foreground">
              {evt.content.length > 600
                ? evt.content.slice(0, 600) + "…"
                : evt.content}
            </pre>
          </li>
        ))}
      </ul>
    </details>
  );
}
