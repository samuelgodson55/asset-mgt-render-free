import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

export type Theme = "dark" | "light";

const STORAGE_KEY = "ledger:theme";

function applyThemeClass(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("light", theme === "light");
}

/** Reads the same source the inline pre-paint script in index.html used, so React's first render agrees with what's already on screen. */
function readInitialTheme(): Theme {
  if (typeof document !== "undefined" && document.documentElement.classList.contains("light")) return "light";
  return "dark";
}

interface ThemeContextValue {
  theme: Theme;
  toggleTheme: () => void;
  setTheme: (t: Theme) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readInitialTheme);

  const setTheme = useCallback((next: Theme) => {
    const root = document.documentElement;
    // Briefly opt every color-ish property into a transition so the swap
    // reads as a cross-fade, then drop it -- keeps the class off during
    // normal interactions elsewhere on the page.
    root.classList.add("theme-transitioning");
    applyThemeClass(next);
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
        setThemeState(e.newValue);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  return <ThemeContext.Provider value={{ theme, toggleTheme, setTheme }}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>");
  return ctx;
}
