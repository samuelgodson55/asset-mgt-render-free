// =============================================================================
// components/ui/TableShell.tsx
// -----------------------------------------------------------------------------
// The bordered card + horizontal-scroll wrapper every listing table sits
// in, plus the loading/empty <tr> rows nearly every table needs. Column
// definitions and row rendering stay with each panel -- this only owns the
// chrome around them.
// =============================================================================
import type { ReactNode } from "react";

export function TableShell({ children }: { children: ReactNode }) {
  return (
    <div className="border border-border-soft bg-surface rounded-[3px] overflow-hidden">
      <div className="overflow-x-auto">{children}</div>
    </div>
  );
}

export function TableHead({ children }: { children: ReactNode }) {
  return (
    <thead className="bg-surface-raised text-text-faint text-[11px] uppercase tracking-wide">
      <tr>{children}</tr>
    </thead>
  );
}

// One <td colSpan={columns}>...</td> row, styled for either the "loading"
// or "no rows" state -- covers every table's two placeholder rows.
export function TablePlaceholderRow({ columns, children }: { columns: number; children: ReactNode }) {
  return (
    <tr>
      <td colSpan={columns} className="px-5 py-6 text-center text-text-faint">
        {children}
      </td>
    </tr>
  );
}
