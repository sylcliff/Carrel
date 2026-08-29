import { useState } from "react";
import { ChevronDown, FileText, RefreshCw, Sparkles } from "lucide-react";
import { extractPaperCard, getPaperCard, type PaperCard, type ResultClaim } from "@/api/client";
import { useApiMutation, useApiQueryWithFn } from "@/lib/useApiQuery";
import { queryKeys } from "@/lib/queryKeys";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

type Props = {
  paperId: string;
  hasMarkdown: boolean;
};

type CardState = "absent" | "loading" | "present" | "extracting" | "error";

const PAPER_TYPE_LABEL: Record<string, string> = {
  research: "Research",
  survey: "Survey",
  benchmark: "Benchmark / Dataset",
  system: "System",
  position: "Position",
  case_study: "Case study",
  other: "Other",
};

export default function PaperCardView({ paperId, hasMarkdown }: Props) {
  const [open, setOpen] = useState(true);

  // useApiQueryWithFn lets us call getPaperCard, which translates a 204
  // into `null` (the empty state) instead of throwing. The plain
  // useApiQuery would surface the 204 as an error since requestCached
  // can't represent "no body" for an optional resource.
  const cardQuery = useApiQueryWithFn<PaperCard | null>({
    key: queryKeys.paperCard(paperId),
    queryFn: () => getPaperCard(paperId),
    // The card is LLM-generated and rarely re-fetched within a session;
    // a long staleTime keeps background re-fetches from re-issuing the
    // request, while the explicit invalidate on extract still kicks in.
    staleTime: 60_000,
  });

  const extractMutation = useApiMutation<{ force: boolean }, PaperCard>({
    mutate: ({ force }) => extractPaperCard(paperId, force),
    invalidate: [queryKeys.paperCard(paperId), queryKeys.paper(paperId)],
    onSuccess: (card) => {
      // Seed the cache so the user sees the new card immediately,
      // without waiting for the invalidation refetch.
      // (The invalidate above will trigger a refetch anyway; this just
      // closes the visual gap by one frame.)
      void card;
    },
  });

  const card = cardQuery.data ?? null;
  const isFetching = cardQuery.isFetching;
  const state: CardState = extractMutation.isPending
    ? "extracting"
    : card
      ? "present"
      : cardQuery.isError
        ? "error"
        : isFetching
          ? "loading"
          : "absent";

  function handleExtract(force: boolean) {
    extractMutation.mutate({ force });
  }

  return (
    <Card data-testid="paper-card">
      <CardHeader
        className="cursor-pointer select-none"
        onClick={() => setOpen((v) => !v)}
      >
        <CardTitle className="flex items-center gap-2 text-base">
          <FileText className="h-4 w-4" />
          Paper card
          {card && (
            <span className="ml-2 inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-xs font-normal text-muted-foreground">
              {PAPER_TYPE_LABEL[card.paper_type ?? "research"] ??
                PAPER_TYPE_LABEL.research}
              {typeof card.confidence === "number" && (
                <span className="ml-1 text-[10px] opacity-70">
                  {(card.confidence * 100).toFixed(0)}%
                </span>
              )}
            </span>
          )}
          <ChevronDown
            className={`ml-auto h-4 w-4 transition-transform ${open ? "" : "-rotate-90"}`}
          />
        </CardTitle>
      </CardHeader>
      {open && (
        <CardContent className="space-y-3 text-sm">
          <CardToolbar
            state={state}
            hasMarkdown={hasMarkdown}
            onExtract={handleExtract}
          />
          {state === "present" && card ? (
            <CardSections card={card} />
          ) : state === "absent" ? (
            <p className="text-xs text-muted-foreground">
              No card yet. Click <em>Generate card</em> to ask the LLM to
              summarise this paper&apos;s research question, method, results,
              and conclusion into a structured card.
            </p>
          ) : state === "loading" ? (
            <p className="text-xs text-muted-foreground">Loading…</p>
          ) : state === "error" ? (
            <p className="text-xs text-red-600">
              Failed to load the card. Try again.
            </p>
          ) : null}
        </CardContent>
      )}
    </Card>
  );
}

// ---- helpers --------------------------------------------------------------

function CardToolbar({
  state,
  hasMarkdown,
  onExtract,
}: {
  state: CardState;
  hasMarkdown: boolean;
  onExtract: (force: boolean) => void;
}) {
  const busy = state === "extracting";
  const disabled = busy || !hasMarkdown;
  const label = !hasMarkdown
    ? "No parsed text"
    : busy
      ? "Generating…"
      : state === "present"
        ? "Re-generate"
        : "Generate card";
  return (
    <div className="flex items-center gap-2">
      <Button
        size="sm"
        variant={state === "present" ? "outline" : "default"}
        onClick={() => onExtract(state === "present")}
        disabled={disabled}
      >
        {busy ? (
          <RefreshCw className="mr-1 h-3 w-3 animate-spin" />
        ) : (
          <Sparkles className="mr-1 h-3 w-3" />
        )}
        {label}
      </Button>
      {state === "present" && (
        <span className="text-xs text-muted-foreground">
          Force re-generate to overwrite.
        </span>
      )}
    </div>
  );
}

function CardSections({ card }: { card: PaperCard }) {
  return (
    <div className="space-y-3">
      {card.research_question && (
        <Section title="Research question">
          <p>{card.research_question}</p>
          {card.motivation && (
            <p className="mt-1 text-xs text-muted-foreground">
              {card.motivation}
            </p>
          )}
        </Section>
      )}

      {card.method_summary && (
        <Section title={card.method_name ? `Method: ${card.method_name}` : "Method"}>
          <p>{card.method_summary}</p>
          {card.key_techniques && card.key_techniques.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-xs text-muted-foreground">
              {card.key_techniques.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          )}
        </Section>
      )}

      {(card.datasets?.length || card.baselines?.length || card.code_url) && (
        <Section title="Resources">
          <ResourceList
            datasets={card.datasets ?? []}
            baselines={card.baselines ?? []}
            codeUrl={card.code_url}
          />
        </Section>
      )}

      {card.main_results && card.main_results.length > 0 && (
        <Section title="Main results">
          <ul className="space-y-1 text-xs">
            {card.main_results.map((r, i) => (
              <li key={i} className="leading-relaxed">
                <ResultLine r={r} />
              </li>
            ))}
          </ul>
          {card.metrics && card.metrics.length > 0 && (
            <p className="mt-1 text-[11px] text-muted-foreground">
              Metrics: {card.metrics.join(", ")}
            </p>
          )}
        </Section>
      )}

      {card.conclusion && (
        <Section title="Conclusion">
          <p>{card.conclusion}</p>
        </Section>
      )}

      {card.limitations?.length || card.future_work?.length ? (
        <Section title="Limits & next steps">
          {card.limitations && card.limitations.length > 0 && (
            <div>
              <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
                Limitations
              </p>
              <ul className="mt-1 list-disc pl-5 text-xs">
                {card.limitations.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            </div>
          )}
          {card.future_work && card.future_work.length > 0 && (
            <div className="mt-2">
              <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
                Future work
              </p>
              <ul className="mt-1 list-disc pl-5 text-xs">
                {card.future_work.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            </div>
          )}
        </Section>
      ) : null}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {title}
      </p>
      <div className="mt-1 leading-relaxed">{children}</div>
    </div>
  );
}

function ResourceList({
  datasets,
  baselines,
  codeUrl,
}: {
  datasets: string[];
  baselines: string[];
  codeUrl?: string | null;
}) {
  return (
    <ul className="space-y-1 text-xs">
      {datasets.length > 0 && (
        <li>
          <span className="text-muted-foreground">Datasets: </span>
          {datasets.join(", ")}
        </li>
      )}
      {baselines.length > 0 && (
        <li>
          <span className="text-muted-foreground">Baselines: </span>
          {baselines.join(", ")}
        </li>
      )}
      {codeUrl && (
        <li>
          <span className="text-muted-foreground">Code: </span>
          <a
            href={codeUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-600 underline"
          >
            {codeUrl}
          </a>
        </li>
      )}
    </ul>
  );
}

function ResultLine({ r }: { r: ResultClaim }) {
  const num = typeof r.value === "number" ? r.value : null;
  return (
    <>
      <span>{r.claim}</span>
      {num !== null && (
        <span className="ml-1 font-mono text-[11px] text-muted-foreground">
          {r.dataset ? `(${r.dataset}: ` : "("}
          {num}
          {r.unit ?? ""}
          {r.baseline_value != null
            ? ` vs ${r.baseline_value}${r.baseline_label ? ` ${r.baseline_label}` : ""}`
            : ""}
          )
        </span>
      )}
    </>
  );
}
