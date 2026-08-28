// Layer 3 — typed React Query wrappers for the Carrel API.
//
// useApiQuery is a thin wrapper around useQuery that:
//   1. Builds the query string from a params object (mirrors the
//      client.ts builder style — keep them in sync if you add params).
//   2. Calls requestCached<T> so 304s short-circuit to the prior body.
//   3. Lets callers override staleTime per-query (e.g. markdown → Infinity).
//
// useApiMutation wraps useMutation with the optimistic update contract:
//   1. Snapshots the previous value at the key.
//   2. Calls onOptimistic (which should setQueryData to the optimistic
//      value).
//   3. On error, calls onRollback (which restores the snapshot).
//   4. On success, invalidates the configured key (and any cascade keys)
//      so peers refetch fresh data.

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryKey,
  type UseMutationOptions,
} from "@tanstack/react-query";
import { requestCached } from "@/api/client";

export interface UseApiQueryParams {
  key: QueryKey;
  path: string;
  params?: Record<string, unknown>;
  staleTime?: number;
  enabled?: boolean;
  // Forwarded to useQuery; default `network` is what we want.
  refetchOnWindowFocus?: boolean;
}

function buildQueryString(params?: Record<string, unknown>): string {
  if (!params) return "";
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    if (Array.isArray(v)) {
      for (const item of v) q.append(k, String(item));
    } else if (typeof v === "boolean") {
      q.set(k, String(v));
    } else {
      q.set(k, String(v));
    }
  }
  const qs = q.toString();
  return qs ? `?${qs}` : "";
}

export function useApiQuery<T>({
  key,
  path,
  params,
  staleTime,
  enabled,
  refetchOnWindowFocus,
}: UseApiQueryParams) {
  const qs = buildQueryString(params);
  const fullPath = `${path}${qs}`;
  return useQuery<T>({
    queryKey: key,
    queryFn: () => requestCached<T>(fullPath),
    staleTime,
    enabled,
    refetchOnWindowFocus,
  });
}

// Convenience for the case where a query needs an entirely custom queryFn
// (e.g. multipart responses). Still routed through requestCached so ETag
// behaviour is identical.
export function useApiQueryWithFn<T>({
  key,
  queryFn,
  staleTime,
  enabled,
  refetchOnWindowFocus,
}: {
  key: QueryKey;
  queryFn: () => Promise<T>;
  staleTime?: number;
  enabled?: boolean;
  refetchOnWindowFocus?: boolean;
}) {
  return useQuery<T>({
    queryKey: key,
    queryFn,
    staleTime,
    enabled,
    refetchOnWindowFocus,
  });
}

// ---- useApiMutation ---------------------------------------------------------

export interface UseApiMutationConfig<TIn, TOut> {
  // The HTTP call. Throw to trigger rollback.
  mutate: (input: TIn) => Promise<TOut>;
  // Keys to invalidate on success. Each entry cascades to its descendants.
  invalidate?: QueryKey[];
  // Optional side-effect after a successful mutation (e.g. toast).
  onSuccess?: (output: TOut, input: TIn) => void;
  // Pre-mutation optimistic write. Receives the QueryClient so callers can
  // setQueryData on any key (e.g. both the per-paper detail and the list).
  onOptimistic?: (input: TIn, queryClient: ReturnType<typeof useQueryClient>) => void;
  // Called on error with the snapshot to restore. The wrapper does not
  // itself call setQueryData — the consumer knows which key(s) it touched
  // in onOptimistic and where to roll them back.
  onRollback?: (
    input: TIn,
    error: unknown,
    queryClient: ReturnType<typeof useQueryClient>,
  ) => void;
  // Standard useMutation options (e.g. retry behaviour overrides).
  mutationOptions?: Omit<UseMutationOptions<TOut, unknown, TIn>, "mutationFn">;
}

export function useApiMutation<TIn, TOut>(config: UseApiMutationConfig<TIn, TOut>) {
  const queryClient = useQueryClient();
  const { mutate, invalidate = [], onOptimistic, onRollback, onSuccess, mutationOptions } =
    config;

  return useMutation<TOut, unknown, TIn>({
    mutationFn: async (input) => {
      // Order matters: optimistic write first so the user sees the new
      // value immediately, then the real call. If mutate() throws we
      // roll back below.
      onOptimistic?.(input, queryClient);
      const result = await mutate(input);
      // Invalidate after the server confirms — the optimistic write
      // already covered the immediate UX, so we only need to mark peers
      // stale.
      for (const key of invalidate) {
        queryClient.invalidateQueries({ queryKey: key });
      }
      onSuccess?.(result, input);
      return result;
    },
    onError: (error, input) => {
      onRollback?.(input, error, queryClient);
    },
    ...mutationOptions,
  });
}
