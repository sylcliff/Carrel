// Layer 3 — typed React Query wrappers for the Carrel API.
//
// For plain "query → fetch" reads, use `useQuery` directly with a
// `requestCached` / `requestWithHeadersCached` queryFn — those helpers
// already implement the ETag 304 short-circuit.
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
  useQueryClient,
  type QueryKey,
  type UseMutationOptions,
} from "@tanstack/react-query";

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
