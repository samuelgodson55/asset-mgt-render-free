// =============================================================================
// components/ui/SearchInput.tsx
// -----------------------------------------------------------------------------
// The icon + text-input combo used to drive every server-side-searched
// listing table (Users, Outsiders, Deleted Users, Deleted Assets, Quotes) --
// same generic search box legacy js/ui.js's search/pagination engine drove
// across every listing table there.
// =============================================================================
import { Search } from "lucide-react";

export function SearchInput({
  value,
  onChange,
  placeholder,
  className = "relative max-w-xs flex-1",
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  className?: string;
}) {
  return (
    <div className={className}>
      <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full bg-surface border border-border-soft rounded-[3px] pl-7 pr-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none"
      />
    </div>
  );
}
