import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeRaw from "rehype-raw";
import "katex/dist/katex.min.css";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadMessage,
  ThreadPrimitive,
  useLocalRuntime,
  type ChatModelAdapter,
  type TextMessagePartProps,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { BookOpen, Send, Square } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { getChatMessages, saveChatMessages, type ChatTurn } from "@/api/client";
import rehypeRawMath from "./rehypeRawMath";

// ---------------------------------------------------------------------------
// Types — mirror the backend SSE frames in carrel/api/chat.py
// ---------------------------------------------------------------------------

interface ChatRequestMessage {
  role: "user" | "assistant";
  content: string;
}

type ServerEvent =
  | { sources: string[] }
  | { t: string }
  | { error: string };

// ---------------------------------------------------------------------------
// Persistence — the transcript lives on the server (one row per turn) so it
// follows the user across devices and browsers. The client loads it on mount
// and PUTs the full ordered turn list whenever the conversation settles.
// ---------------------------------------------------------------------------

function toMessageLikes(turns: ChatTurn[]): ThreadMessageLike[] {
  return turns
    .filter((m) => (m.role === "user" || m.role === "assistant") && m.content)
    .map((m, i) => ({ id: `hist-${i}`, role: m.role, content: m.content }));
}

function sameTurns(a: ChatTurn[], b: ChatTurn[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((t, i) => t.role === b[i].role && t.content === b[i].content);
}

// ---------------------------------------------------------------------------
// Flatten assistant-ui messages -> our wire format
// ---------------------------------------------------------------------------

function messageText(m: ThreadMessage): string {
  return m.content
    .filter((p): p is { type: "text"; text: string } => p.type === "text")
    .map((p) => p.text)
    .join("");
}

function toWireMessages(messages: readonly ThreadMessage[]): ChatRequestMessage[] {
  const out: ChatRequestMessage[] = [];
  for (const m of messages) {
    if (m.role !== "user" && m.role !== "assistant") continue;
    const text = messageText(m);
    if (!text) continue;
    // Collapse consecutive same-role turns defensively.
    const last = out[out.length - 1];
    if (last && last.role === m.role) last.content += `\n\n${text}`;
    else out.push({ role: m.role, content: text });
  }
  return out;
}

// ---------------------------------------------------------------------------
// SSE parsing: split a ReadableStream into ServerEvent objects
// ---------------------------------------------------------------------------

async function* parseSSE(
  reader: ReadableStreamDefaultReader<Uint8Array>,
): AsyncGenerator<ServerEvent | "done"> {
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Frames are separated by a blank line.
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const rawFrame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of rawFrame.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (payload === "[DONE]") {
          yield "done";
          continue;
        }
        try {
          yield JSON.parse(payload) as ServerEvent;
        } catch {
          // ignore malformed frame
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Markdown answer — reuse the project's remark/rehype plugin stack so math
// (including $...$ inside raw HTML) renders exactly as it does in the reader.
// ---------------------------------------------------------------------------

function MarkdownAnswer(props: TextMessagePartProps) {
  return (
    <div className="md-body text-sm leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeRawMath, rehypeRaw, rehypeKatex]}
      >
        {props.text}
      </ReactMarkdown>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Message bubbles
// ---------------------------------------------------------------------------

function UserBubble() {
  return (
    <MessagePrimitive.Root className="flex justify-end">
      <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-sm bg-primary px-3.5 py-2 text-sm text-primary-foreground">
        <MessagePrimitive.Content />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantBubble() {
  return (
    <MessagePrimitive.Root className="flex flex-col gap-1">
      <div className="text-xs font-medium text-muted-foreground">Assistant</div>
      <div className="rounded-2xl rounded-tl-sm border bg-card px-3.5 py-2.5 text-sm">
        <MessagePrimitive.Content components={{ Text: MarkdownAnswer }} />
      </div>
    </MessagePrimitive.Root>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface PaperChatProps {
  paperId: string;
  hasMarkdown: boolean;
}

// ---------------------------------------------------------------------------
// Inner thread — owns the local runtime; remounted (via key) to clear history.
// ---------------------------------------------------------------------------

interface ChatThreadProps {
  paperId: string;
  disabled: boolean;
  sources: string[] | null;
  onSources: (sources: string[] | null) => void;
  initialTurns: ChatTurn[];
}

function ChatThread({ paperId, disabled, sources, onSources, initialTurns }: ChatThreadProps) {
  // Snapshot saved history once per remount (paper change / explicit clear).
  const initialMessages = useMemo(() => toMessageLikes(initialTurns), [initialTurns]);

  const adapter = useMemo<ChatModelAdapter>(
    () => ({
      async *run({ messages, abortSignal }) {
        onSources(null);
        const wire = toWireMessages(messages);
        const res = await fetch(`/api/papers/${encodeURIComponent(paperId)}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: wire }),
          signal: abortSignal,
        });

        if (!res.ok || !res.body) {
          const text = await res.text().catch(() => res.statusText);
          throw new Error(text || `Request failed (${res.status})`);
        }

        let answer = "";
        const reader = res.body.getReader();
        for await (const evt of parseSSE(reader)) {
          if (evt === "done") break;
          if ("sources" in evt) {
            onSources(evt.sources);
          } else if ("t" in evt) {
            answer += evt.t;
            // The local runtime REPLACES message content on each yield (its
            // initial parts list is fixed at run start), so we re-yield the
            // whole accumulated answer rather than just the new delta.
            yield { content: [{ type: "text", text: answer }] };
          } else if ("error" in evt) {
            throw new Error(evt.error);
          }
        }
        if (!answer) {
          yield { content: [{ type: "text", text: "（没有收到回复）" }] };
        }
      },
    }),
    [paperId, onSources],
  );

  const runtime = useLocalRuntime(adapter, { initialMessages });

  // Persist the transcript server-side whenever it settles. We skip writes
  // while a run is streaming (each token would otherwise trigger a PUT) and
  // flush shortly after running goes false. Only PUT when the turn list
  // actually changed, and ignore results from superseded saves.
  const saveTimer = useRef<number | null>(null);
  const lastSaved = useRef<ChatTurn[]>(initialTurns);
  const saveSeq = useRef(0);
  useEffect(() => {
    lastSaved.current = initialTurns;
  }, [initialTurns]);

  useEffect(() => {
    const scheduleSave = () => {
      const state = runtime.thread.getState();
      if (state.isRunning) return;
      const wire = toWireMessages(state.messages);
      if (sameTurns(wire, lastSaved.current)) return;
      if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
      saveTimer.current = window.setTimeout(() => {
        saveTimer.current = null;
        const mySeq = ++saveSeq.current;
        const snapshot = wire;
        saveChatMessages(paperId, snapshot)
          .then(() => {
            if (mySeq !== saveSeq.current) return; // a newer save superseded
            lastSaved.current = snapshot;
          })
          .catch((e) => {
            // Transient failure — transcript stays in memory and saves again
            // on the next settle.
            console.warn("chat: save failed", e);
          });
      }, 800);
    };
    const unsubscribe = runtime.thread.subscribe(scheduleSave);
    return () => {
      unsubscribe();
      if (saveTimer.current !== null) window.clearTimeout(saveTimer.current);
    };
  }, [runtime, paperId]);

  // Best-effort flush on unmount / tab hide so the last answer isn't lost.
  // Uses sendBeacon-style PUT via fetch keepalive; falls back silently.
  useEffect(() => {
    const flush = () => {
      const state = runtime.thread.getState();
      if (state.isRunning) return;
      const wire = toWireMessages(state.messages);
      if (sameTurns(wire, lastSaved.current)) return;
      try {
        fetch(`/api/papers/${encodeURIComponent(paperId)}/chat/messages`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: wire }),
          keepalive: true,
        }).catch(() => {});
        lastSaved.current = wire;
      } catch {
        // ignore
      }
    };
    window.addEventListener("beforeunload", flush);
    document.addEventListener("visibilitychange", flush);
    return () => {
      window.removeEventListener("beforeunload", flush);
      document.removeEventListener("visibilitychange", flush);
    };
  }, [runtime, paperId]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="flex min-h-0 flex-1 flex-col">
        <ThreadPrimitive.Viewport className="flex flex-1 flex-col gap-4 overflow-y-auto pr-1">
          <ThreadPrimitive.Empty>
            <div className="flex flex-1 items-center justify-center py-10 text-center text-sm text-muted-foreground">
              {disabled
                ? "Download & parse the paper first, then ask questions about it."
                : "Ask anything about this paper. Answers cite the sections used."}
            </div>
          </ThreadPrimitive.Empty>
          <ThreadPrimitive.Messages
            components={{ UserMessage: UserBubble, AssistantMessage: AssistantBubble }}
          />
        </ThreadPrimitive.Viewport>

        {sources && sources.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {sources.map((s, i) => (
              <span
                key={i}
                className="inline-flex max-w-full truncate rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
                title={s}
              >
                {s}
              </span>
            ))}
          </div>
        )}

        <ComposerPrimitive.Root className="mt-auto flex items-end gap-2 rounded-lg border bg-background p-2 focus-within:ring-1 focus-within:ring-ring">
          <ComposerPrimitive.Input
            rows={5}
            autoFocus
            disabled={disabled}
            placeholder={disabled ? "Parse the paper to enable chat…" : "Ask about this paper…"}
            className="h-[7.5rem] max-h-[7.5rem] flex-1 resize-none bg-transparent px-1 py-1 text-sm leading-relaxed outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed"
          />
          <ThreadPrimitive.If running={false}>
            <ComposerPrimitive.Send asChild>
              <Button size="icon" className="h-8 w-8 shrink-0" disabled={disabled}>
                <Send className="h-4 w-4" />
              </Button>
            </ComposerPrimitive.Send>
          </ThreadPrimitive.If>
          <ThreadPrimitive.If running>
            <ComposerPrimitive.Cancel asChild>
              <Button size="icon" variant="outline" className="h-8 w-8 shrink-0">
                <Square className="h-4 w-4" />
              </Button>
            </ComposerPrimitive.Cancel>
          </ThreadPrimitive.If>
        </ComposerPrimitive.Root>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}

// ---------------------------------------------------------------------------
// Outer card — header, source chips, and the remountable inner thread
// ---------------------------------------------------------------------------

export function PaperChat({ paperId, hasMarkdown }: PaperChatProps) {
  // Sources (chapter headings) for the latest answer, shown as small chips.
  const [sources, setSources] = useState<string[] | null>(null);
  // Server-persisted transcript, loaded on mount / paper change.
  const [initialTurns, setInitialTurns] = useState<ChatTurn[]>([]);
  const [historyReady, setHistoryReady] = useState(false);
  // Bumping this key remounts the whole runtime thread (e.g. after Clear).
  const [threadKey, setThreadKey] = useState(0);

  const disabled = !hasMarkdown;

  useEffect(() => {
    let cancelled = false;
    setHistoryReady(false);
    getChatMessages(paperId)
      .then((res) => {
        if (cancelled) return;
        setInitialTurns(
          res.messages
            .filter((m) => m.role === "user" || m.role === "assistant")
            .map((m) => ({ role: m.role, content: m.content })),
        );
      })
      .catch(() => {
        if (cancelled) return;
        setInitialTurns([]);
      })
      .finally(() => {
        if (!cancelled) setHistoryReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [paperId]);

  async function onClear() {
    setSources(null);
    setInitialTurns([]);
    // Optimistically reset the runtime; the server state follows.
    setThreadKey((k) => k + 1);
    try {
      await saveChatMessages(paperId, []);
    } catch (e) {
      console.warn("chat: clear failed on server", e);
    }
  }

  return (
    <Card className="flex h-[70vh] flex-col xl:h-full xl:rounded-none xl:border-0 xl:shadow-none">
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <BookOpen className="h-4 w-4" />
          Chat with this paper
        </CardTitle>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs"
          onClick={onClear}
        >
          Clear
        </Button>
      </CardHeader>
      <CardContent className="flex min-h-0 flex-1 flex-col gap-3 p-4 pt-0">
        {!historyReady ? (
          <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">
            Loading conversation…
          </div>
        ) : (
          <ChatThread
            key={`${paperId}-${threadKey}`}
            paperId={paperId}
            disabled={disabled}
            sources={sources}
            onSources={setSources}
            initialTurns={initialTurns}
          />
        )}
      </CardContent>
    </Card>
  );
}
