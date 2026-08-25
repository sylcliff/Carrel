import { useMemo } from "react";
import { Link } from "react-router-dom";
import rehypeWikiLinks from "./rehypeWikiLinks";

import {
  ChatPanel,
  type ChatPanelConfig,
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
  // Memoize so the history-load effect only re-runs when `pagesExist`
  // actually flips (e.g. the user compiles the wiki mid-session).
  const config = useMemo<ChatPanelConfig<WikiSource>>(
    () => ({
      ...WIKI_CHAT_CONFIG,
      enabled: pagesExist,
    }),
    [pagesExist],
  );
  return <ChatPanel config={config} />;
}
