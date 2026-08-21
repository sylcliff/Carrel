import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { listTopics, type TopicWithCount } from "@/api/client";
import { topicDotClass } from "@/lib/topicColor";
import { cn } from "@/lib/utils";

// Reads the selected topic names out of the URL (?topic=X&topic=Y). Shared by
// the sidebar (to render checkboxes) and the Library page (to fetch filtered
// results), so deep links and card clicks stay in sync.
export function readSelectedTopics(sp: URLSearchParams): string[] {
  return sp.getAll("topic").filter(Boolean);
}

export function TopicSidebar({ className }: { className?: string }) {
  const [topics, setTopics] = useState<TopicWithCount[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();

  const selected = new Set(readSelectedTopics(searchParams));

  useEffect(() => {
    let cancelled = false;
    listTopics()
      .then((t) => !cancelled && setTopics(t))
      .catch(() => !cancelled && setTopics([]))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);

  function toggle(name: string, checked: boolean) {
    const next = new Set(readSelectedTopics(searchParams));
    if (checked) next.add(name);
    else next.delete(name);
    const params = new URLSearchParams(searchParams);
    params.delete("topic");
    for (const t of next) params.append("topic", t);
    // The filtered paper list lives on /library; toggling always lands there.
    const qs = params.toString();
    navigate(`/library${qs ? `?${qs}` : ""}`);
  }

  return (
    <nav className={cn("space-y-1", className)} aria-label="Topics filter">
      <div className="mb-2 flex items-center justify-between px-2">
        <h2 className="text-sm font-semibold">Topics</h2>
        {selected.size > 0 && (
          <button
            onClick={() => navigate("/library")}
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            Clear
          </button>
        )}
      </div>
      {loading ? (
        <p className="px-2 text-xs text-muted-foreground">Loading…</p>
      ) : topics.length === 0 ? (
        <p className="px-2 text-xs text-muted-foreground">
          No topics yet. Classify your library to see them here.
        </p>
      ) : (
        <ul className="space-y-0.5">
          {topics.map((t) => {
            const checked = selected.has(t.name);
            return (
              <li key={t.id}>
                <label
                  className={cn(
                    "flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-sm hover:bg-muted",
                    checked && "bg-muted/60",
                  )}
                  title={t.description ?? t.name}
                >
                  <input
                    type="checkbox"
                    className="h-3.5 w-3.5 shrink-0"
                    checked={checked}
                    onChange={(e) => toggle(t.name, e.target.checked)}
                  />
                  <span
                    className={cn(
                      "h-2 w-2 shrink-0 rounded-full",
                      topicDotClass(t.name),
                    )}
                  />
                  <span className="flex-1 truncate">{t.name}</span>
                  <span className="text-xs tabular-nums text-muted-foreground">
                    {t.paper_count}
                  </span>
                </label>
              </li>
            );
          })}
        </ul>
      )}
    </nav>
  );
}
