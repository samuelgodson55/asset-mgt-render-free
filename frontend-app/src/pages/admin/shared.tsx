// =============================================================================
// Shared JSX component used by more than one Admin/Manager panel
// (UsersPanel + OutsidersPanel). Pure, non-JSX helpers live in
// ./sharedHelpers.ts instead -- kept separate so Vite's Fast Refresh can
// tell components and plain utility exports apart.
// =============================================================================
import type { UserRow, OutsiderRow } from "../../lib/types";

export function AlertDots({ alerts }: { alerts: UserRow["alerts"] | OutsiderRow["alerts"] }) {
  if (!alerts.overdue && !alerts.due_soon && !alerts.pending_extension) return null;
  return (
    <span className="inline-flex items-center gap-1 ml-1.5">
      {alerts.overdue && <span title="Has an overdue item" className="w-1.5 h-1.5 rounded-full bg-rust" />}
      {!alerts.overdue && alerts.due_soon && <span title="Has an item due soon" className="w-1.5 h-1.5 rounded-full bg-brass" />}
      {alerts.pending_extension && <span title="Has a pending extension request" className="w-1.5 h-1.5 rounded-full bg-sky" />}
    </span>
  );
}
