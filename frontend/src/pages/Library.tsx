import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, Star } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusDot } from "@/components/StatusDot";
import {
  deletePaper,
  listPapers,
  listTags,
  type PaperSummary,
  type TagWithCount,
} from "@/api/client";
import { useDebouncedCallback } from "@/lib/useDebouncedCallback";

export default function Library() {
  const [papers, setPapers] = useState<PaperSummary[]>([]);
  const [allTags, setAllTags] = useState<TagWithCount[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Filter state
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [favOnly, setFavOnly] = useState(false);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [tagMenuOpen, setTagMenuOpen] = useState(false);

  const debounceQ = useDebouncedCallback((value: string) => {
    setDebouncedQ(value);
  }, 350);

  useEffect(() => {
    listTags()
      .then(setAllTags)
      .catch(() => setAllTags([]));
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listPapers({
      limit: 200,
      favorite: favOnly || undefined,
      q: debouncedQ.trim() || undefined,
      tag: selectedTags.length ? selectedTags : undefined,
    })
      .then((rows) => {
        if (!cancelled) setPapers(rows);
      })
      .catch((e) => {
        if (!cancelled) setErr(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [debouncedQ, favOnly, selectedTags]);

  function toggleTag(name: string, on: boolean) {
    setSelectedTags((prev) =>
      on ? [...prev, name] : prev.filter((t) => t !== name),
    );
  }

  function addTagFilter(name: string) {
    setSelectedTags((prev) => (prev.includes(name) ? prev : [...prev, name]));
  }

  async function handleDelete(p: PaperSummary) {
    const confirmed = window.confirm(
      `Delete "${p.title}"?\n\nThis removes the paper, its parsed text, embeddings, and downloaded PDF from the library. This cannot be undone.`,
    );
    if (!confirmed) return;
    setDeletingId(p.id);
    setErr(null);
    try {
      await deletePaper(p.id);
      setPapers((prev) => prev.filter((x) => x.id !== p.id));
    } catch (e) {
      setErr(`Failed to delete ${p.id}: ${String(e)}`);
    } finally {
      setDeletingId(null);
    }
  }

  const activeFilterCount =
    (favOnly ? 1 : 0) + selectedTags.length + (debouncedQ.trim() ? 1 : 0);

  const tagNameSet = useMemo(() => new Set(allTags.map((t) => t.name)), [allTags]);

  return (
    <main className="container max-w-screen-2xl space-y-4 py-8">
      <h1 className="text-2xl font-bold">Library</h1>

      <div className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/20 p-2">
        <input
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            debounceQ(e.target.value);
          }}
          placeholder="Search title or author…"
          className="h-9 min-w-[12rem] flex-1 rounded-md border border-input bg-background px-3 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
        />

        <div className="relative">
          <Button
            variant="outline"
            size="sm"
            onClick={() => setTagMenuOpen((o) => !o)}
          >
            Tags {selectedTags.length > 0 && `(${selectedTags.length})`}
            <ChevronDown className="ml-1 h-3 w-3" />
          </Button>
          {tagMenuOpen && (
            <div className="absolute right-0 z-10 mt-1 max-h-64 w-56 overflow-auto rounded-md border bg-background p-1 shadow-md">
              {allTags.length === 0 && (
                <div className="px-2 py-1 text-xs text-muted-foreground">
                  No tags yet
                </div>
              )}
              {allTags.map((t) => (
                <label
                  key={t.id}
                  className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-sm hover:bg-muted"
                >
                  <input
                    type="checkbox"
                    checked={selectedTags.includes(t.name)}
                    onChange={(e) => toggleTag(t.name, e.target.checked)}
                  />
                  <span className="flex-1 truncate">{t.name}</span>
                  <span className="text-xs text-muted-foreground">
                    {t.paper_count}
                  </span>
                </label>
              ))}
            </div>
          )}
        </div>

        <label className="inline-flex h-9 cursor-pointer items-center gap-1.5 rounded-md border border-input bg-background px-3 text-sm">
          <input
            type="checkbox"
            checked={favOnly}
            onChange={(e) => setFavOnly(e.target.checked)}
          />
          <Star
            className={
              favOnly
                ? "h-3.5 w-3.5 fill-yellow-400 text-yellow-500"
                : "h-3.5 w-3.5"
            }
          />
          Favorites
        </label>

        {activeFilterCount > 0 && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setQ("");
              setDebouncedQ("");
              setFavOnly(false);
              setSelectedTags([]);
            }}
          >
            Clear
          </Button>
        )}
      </div>

      <p className="text-sm text-muted-foreground">
        {loading
          ? "Loading…"
          : `${papers.length} paper(s) shown${activeFilterCount ? " (filtered)" : ""}.`}
      </p>
      {err && <p className="text-sm text-red-600">{err}</p>}

      <div className="grid gap-2">
        {!loading && papers.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              No papers match the current filters.
            </CardContent>
          </Card>
        )}
        {papers.map((p, i) => (
          <Card key={p.id}>
            <CardContent className="flex items-start gap-3 p-4">
              <div className="flex items-center gap-1 pt-1.5">
                {p.favorite && (
                  <Star className="h-3.5 w-3.5 fill-yellow-400 text-yellow-500" />
                )}
                <StatusDot s={p.status} />
              </div>
              <span className="w-7 shrink-0 select-none pt-1 text-right text-sm tabular-nums text-muted-foreground">
                {i + 1}
              </span>
              <div className="min-w-0 flex-1">
                <Link
                  to={`/papers/${encodeURIComponent(p.id)}`}
                  className="font-medium hover:underline"
                >
                  {p.title}
                </Link>
                <div className="text-xs text-muted-foreground">
                  {p.authors.slice(0, 4).join(", ")}
                  {p.authors.length > 4 ? " et al." : ""} · {p.venue ?? p.source}
                  {p.publication_date && <> · {p.publication_date}</>}
                </div>
                {p.tags.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {p.tags.map((t) => (
                      <button
                        key={t}
                        type="button"
                        onClick={() => addTagFilter(t)}
                        title={`Filter by tag: ${t}`}
                        className={`rounded-full border px-1.5 py-0.5 text-[10px] hover:bg-muted ${
                          selectedTags.includes(t) || !tagNameSet.has(t)
                            ? "bg-muted/60"
                            : "bg-muted/30"
                        }`}
                      >
                        {t}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-3 text-xs text-muted-foreground">
                {p.citation_count !== null && p.citation_count !== undefined && (
                  <span title="Citations (Semantic Scholar)">
                    🏆 {p.citation_count}
                  </span>
                )}
                <span>{p.status}</span>
                <Button
                  variant="outline"
                  size="sm"
                  className="text-red-600 hover:bg-red-50 hover:text-red-700"
                  onClick={() => handleDelete(p)}
                  disabled={deletingId === p.id}
                  title="Delete paper and its files"
                >
                  {deletingId === p.id ? "Deleting…" : "Delete"}
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </main>
  );
}
