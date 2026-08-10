// =============================================================================
// components/ui/RowDetails.tsx
// -----------------------------------------------------------------------------
// React port of legacy js/ui.js's rowDetailsTrigger()/openRowDetailsFromElement()
// pattern: every listing table hides its lower-priority columns below `sm`
// (see each column's `hidden sm:table-cell`) so a table never forces a phone
// into a long horizontal scroll. What gets hidden isn't lost, though -- the
// whole row becomes tappable on mobile and opens THIS shared modal with the
// hidden fields as a simple label/value list, plus a full-width "Actions"
// block reusing the exact same buttons the desktop Actions column shows.
//
// `fields` mirrors the legacy `[label, value][]` shape: an entry with an
// empty label renders as a full-width block instead of a label/value row --
// used for the trailing Actions block each table passes.
// =============================================================================
import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";
import { Modal, ModalHeader } from "./Modal";

export interface RowDetailField {
  /** Empty string renders `value` as a full-width block (e.g. the Actions row) instead of a label/value pair. */
  label: string;
  value: ReactNode;
}

export function RowDetailsModal({
  title,
  subtitle,
  fields,
  onClose,
}: {
  title: string;
  subtitle?: string;
  fields: RowDetailField[];
  onClose: () => void;
}) {
  return (
    <Modal onClose={onClose} size="sm" scrollable>
      <ModalHeader title={title} subtitle={subtitle} onClose={onClose} />
      <dl className="flex flex-col">
        {fields.length === 0 && <p className="py-2 text-center text-[12.5px] text-text-faint">No additional details.</p>}
        {fields.map((f, i) =>
          f.label ? (
            <div key={i} className="flex items-start justify-between gap-4 border-b border-border-soft py-2.5 last:border-0">
              <dt className="text-[12px] text-text-faint">{f.label}</dt>
              <dd className="text-right text-[12.5px] font-medium text-text">{f.value}</dd>
            </div>
          ) : (
            <div key={i} className="pt-3">{f.value}</div>
          ),
        )}
      </dl>
    </Modal>
  );
}

// Mobile-only affordance showing a row is tappable -- put at the end of a
// row's primary (always-visible) cell, right where the legacy chevron
// (`sm:hidden` inline SVG) sat, so the "there's more here" hint shows up
// exactly where a thumb would tap.
export function MobileRowChevron() {
  return <ChevronRight size={15} className="ml-auto shrink-0 text-text-faint sm:hidden" />;
}
