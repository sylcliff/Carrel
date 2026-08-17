const API_BASE = "/api";

export class APIError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new APIError(res.status, text || res.statusText);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---- Health ----

export interface Health {
  status: string;
  version: string;
  db: string;
  mineru: string;
}

export const getHealth = () => request<Health>("/health");

// ---- Papers ----

export interface PaperSummary {
  id: string;
  title: string;
  venue: string | null;
  publication_date: string | null;
  authors: string[];
  oa_status: string;
  status: string;
  tldr_zh: string | null;
  tldr_en: string | null;
  keywords: string[];
  source: string;
}

export interface PaperDetail extends PaperSummary {
  abstract: string | null;
  doi: string | null;
  arxiv_id: string | null;
  pdf_url: string | null;
  pdf_path: string | null;
  md_path: string | null;
  summary_zh: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export const listPapers = (params?: { limit?: number; offset?: number; status?: string }) => {
  const q = new URLSearchParams();
  if (params?.limit) q.set("limit", String(params.limit));
  if (params?.offset) q.set("offset", String(params.offset));
  if (params?.status) q.set("status", params.status);
  const qs = q.toString();
  return request<PaperSummary[]>(`/papers${qs ? `?${qs}` : ""}`);
};

export const getPaper = (id: string) => request<PaperDetail>(`/papers/${encodeURIComponent(id)}`);

export const getPaperMarkdown = (id: string) =>
  request<{ id: string; body: string | null; md_path: string | null }>(
    `/papers/${encodeURIComponent(id)}/markdown`
  );

// ---- Processing (download PDF + parse with MinerU) ----

export const processPaper = (paperId: string, background = false) =>
  request<Job[]>("/process", {
    method: "POST",
    body: JSON.stringify({ paper_id: paperId, background }),
  });

export const processPending = (limit = 10, background = false) =>
  request<Job[]>("/process", {
    method: "POST",
    body: JSON.stringify({ limit, background }),
  });

// ---- Subscriptions ----

export interface Subscription {
  id: number;
  kind: "keyword" | "author" | "venue" | "arxiv_category";
  value: string;
  label: string | null;
  enabled: boolean;
  created_at: string;
}

export const listSubscriptions = () => request<Subscription[]>("/subscriptions");

export const createSubscription = (body: {
  kind: Subscription["kind"];
  value: string;
  label?: string;
  enabled?: boolean;
}) =>
  request<Subscription>("/subscriptions", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const deleteSubscription = (id: number) =>
  request<{ deleted: boolean }>(`/subscriptions/${id}`, { method: "DELETE" });

// ---- Sync / Jobs ----

export interface Job {
  id: number;
  kind: string;
  status: string;
  message: string | null;
  stats: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export const triggerSync = (lookbackHours = 24, background = false) =>
  request<Job>("/sync", {
    method: "POST",
    body: JSON.stringify({ lookback_hours: lookbackHours, background }),
  });

export const listJobs = () => request<Job[]>("/sync/jobs");

export const getJob = (id: number) => request<Job>(`/sync/jobs/${id}`);
