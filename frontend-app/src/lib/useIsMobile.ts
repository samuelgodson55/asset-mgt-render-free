import { useEffect, useState } from "react";

// Mirrors Tailwind's default `lg` breakpoint (1024px) -- the same width
// every `lg:` class in the app (e.g. Quotations.tsx's `grid-cols-1
// lg:grid-cols-3`) switches on, so "mobile" here means "below the point
// the two-column layouts collapse to one column".
const MOBILE_BREAKPOINT = 1024;

/** Same matchMedia + change-listener shape lib/theme.tsx's ThemeProvider
 * already uses for its light/dark system-preference read, just keyed off
 * a width query instead of `prefers-color-scheme`. Defaults to `false`
 * (desktop) during SSR/first paint since `window` isn't available yet. */
export function useIsMobile(breakpoint: number = MOBILE_BREAKPOINT): boolean {
  const [isMobile, setIsMobile] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia(`(max-width: ${breakpoint - 1}px)`).matches;
  });

  useEffect(() => {
    const mql = window.matchMedia(`(max-width: ${breakpoint - 1}px)`);
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mql.addEventListener("change", handler);
    setIsMobile(mql.matches);
    return () => mql.removeEventListener("change", handler);
  }, [breakpoint]);

  return isMobile;
}
