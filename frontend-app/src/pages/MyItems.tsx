import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { PackageCheck, CalendarClock, X, Send } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { myItemsApi, extensionsApi, ApiError, formatDate } from "../lib/api";
import type { MyItem } from "../lib/types";
import { ExportButtons } from "../components/ExportButtons";
import { PaginationBar, RowsPerPageSelect } from "../components/PaginationBar";
import { DEFAULT_PAGE_SIZE } from "../lib/pagination";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

function ExtensionRequestModal({ item, onClose, onSent }: { item: MyItem | null; onClose: () => void; onSent: () => void }) {
  const [newDate, setNewDate] = useState("");
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setNewDate("");
    setReason("");
    setError(null);
  }, [item]);

  if (!item) return null;

  const submit = async () => {
    if (!newDate) {
      setError("Choose a date.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await extensionsApi.request(item.checkout_id, newDate, reason.trim());
      onSent();
    } catch (err) {
      setError(errMsg(err, "Couldn't submit the request."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel"
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6"
      >
        <div className="flex items-start justify-between mb-1">
          <h2 className="font-display text-lg font-semibold text-text">Request extension</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <p className="text-[12.5px] text-text-muted mb-4">
          {item.asset_name} — currently due {formatDate(item.due_date)}.
        </p>

        <label className="block mb-3">
          <span className="text-[11px] uppercase tracking-wider text-text-faint">New due date</span>
          <input
            type="date"
            value={newDate}
            onChange={(e) => setNewDate(e.target.value)}
            className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text focus:border-brass/50 focus:outline-none transition-colors"
          />
        </label>
        <label className="block mb-4">
          <span className="text-[11px] uppercase tracking-wider text-text-faint">Reason (optional)</span>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={3}
            placeholder="Why do you need more time?"
            className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors resize-none"
          />
        </label>

        {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5 mb-4">{error}</div>}

        <button
          onClick={submit}
          disabled={submitting}
          className="w-full flex items-center justify-center gap-2 bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors"
        >
          <Send size={13} /> {submitting ? "Sending…" : "Submit request"}
        </button>
      </motion.div>
    </AnimatePresence>
  );
}

export function MyItems() {
  const [items, setItems] = useState<MyItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<MyItem | null>(null);
  const [sentMsg, setSentMsg] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();

  // TRUE server-side pagination (same `limit`/`offset` pattern as the
  // Asset Inventory table -- see pages/Assets.tsx) -- every page turn or
  // "rows per page" change re-fetches just that slice from
  // GET /users/me/items?limit=&offset= instead of downloading the whole
  // custody ledger and paging through it in memory.
  const refresh = () => {
    setLoading(true);
    myItemsApi
      .list(perPage, offset)
      .then((data) => {
        setItems(data.assigned_items);
        setTotal(data.total);
      })
      .catch(() => { setItems([]); setTotal(0); })
      .finally(() => setLoading(false));
  };

  useEffect(refresh, [perPage, offset]);

  // Called from the "Rows per page" <select> -- always jumps back to the
  // first page on a page-size change (mirrors Assets.tsx's handlePerPageChange).
  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  // Deep link from the Notification Bell (?extend=<checkout_id>) -- opens
  // the Request Extension modal straight away for that item, same
  // click-through as legacy notifications.js's personal alert rows. Only
  // matches against whichever page happens to be loaded; if the item
  // isn't on the current page nothing opens, same limitation the legacy
  // client-paginated table already had.
  useEffect(() => {
    const raw = searchParams.get("extend");
    if (!raw || items.length === 0) return;
    const checkoutId = Number(raw);
    const item = items.find((i) => i.checkout_id === checkoutId);
    if (item) setSelected(item);
    setSearchParams((prev) => { const next = new URLSearchParams(prev); next.delete("extend"); return next; }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6 flex items-start justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl font-semibold text-text">My Items</h1>
          <p className="text-text-muted text-sm mt-1">{total} item(s) currently checked out to you</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
          <ExportButtons
            disabled={total === 0}
            urlFor={(format) => myItemsApi.exportUrl(format)}
            filenameFor={(format) => `my_properties.${format}`}
          />
        </div>
      </motion.div>

      {sentMsg && <div className="max-w-xl bg-moss/10 border border-moss/30 text-moss-soft text-[13px] rounded-[3px] px-4 py-3 mb-4">{sentMsg}</div>}

      <div className="border border-border-soft bg-surface rounded-[3px] overflow-hidden">
        <div className="overflow-x-auto">
        <table className="w-full text-left text-[12.5px]">
          <thead className="bg-surface-raised text-text-faint text-[11px] uppercase tracking-wide">
            <tr>
              <th className="px-5 py-3 font-medium">Asset</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Qty</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Checked out</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium">Due back</th>
              <th className="px-5 py-3 font-medium">Status</th>
              <th className="hidden sm:table-cell px-5 py-3 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border-soft">
            {loading && <tr><td colSpan={6} className="px-5 py-6 text-center text-text-faint">Loading…</td></tr>}
            {!loading && items.length === 0 && (
              <tr><td colSpan={6} className="px-5 py-8 text-center text-text-faint">You have no items currently checked out.</td></tr>
            )}
            {items.map((item) => (
              <tr key={item.checkout_id}>
                <td className="px-5 py-3">
                  <p className="text-text font-medium flex items-center gap-2"><PackageCheck size={13} className="text-moss-soft shrink-0" />{item.asset_name}</p>
                  {/* MOBILE FIX: qty/checked-out/due-back and the "Request
                      extension" action all lived in columns hidden below
                      `sm`, with no fallback -- on a phone there was
                      previously no way to see when an item was due, let
                      alone request an extension on it. Mirrors the same
                      "stack it under the primary cell on mobile" pattern
                      already used for the System Backups table. */}
                  <p className="mt-1 text-[11px] text-text-faint sm:hidden">
                    Qty {item.quantity} · Out {formatDate(item.checkout_date)} · Due {formatDate(item.due_date)}
                  </p>
                  <button
                    onClick={() => setSelected(item)}
                    className="sm:hidden mt-2 flex items-center gap-1.5 rounded-md border border-border-soft px-2.5 py-1 text-[11.5px] font-medium text-text-muted hover:border-sky/50 hover:text-sky transition-colors"
                  >
                    <CalendarClock size={11} /> Request extension
                  </button>
                </td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{item.quantity}</td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{formatDate(item.checkout_date)}</td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{formatDate(item.due_date)}</td>
                <td className="px-5 py-3">
                  {/* STATUS FIX: this only ever checked item.due_soon, so
                      an item that had already gone overdue still showed
                      the same "On loan" badge as one with weeks left --
                      the backend was already computing item.overdue
                      alongside due_soon (services/user_service.py's
                      _group_assigned_items()), it just wasn't read here.
                      Overdue takes priority over due-soon since a loan
                      can't be both at once, and overdue is the more
                      urgent of the two. item.pending_extension is a
                      separate, non-exclusive state -- a loan can be
                      overdue/due-soon/on-loan AND have a request sitting
                      in review at the same time -- so it renders as its
                      own badge alongside the primary one rather than
                      replacing it, same sky color Notifications.tsx and
                      Admin.tsx's AlertDots already use for this state. */}
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    {item.overdue ? (
                      <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-rust-soft"><span className="w-1.5 h-1.5 rounded-full bg-rust" />Overdue</span>
                    ) : item.due_soon ? (
                      <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-brass-soft"><span className="w-1.5 h-1.5 rounded-full bg-brass" />Due soon</span>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-sky"><span className="w-1.5 h-1.5 rounded-full bg-sky" />On loan</span>
                    )}
                    {item.pending_extension && (
                      <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-sky" title="You already have an extension request awaiting review for this item">
                        <span className="w-1.5 h-1.5 rounded-full bg-sky" />Extension pending
                      </span>
                    )}
                  </div>
                </td>
                <td className="hidden sm:table-cell px-5 py-3 text-right">
                  <button
                    onClick={() => setSelected(item)}
                    className="flex items-center gap-1.5 ml-auto rounded-md border border-border-soft px-2.5 py-1 text-[11.5px] font-medium text-text-muted hover:border-sky/50 hover:text-sky transition-colors"
                  >
                    <CalendarClock size={11} /> Request extension
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </div>

      <div className="mt-5">
        <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />
      </div>

      <ExtensionRequestModal
        item={selected}
        onClose={() => setSelected(null)}
        onSent={() => {
          setSelected(null);
          setSentMsg("Extension request submitted -- your manager/admin will review it shortly.");
          refresh();
        }}
      />
    </div>
  );
}
