// =============================================================================
// components/ui/Modal.tsx
// -----------------------------------------------------------------------------
// Generic, page-agnostic modal shell shared by every confirm/create/edit
// dialog in Admin/Manager (and beyond) -- the React equivalent of legacy
// js/ui.js's openModal()/closeModal()/initModalBackdropDismiss(): every
// modal in this app is built the same way (a dim/blurred backdrop behind a
// centered panel), so that markup lives here once instead of being
// copy-pasted into every modal component.
//
// Nothing in this file knows about users/outsiders/backups specifically --
// callers pass their own content as children.
// =============================================================================
import { motion, AnimatePresence } from "framer-motion";
import { X } from "lucide-react";
import type { ReactNode } from "react";

type ModalTone = "default" | "danger" | "success";

const BORDER_BY_TONE: Record<ModalTone, string> = {
  default: "border-border-soft",
  danger: "border-rust/30",
  success: "border-moss/30",
};

export function Modal({
  onClose,
  size = "sm",
  tone = "default",
  scrollable = false,
  dismissOnBackdropClick = true,
  children,
}: {
  /** Omit (or pass undefined) for a modal that can't be dismissed by clicking outside it, e.g. RestoreCompleteModal. */
  onClose?: () => void;
  size?: "sm" | "md";
  tone?: ModalTone;
  scrollable?: boolean;
  dismissOnBackdropClick?: boolean;
  children: ReactNode;
}) {
  const maxWidth = size === "md" ? "max-w-md" : "max-w-sm";
  return (
    <AnimatePresence>
      <motion.div
        key="backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={dismissOnBackdropClick ? onClose : undefined}
        className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40"
      />
      <motion.div
        key="panel"
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className={`fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full ${maxWidth} bg-surface border ${BORDER_BY_TONE[tone]} rounded-[4px] p-6 ${
          scrollable ? "max-h-[85vh] overflow-y-auto" : ""
        }`}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}

// Title + close button row -- the top of nearly every modal. Omit onClose
// to render the title alone (e.g. RestoreCompleteModal, which is only
// dismissed via its own "Sign out and continue" button).
export function ModalHeader({ title, subtitle, onClose }: { title: string; subtitle?: string; onClose?: () => void }) {
  return (
    <div className="flex items-start justify-between mb-1">
      <div>
        <h2 className="font-display text-lg font-semibold text-text">{title}</h2>
        {subtitle && <p className="text-[12.5px] text-text-muted mt-1 mb-3">{subtitle}</p>}
      </div>
      {onClose && (
        <button onClick={onClose} className="text-text-faint hover:text-text transition-colors">
          <X size={16} />
        </button>
      )}
    </div>
  );
}

// The small uppercase "Destructive action" / "Restore complete" eyebrow
// line used above the title in the higher-stakes modals (RestoreModal,
// RestoreCompleteModal).
export function ModalEyebrow({ icon, label, tone }: { icon: ReactNode; label: string; tone: "danger" | "success" }) {
  const color = tone === "danger" ? "text-rust-soft" : "text-moss-soft";
  return (
    <div className={`flex items-center gap-2 mb-2 ${color}`}>
      {icon}
      <span className="text-[11px] uppercase tracking-wider">{label}</span>
    </div>
  );
}
