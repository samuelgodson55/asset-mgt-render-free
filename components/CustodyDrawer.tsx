import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, PackageCheck, Loader2, CalendarClock, Check, Ban, Send, QrCode, ScanLine } from "lucide-react";
import { usersApi, outsidersApi, checkoutsApi, extensionsApi, ApiError, formatDate } from "../lib/api";
import type { CustodyItem } from "../lib/types";
import { ExportButtons } from "./ExportButtons";
import { ReceiptModal } from "./ReceiptModal";
import type { ReceiptTarget } from "../lib/receipt";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

// Shared by the single-item "Extend" button and the "Bulk extend selected"
// bar action -- both just POST one new due date, either to one checkout
// (POST /checkouts/{id}/extend) or many at once
// (POST /checkouts/bulk-extend). See legacy js/components/extensions.js's
// openDirectExtendModal()/openBulkExtendModal().
function ExtendModal({
  open,
  title,
  subtitle,
  onClose,
  onSubmit,
}: {
  open: boolean;
  title: string;
  subtitle: string;
  onClose: () => void;
  onSubmit: (newDueDate: string, reason: string) => Promise<void>;
}) {
  const [newDueDate, setNewDueDate] = useState("");
  const [reason, setReason] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setNewDueDate("");
      setReason("");
      setFieldError(null);
    }
  }, [open]);

  if (!open) return null;

  const submit = async () => {
    if (!newDueDate) {
      setFieldError("Choose a date.");
      return;
    }
    setFieldError(null);
    setSubmitting(true);
    try {
      await onSubmit(newDueDate, reason.trim());
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/80 backdrop-blur-sm z-[60]" />
      <motion.div key="panel"
        initial={{ opacity: 0, y: 12, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-[70] w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6"
      >
        <div className="flex items-start justify-between mb-1">
          <h2 className="font-display text-lg font-semibold text-text">{title}</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <p className="text-[12.5px] text-text-muted mb-4">{subtitle}</p>
        <label className="block mb-3">
          <span className="text-[11px] uppercase tracking-wider text-text-faint">New due date</span>
          <input
            type="date"
            value={newDueDate}
            onChange={(e) => setNewDueDate(e.target.value)}
            className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text focus:border-brass/50 focus:outline-none transition-colors"
          />
          {fieldError && <span className="block mt-1 text-[11px] text-rust-soft">{fieldError}</span>}
        </label>
        <label className="block mb-4">
          <span className="text-[11px] uppercase tracking-wider text-text-faint">Reason (optional)</span>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={2}
            placeholder="Why more time is being granted"
            className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors resize-none"
          />
        </label>
        <button
          onClick={submit}
          disabled={submitting}
          className="w-full flex items-center justify-center gap-2 bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors"
        >
          {submitting ? <Loader2 size={13} className="animate-spin" /> : <CalendarClock size={13} />}
          {submitting ? "Extending…" : "Grant extension"}
        </button>
      </motion.div>
    </AnimatePresence>
  );
}

// Denying without a reason leaves the requester (and the Audit Trail)
// guessing -- same "Deny reason" pattern as Checkouts.tsx's
// DenyReasonModal, just scoped to a bare requestId/assetName pair instead
// of a full ExtensionRequest, since that's all a Custody Ledger row has.
function DenyModal({
  target,
  onClose,
  onSubmit,
}: {
  target: { requestId: number; assetName: string } | null;
  onClose: () => void;
  onSubmit: (note: string) => Promise<void>;
}) {
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => setNote(""), [target]);
  if (!target) return null;

  const submit = async () => {
    setSubmitting(true);
    try {
      await onSubmit(note.trim());
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/80 backdrop-blur-sm z-[60]" />
      <motion.div key="panel"
        initial={{ opacity: 0, y: 12, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-[70] w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6"
      >
        <div className="flex items-start justify-between mb-1">
          <h2 className="font-display text-lg font-semibold text-text">Deny extension</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <p className="text-[12.5px] text-text-muted mb-4">{target.assetName}</p>
        <label className="block mb-4">
          <span className="text-[11px] uppercase tracking-wider text-text-faint">Note (optional)</span>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={3}
            placeholder="Let them know why"
            className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-rust/50 focus:outline-none transition-colors resize-none"
          />
        </label>
        <button
          onClick={submit}
          disabled={submitting}
          className="w-full flex items-center justify-center gap-2 bg-rust hover:bg-rust-soft disabled:opacity-60 text-white font-medium text-[13px] rounded-[3px] py-2.5 transition-colors"
        >
          <Send size={13} /> {submitting ? "Sending…" : "Deny request"}
        </button>
      </motion.div>
    </AnimatePresence>
  );
}

export function CustodyDrawer({
  target,
  onClose,
}: {
  target: { type: "user" | "outsider"; id: number; name: string; highlightCheckoutId?: number } | null;
  onClose: () => void;
}) {
  const [items, setItems] = useState<CustodyItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [returningId, setReturningId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Bulk-selection + per-row return-quantity state, mirroring legacy
  // custody.js's custody-item-checkbox / returnQty-{id} inputs.
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [qtyByCheckout, setQtyByCheckout] = useState<Record<number, number>>({});
  const [bulkBusy, setBulkBusy] = useState(false);

  const [extendTarget, setExtendTarget] = useState<CustodyItem | null>(null);
  const [bulkExtendOpen, setBulkExtendOpen] = useState(false);
  const [decidingId, setDecidingId] = useState<number | null>(null);
  const [denyTarget, setDenyTarget] = useState<{ requestId: number; assetName: string } | null>(null);

  // Whole-ledger scannable receipt -- "hand the person a ticket" at the
  // front desk. Built straight from the already-loaded `items` (this
  // drawer never paginates), same ReceiptTarget shape MyItems.tsx and
  // Checkouts.tsx build for their own per-row receipts.
  const [receiptOpen, setReceiptOpen] = useState(false);

  // Quick find: a returning borrower's OWN receipt barcode encodes
  // "CO-<checkout_id>" (see lib/receipt.ts's buildReceiptCode()) -- a
  // handheld scanner (or a phone's camera app typed into this box) can
  // key straight off that to jump to the matching row instead of hunting
  // through the list by eye, speeding up in-person check-in.
  const [scanQuery, setScanQuery] = useState("");
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const rowRefs = useRef<Record<number, HTMLDivElement | null>>({});
  // Tracks the `type:id:highlightCheckoutId` combo already auto-highlighted
  // so the effect below fires exactly once per open -- without it, every
  // refresh() (a return, an extend, a bulk action) would replace `items`
  // with a new array and re-trigger the scroll/flash on a row the person
  // has already seen and is actively working in.
  const appliedHighlightRef = useRef<string | null>(null);

  const handleScanSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const raw = scanQuery.trim();
    if (!raw) return;
    const match = raw.match(/(\d+)/);
    const id = match ? Number(match[1]) : NaN;
    const item = items.find((i) => i.checkout_id === id);
    if (item) {
      setHighlightId(item.checkout_id);
      rowRefs.current[item.checkout_id]?.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => setHighlightId((cur) => (cur === item.checkout_id ? null : cur)), 2200);
    } else {
      setError(`No item on this ledger matches "${raw}".`);
    }
    setScanQuery("");
  };

  const refresh = () => {
    if (!target) return;
    setLoading(true);
    const call = target.type === "user" ? usersApi.items(target.id) : outsidersApi.items(target.id);
    call
      .then((data) => {
        setItems(data.assigned_items);
        setQtyByCheckout((prev) => {
          const next: Record<number, number> = {};
          for (const item of data.assigned_items) {
            next[item.checkout_id] = prev[item.checkout_id] && prev[item.checkout_id] <= item.outstanding ? prev[item.checkout_id] : item.outstanding;
          }
          return next;
        });
        setSelected((prev) => {
          const ids = new Set(data.assigned_items.map((i) => i.checkout_id));
          return new Set(Array.from(prev).filter((id) => ids.has(id)));
        });
      })
      .catch((err) => setError(errMsg(err, "Couldn't load custody items.")))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    setItems([]);
    setError(null);
    setNotice(null);
    setSelected(new Set());
    setQtyByCheckout({});
    appliedHighlightRef.current = null;
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.id, target?.type]);

  // Global header search's "CO-<id>" deep link (see Layout.tsx's
  // submitHeaderSearch()): once this person's items have loaded, find the
  // exact row and reuse the same highlight/scroll behavior the manual
  // scan-to-find box above already uses -- see handleScanSubmit().
  useEffect(() => {
    if (!target?.highlightCheckoutId || loading) return;
    const key = `${target.type}:${target.id}:${target.highlightCheckoutId}`;
    if (appliedHighlightRef.current === key) return;
    const item = items.find((i) => i.checkout_id === target.highlightCheckoutId);
    if (!item) return;
    appliedHighlightRef.current = key;
    setHighlightId(item.checkout_id);
    rowRefs.current[item.checkout_id]?.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => setHighlightId((cur) => (cur === item.checkout_id ? null : cur)), 2200);
  }, [target, loading, items]);

  if (!target) return null;

  const allSelected = items.length > 0 && selected.size === items.length;

  const toggleSelectAll = () => {
    setSelected(allSelected ? new Set() : new Set(items.map((i) => i.checkout_id)));
  };

  const toggleOne = (checkoutId: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(checkoutId)) next.delete(checkoutId);
      else next.add(checkoutId);
      return next;
    });
  };

  const returnItem = async (item: CustodyItem, quantity: number) => {
    if (!quantity || quantity < 1 || quantity > item.outstanding) {
      setError(`Enter a quantity between 1 and ${item.outstanding} for ${item.asset_name}.`);
      return;
    }
    setError(null);
    setReturningId(item.checkout_id);
    try {
      await checkoutsApi.returnItem(item.checkout_id, quantity);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Return failed."));
    } finally {
      setReturningId(null);
    }
  };

  // Bulk return always clears the FULL outstanding amount on every
  // selected line -- for a partial return on one item, use that item's
  // own quantity field instead. Runs sequentially (not Promise.all) so
  // one failure doesn't leave the rest half-applied in an unpredictable
  // order, matching legacy custody.js's bulkProcessReturns().
  const bulkReturn = async (checkoutIds: number[]) => {
    if (!checkoutIds.length) return;
    setBulkBusy(true);
    setError(null);
    const failures: string[] = [];
    for (const id of checkoutIds) {
      const item = items.find((i) => i.checkout_id === id);
      if (!item) continue;
      try {
        await checkoutsApi.returnItem(id, item.outstanding);
      } catch (err) {
        failures.push(`${item.asset_name}: ${errMsg(err, "return failed")}`);
      }
    }
    setBulkBusy(false);
    if (failures.length) setError(`Some returns didn't go through — ${failures.join("; ")}`);
    else setNotice(`Processed ${checkoutIds.length} return(s).`);
    setSelected(new Set());
    refresh();
  };

  const processAllReturns = () => bulkReturn(items.map((i) => i.checkout_id));

  const submitDirectExtend = async (newDueDate: string, reason: string) => {
    if (!extendTarget) return;
    setError(null);
    try {
      await checkoutsApi.extend(extendTarget.checkout_id, newDueDate, reason || null);
      setExtendTarget(null);
      setNotice(`Extended ${extendTarget.asset_name} to ${formatDate(newDueDate)}.`);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Extend failed."));
    }
  };

  const submitBulkExtend = async (newDueDate: string, reason: string) => {
    const checkoutIds = Array.from(selected);
    if (!checkoutIds.length) return;
    setError(null);
    try {
      const result = await checkoutsApi.bulkExtend(checkoutIds, newDueDate, reason || null);
      setBulkExtendOpen(false);
      if (result.failed > 0) {
        const lines = result.results.filter((r) => !r.success).map((r) => `#${r.checkout_id}: ${r.error}`);
        setError(`${result.message ?? "Some extensions failed."} ${lines.join("; ")}`);
      } else {
        setNotice(result.message ?? `Extended ${result.succeeded} item(s).`);
      }
      setSelected(new Set());
      refresh();
    } catch (err) {
      setError(errMsg(err, "Bulk extend failed."));
    }
  };

  const approveExtension = async (item: CustodyItem) => {
    if (!item.pending_extension_request_id) return;
    setDecidingId(item.checkout_id);
    setError(null);
    try {
      await extensionsApi.decide(item.pending_extension_request_id, true, null);
      setNotice(`Approved extension on ${item.asset_name}.`);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Couldn't approve that request."));
    } finally {
      setDecidingId(null);
    }
  };

  const submitDenyExtension = async (note: string) => {
    if (!denyTarget) return;
    setDecidingId(denyTarget.requestId);
    setError(null);
    try {
      await extensionsApi.decide(denyTarget.requestId, false, note || null);
      setDenyTarget(null);
      setNotice(`Denied extension on ${denyTarget.assetName}.`);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Couldn't deny that request."));
    } finally {
      setDecidingId(null);
    }
  };

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel"
        initial={{ opacity: 0, x: 24 }}
        animate={{ opacity: 1, x: 0 }}
        exit={{ opacity: 0, x: 16 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="fixed right-0 top-0 bottom-0 z-50 w-full max-w-md bg-surface border-l border-border-soft p-6 overflow-y-auto"
      >
        <div className="flex items-start justify-between mb-1">
          <div>
            <p className="text-[11px] uppercase tracking-wider text-text-faint">Custody ledger</p>
            <h2 className="font-display text-lg font-semibold text-text">{target.name}</h2>
          </div>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={18} /></button>
        </div>
        <div className="flex items-center justify-between gap-3 mt-2 mb-4">
          <p className="text-[12.5px] text-text-muted">{items.length} item(s) currently in custody.</p>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => setReceiptOpen(true)}
              disabled={items.length === 0}
              title="Scannable receipt for this whole ledger"
              className="flex items-center gap-1 rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <QrCode size={11} /> Receipt
            </button>
            <ExportButtons
              compact
              disabled={items.length === 0}
              urlFor={(format) => (target.type === "user" ? usersApi.itemsExportUrl(target.id, format) : outsidersApi.itemsExportUrl(target.id, format))}
              filenameFor={(format) => `${target.name.toLowerCase().replace(/\s+/g, "_")}_properties.${format}`}
            />
          </div>
        </div>

        {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5 mb-3">{error}</div>}
        {notice && !error && <div className="bg-moss/10 border border-moss/30 text-moss-soft text-[12px] rounded-[3px] px-3 py-2.5 mb-3">{notice}</div>}

        {items.length > 0 && (
          <form onSubmit={handleScanSubmit} className="flex items-center gap-1.5 mb-3">
            <div className="relative flex-1">
              <ScanLine size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint pointer-events-none" />
              <input
                value={scanQuery}
                onChange={(e) => setScanQuery(e.target.value)}
                placeholder="Scan their receipt barcode to find an item…"
                className="w-full bg-ink-soft border border-border-soft rounded-[3px] pl-8 pr-2.5 py-2 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors"
              />
            </div>
          </form>
        )}

        {items.length > 0 && (
          <div className="flex items-center justify-between gap-2 mb-3 px-0.5">
            <label className="flex items-center gap-2 text-[11.5px] text-text-muted cursor-pointer select-none">
              <input type="checkbox" checked={allSelected} onChange={toggleSelectAll} className="h-3.5 w-3.5 rounded border-border-soft" />
              {selected.size > 0 ? `${selected.size} selected` : "Select all"}
            </label>
            <div className="flex items-center gap-2">
              <button
                onClick={() => bulkReturn(Array.from(selected))}
                disabled={selected.size === 0 || bulkBusy}
                className="text-[11px] font-medium text-moss-soft hover:text-moss disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                Process selected
              </button>
              <span className="text-text-faint text-[11px]">·</span>
              <button
                onClick={() => setBulkExtendOpen(true)}
                disabled={selected.size === 0 || bulkBusy}
                className="text-[11px] font-medium text-brass-soft hover:text-brass disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                Extend selected
              </button>
              <span className="text-text-faint text-[11px]">·</span>
              <button
                onClick={processAllReturns}
                disabled={bulkBusy}
                className="text-[11px] font-medium text-text-muted hover:text-text disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                Process all
              </button>
            </div>
          </div>
        )}

        <div className="flex flex-col gap-2.5">
          {loading && <p className="text-[12px] text-text-faint text-center py-8">Loading…</p>}
          {!loading && items.length === 0 && <p className="text-[12px] text-text-faint text-center py-8">Nothing currently checked out.</p>}
          {items.map((item) => {
            const qty = qtyByCheckout[item.checkout_id] ?? item.outstanding;
            const busy = returningId === item.checkout_id || decidingId === item.checkout_id;
            return (
              <div
                key={item.checkout_id}
                ref={(el) => { rowRefs.current[item.checkout_id] = el; }}
                className={`border rounded-[3px] p-3.5 transition-colors duration-300 ${
                  highlightId === item.checkout_id ? "border-brass bg-brass/10" : "border-border-soft"
                }`}
              >
                <div className="flex items-start gap-2.5">
                  <input
                    type="checkbox"
                    checked={selected.has(item.checkout_id)}
                    onChange={() => toggleOne(item.checkout_id)}
                    className="mt-1 h-3.5 w-3.5 shrink-0 rounded border-border-soft"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-[13px] text-text font-medium flex items-center gap-1.5 flex-wrap">
                          <PackageCheck size={12} className="text-moss-soft shrink-0" />
                          {item.asset_name}
                          {item.pending_extension && (
                            <span className="text-[10px] font-medium rounded-full px-1.5 py-0.5 bg-brass/15 text-brass-soft">
                              Requesting → {item.pending_extension_new_due_date}
                            </span>
                          )}
                          {item.is_outsourced && (
                            <span className="text-[10px] font-medium rounded-full px-1.5 py-0.5 bg-brass/10 text-brass-soft">
                              Outsourced{item.outsourced_source ? ` · ${item.outsourced_source}` : ""}
                            </span>
                          )}
                        </p>
                        <p className="text-[11px] text-text-faint font-mono mt-0.5">qty {item.outstanding} of {item.quantity} · due {formatDate(item.due_date)}</p>
                        {item.pending_extension && item.pending_extension_reason && (
                          <p className="text-[11px] text-text-faint italic mt-0.5">"{item.pending_extension_reason}"</p>
                        )}
                      </div>
                      {item.overdue && <span className="shrink-0 text-[10px] font-medium text-rust-soft">Overdue</span>}
                      {!item.overdue && item.due_soon && <span className="shrink-0 text-[10px] font-medium text-brass-soft">Due soon</span>}
                    </div>

                    <div className="flex flex-wrap items-center gap-1.5 mt-2.5">
                      <input
                        type="number"
                        min={1}
                        max={item.outstanding}
                        value={qty}
                        onChange={(e) => {
                          const v = Math.max(1, Math.min(item.outstanding, Number(e.target.value) || 1));
                          setQtyByCheckout((prev) => ({ ...prev, [item.checkout_id]: v }));
                        }}
                        className="w-14 bg-ink-soft border border-border-soft rounded-[3px] px-2 py-1.5 text-[11.5px] text-text focus:border-moss/50 focus:outline-none transition-colors"
                      />

                      {item.pending_extension && item.pending_extension_request_id ? (
                        <>
                          <button
                            onClick={() => approveExtension(item)}
                            disabled={busy}
                            className="flex items-center gap-1 border border-moss/40 bg-moss/10 hover:bg-moss/20 disabled:opacity-60 text-moss-soft text-[11px] font-medium rounded-[3px] px-2.5 py-1.5 transition-colors"
                          >
                            {decidingId === item.checkout_id ? <Loader2 size={11} className="animate-spin" /> : <Check size={11} />} Approve
                          </button>
                          <button
                            onClick={() => setDenyTarget({ requestId: item.pending_extension_request_id!, assetName: item.asset_name })}
                            disabled={busy}
                            className="flex items-center gap-1 border border-border-soft hover:border-rust/50 hover:text-rust-soft disabled:opacity-60 text-text-muted text-[11px] font-medium rounded-[3px] px-2.5 py-1.5 transition-colors"
                          >
                            <Ban size={11} /> Deny
                          </button>
                        </>
                      ) : (
                        <button
                          onClick={() => setExtendTarget(item)}
                          disabled={busy}
                          className="flex items-center gap-1 border border-border-soft hover:border-brass/50 hover:text-brass-soft disabled:opacity-60 text-text-muted text-[11px] font-medium rounded-[3px] px-2.5 py-1.5 transition-colors"
                        >
                          <CalendarClock size={11} /> Extend
                        </button>
                      )}

                      <button
                        onClick={() => returnItem(item, qty)}
                        disabled={busy}
                        className="flex items-center gap-1 border border-border-soft hover:border-moss/50 hover:text-moss-soft disabled:opacity-60 text-text-muted text-[11px] font-medium rounded-[3px] px-2.5 py-1.5 transition-colors ml-auto"
                      >
                        {returningId === item.checkout_id ? <Loader2 size={11} className="animate-spin" /> : null}
                        {returningId === item.checkout_id ? "Processing…" : `Return ${qty}`}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </motion.div>

      <ExtendModal
        open={!!extendTarget}
        title="Extend checkout"
        subtitle={extendTarget ? `${extendTarget.asset_name} — currently due ${formatDate(extendTarget.due_date)}` : ""}
        onClose={() => setExtendTarget(null)}
        onSubmit={submitDirectExtend}
      />
      <ExtendModal
        open={bulkExtendOpen}
        title="Extend selected"
        subtitle={`${selected.size} item(s) will get the same new due date.`}
        onClose={() => setBulkExtendOpen(false)}
        onSubmit={submitBulkExtend}
      />
      <DenyModal target={denyTarget} onClose={() => setDenyTarget(null)} onSubmit={submitDenyExtension} />

      <ReceiptModal
        target={
          receiptOpen
            ? ({
                holderName: target.name,
                holderSubtitle: target.type === "outsider" ? "Outsider" : undefined,
                note: "Custody ledger snapshot",
                items: items.map((item) => ({
                  checkout_id: item.checkout_id,
                  asset_name: item.asset_name,
                  quantity: item.outstanding,
                  due_date: item.due_date,
                  checked_out_at: item.checkout_date ?? null,
                })),
              } satisfies ReceiptTarget)
            : null
        }
        onClose={() => setReceiptOpen(false)}
      />
    </AnimatePresence>
  );
}
