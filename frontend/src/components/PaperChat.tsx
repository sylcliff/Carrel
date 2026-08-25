import { useMemo } from "react";

import {
  ChatPanel,
  type ChatPanelConfig,
} from "./ChatPanel";
import {
  getChatMessages,
  saveChatMessages,
} from "@/api/client";

// ---------------------------------------------------------------------------
// PaperChat — thin wrapper over the shared ChatPanel.
//
// Everything chat-shaped (assistant-ui thread, SSE, persistence, math + raw
// HTML rendering) is owned by ``ChatPanel``. The per-paper variant is
// different from the wiki variant in exactly four ways:
//   1. Endpoints — paper chat is per-paper, wiki chat is a single global
//      transcript.
//   2. Source chips — paper answers show chapter headings as plain chips;
//      wiki answers link to wiki pages.
//   3. No extra rehype plugin — paper answers don't need to rewrite links
//      to anything inside the app.
//   4. Responsive card sizing — the right column only goes edge-to-edge
//      at ``xl:`` so the paper layout still has visible borders on lg/md.
// ---------------------------------------------------------------------------

export interface PaperChatProps {
  paperId: string;
  hasMarkdown: boolean;
}

const PAPER_CHAT_CONFIG: Omit<
  ChatPanelConfig<string>,
  "loadMessages" | "saveMessages" | "scopeKey" | "enabled" | "chatEndpoint" | "messagesEndpoint"
> = {
  logLabel: "paper chat",
  renderSources: (sources) => (
    <>
      {sources.map((s, i) => (
        <span
          key={i}
          className="inline-flex max-w-full truncate rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
          title={s}
        >
          {s}
        </span>
      ))}
    </>
  ),
  title: "Chat with this paper",
  emptyText: {
    enabled: "Ask anything about this paper. Answers cite the sections used.",
    disabled: "Download & parse the paper first, then ask questions about it.",
  },
  placeholder: {
    enabled: "Ask about this paper…",
    disabled: "Parse the paper to enable chat…",
  },
  cardClassName: "flex h-[70vh] flex-col xl:h-full xl:rounded-none xl:border-0 xl:shadow-none",
};

export function PaperChat({ paperId, hasMarkdown }: PaperChatProps) {
  // Memoize so the history-load effect only re-runs when the paper (or its
  // markdown availability) actually changes — not on every parent render.
  const config = useMemo<ChatPanelConfig<string>>(
    () => ({
      ...PAPER_CHAT_CONFIG,
      scopeKey: paperId,
      enabled: hasMarkdown,
      chatEndpoint: `/api/papers/${encodeURIComponent(paperId)}/chat`,
      messagesEndpoint: `/api/papers/${encodeURIComponent(paperId)}/chat/messages`,
      loadMessages: () => getChatMessages(paperId),
      saveMessages: (messages) => saveChatMessages(paperId, messages),
    }),
    [paperId, hasMarkdown],
  );
  return <ChatPanel config={config} />;
}
