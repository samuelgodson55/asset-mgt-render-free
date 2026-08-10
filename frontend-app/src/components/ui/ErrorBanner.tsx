// =============================================================================
// components/ui/ErrorBanner.tsx
// -----------------------------------------------------------------------------
// The small inline rust-toned banner used to surface a failed request --
// repeated verbatim across nearly every panel/modal before this was pulled
// out. `icon` is optional since a couple of call sites (the compact form
// errors) never had one.
// =============================================================================
import type { ReactNode } from "react";

export function ErrorBanner({ children, icon }: { children: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex items-start gap-2 bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">
      {icon && <span className="mt-0.5 shrink-0">{icon}</span>}
      <span>{children}</span>
    </div>
  );
}
