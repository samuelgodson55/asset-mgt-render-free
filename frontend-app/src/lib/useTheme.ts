// Convenience hook for consuming the light/dark theme context set up by
// `<ThemeProvider>` (see ./theme.tsx). Kept in its own file for the same
// React Fast Refresh reason as useAuth.ts/useCustody.ts.
import { useContext } from "react";
import { ThemeContext } from "./theme-context";

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>");
  return ctx;
}
