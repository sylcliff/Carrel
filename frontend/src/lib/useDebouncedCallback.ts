import { useCallback, useEffect, useRef } from "react";

/**
 * Stable debounced wrapper around `fn`. The latest `fn` is always invoked (via a
 * ref), so callers don't need to memoize the callback. Pending calls are
 * cancelled on unmount.
 */
export function useDebouncedCallback<A extends unknown[]>(
  fn: (...args: A) => void,
  delay: number,
) {
  const fnRef = useRef(fn);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    fnRef.current = fn;
  }, [fn]);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  return useCallback(
    (...args: A) => {
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => fnRef.current(...args), delay);
    },
    [delay],
  );
}
