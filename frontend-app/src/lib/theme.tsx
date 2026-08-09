import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ThemeContext } from "./theme-context";

export type Theme = "dark" | "light";

const STORAGE_KEY = "ledger:theme";

function applyThemeClass(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("light", theme === "light");
}

// Keeps mobile browser chrome (the address bar / status bar tint on
// Android/iOS) in sync with whichever theme is active, ported from the
// legacy frontend's js/theme.js -- setTheme() there updates a
// `<meta name="theme-color">` tag on every toggle, creating it on demand
// if missing (initThemeToggle()). Same colors as the legacy version.
function syncThemeColorMeta(theme: Theme) {
  if (typeof document === "undefined") return;
  let meta = document.querySelector('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement("meta");
    meta.setAttribute("name", "theme-color");
    document.head.appendChild(meta);
  }
  meta.setAttribute("content", theme === "light" ? "#f6f7f9" : "#0f1319");
}

/** Reads the same source the inline pre-paint script in index.html used, so React's first render agrees with what's already on screen. */
function readInitialTheme(): Theme {
  if (typeof document !== "undefined" && document.documentElement.classList.contains("light")) return "light";
  return "dark";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readInitialTheme);

  // Make sure the meta tag exists and matches whatever theme was applied
  // pre-paint, before the first toggle ever happens.
  useEffect(() => {
    syncThemeColorMeta(theme);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setTheme = useCallback((next: Theme) => {
    const root = document.documentElement;
    // Briefly opt every color-ish property into a transition so the swap
    // reads as a cross-fade, then drop it -- keeps the class off during
    // normal interactions elsewhere on the page.
    root.classList.add("theme-transitioning");
    applyThemeClass(next);
    syncThemeColorMeta(next);
    localStorage.setItem(STORAGE_KEY, next);
    setThemeState(next);
    window.setTimeout(() => root.classList.remove("theme-transitioning"), 220);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(theme === "light" ? "dark" : "light");
  }, [theme, setTheme]);

  // Keep in sync if the theme is changed in another tab.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY && (e.newValue === "light" || e.newValue === "dark")) {
        applyThemeClass(e.newValue);
        syncThemeColorMeta(e.newValue);
        setThemeState(e.newValue);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  return <ThemeContext.Provider value={{ theme, toggleTheme, setTheme }}>{children}</ThemeContext.Provider>;
}
