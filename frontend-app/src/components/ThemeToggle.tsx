import { Sun, Moon } from "lucide-react";
import { useTheme } from "../lib/theme";

export function ThemeToggle({ className = "" }: { className?: string }) {
  const { theme, toggleTheme } = useTheme();

  return (
    <button
      type="button"
      onClick={toggleTheme}
      title={theme === "light" ? "Switch to dark mode" : "Switch to light mode"}
      aria-label="Toggle color theme"
      className={`theme-toggle w-7 h-7 rounded-[3px] border border-border-soft text-text-muted hover:text-brass-soft hover:border-brass/40 transition-colors shrink-0 ${className}`}
    >
      <span className="icon-sun"><Sun size={13} strokeWidth={1.75} /></span>
      <span className="icon-moon"><Moon size={13} strokeWidth={1.75} /></span>
    </button>
  );
}
