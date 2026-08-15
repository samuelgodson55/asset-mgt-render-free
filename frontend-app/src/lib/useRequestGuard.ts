import { useCallback, useEffect, useRef } from "react";

/**
 * Guards async UI work against stale responses and unmounts.
 * Calling begin() invalidates every previous request. The returned
 * isCurrent() callback is true only for the newest request while the
 * component is still mounted. This prevents fast search/filter changes,
 * manual refreshes, and route changes from letting an older response
 * overwrite newer state.
 */
export function useRequestGuard() {
  const generation = useRef(0);

  useEffect(() => () => {
    generation.current += 1;
  }, []);

  return useCallback(() => {
    const token = ++generation.current;
    return () => token === generation.current;
  }, []);
}
