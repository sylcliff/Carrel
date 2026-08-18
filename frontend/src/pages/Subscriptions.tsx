import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { addTopJournals, createSubscription, deleteSubscription, listSubscriptions, type Subscription } from "@/api/client";

const KINDS: { value: Subscription["kind"]; label: string; placeholder: string; help: string }[] = [
  { value: "keyword", label: "Keyword", placeholder: "retrieval augmented generation", help: "Searches arXiv and OpenAlex" },
  { value: "arxiv_category", label: "arXiv category", placeholder: "cs.CL", help: "e.g. cs.CL, q-bio.GN, stat.ML" },
  { value: "author", label: "Author (OpenAlex ID)", placeholder: "A5013214678", help: "OpenAlex Author ID (search from openalex.org)" },
  { value: "venue", label: "Venue (OpenAlex ID)", placeholder: "S123...", help: "OpenAlex Source ID (Nature, Cell, Science, ...)" },
];

export default function Subscriptions() {
  const [subs, setSubs] = useState<Subscription[]>([]);
  const [kind, setKind] = useState<Subscription["kind"]>("keyword");
  const [value, setValue] = useState("");
  const [label, setLabel] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      setSubs(await listSubscriptions());
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => { refresh(); }, []);

  async function onAdd() {
    if (!value.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await createSubscription({ kind, value: value.trim(), label: label.trim() || undefined });
      setValue("");
      setLabel("");
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onAddTopJournals() {
    setBusy(true);
    setErr(null);
    try {
      await addTopJournals();
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(id: number) {
    setBusy(true);
    try {
      await deleteSubscription(id);
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const current = KINDS.find((k) => k.value === kind)!;

  return (
    <main className="container space-y-6 py-8">
      <h1 className="text-2xl font-bold">Subscriptions</h1>
      <p className="text-sm text-muted-foreground">
        Tell Carrel what you want it to fetch. You can mix keywords, arXiv categories,
        authors, and venues — all run on every sync.
      </p>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Quick add: top journals</CardTitle>
        </CardHeader>
        <CardContent className="flex items-center justify-between gap-4">
          <p className="text-xs text-muted-foreground">
            One-click subscribe to Nature, Cell, and Science. Open-access papers get
            downloaded and parsed; the rest are stored with title + abstract.
          </p>
          <Button variant="outline" onClick={onAddTopJournals} disabled={busy}>
            Add Nature / Cell / Science
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Add a subscription</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            {KINDS.map((k) => (
              <button
                key={k.value}
                onClick={() => setKind(k.value)}
                className={`rounded-md border px-3 py-1.5 text-sm ${
                  kind === k.value
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-input bg-background hover:bg-muted"
                }`}
              >
                {k.label}
              </button>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">{current.help}</p>
          <div className="grid gap-2 sm:grid-cols-2">
            <input
              type="text"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={current.placeholder}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Label (optional)"
              className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            />
          </div>
          <Button onClick={onAdd} disabled={busy || !value.trim()}>Add</Button>
          {err && <p className="text-sm text-red-600">{err}</p>}
        </CardContent>
      </Card>

      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Your subscriptions ({subs.length})</h2>
        {subs.length === 0 && (
          <p className="text-sm text-muted-foreground">No subscriptions yet.</p>
        )}
        {subs.map((s) => (
          <Card key={s.id}>
            <CardContent className="flex items-center gap-3 p-3">
              <span className="rounded bg-muted px-2 py-0.5 text-xs">{s.kind}</span>
              <span className="font-mono text-sm">{s.value}</span>
              {s.label && <span className="text-sm text-muted-foreground">— {s.label}</span>}
              <div className="flex-1" />
              <button
                onClick={() => onDelete(s.id)}
                className="text-xs text-red-600 hover:underline"
              >
                remove
              </button>
            </CardContent>
          </Card>
        ))}
      </section>
    </main>
  );
}
