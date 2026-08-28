// Layer 3 — React Query bootstrap. The defaults below are tuned for a
// single-user local app: the L1 + L2 backend already short-circuits 304s
// so the browser cache TTL (staleTime) is mostly a UX knob, not a
// correctness one. 30s strikes the balance between "no needless refetch on
// a back/forward nav" and "the local dev experience doesn't show stale
// data for more than half a minute".
//
// refetchOnWindowFocus is off because Carrel has no other tab to receive
// data from; out-of-scope cross-tab invalidation is documented in
// cosmic-marinating-locket.md as an explicit follow-up.

import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: false,
      // Backend invalidation is event-driven; one retry surfaces a real
      // outage without masking transient hiccups.
      retry: 1,
    },
    mutations: {
      // Mutations must surface failure to the user (the optimistic
      // rollback depends on it). One retry is enough to absorb a single
      // dropped packet.
      retry: 1,
    },
  },
});
