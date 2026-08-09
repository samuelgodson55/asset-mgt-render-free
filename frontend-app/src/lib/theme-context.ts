import { createContext } from "react";
import type { Theme } from "./theme";
// `Theme` is imported as type-only, so this doesn't create a runtime
// circular dependency with theme.tsx (which imports ThemeContext back
// from here) -- verbatimModuleSyntax erases the import entirely.

export interface ThemeContextValue {
  theme: Theme;
  toggleTheme: () => void;
  setTheme: (t: Theme) => void;
}

export const ThemeContext = createContext<ThemeContextValue | null>(null);
