import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { PackageCheck, CalendarClock, X, Send } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { myItemsApi, extensionsApi, ApiError, formatDate } from "../lib/api";
import type { MyItem } from "../lib/types";
import { ExportButtons } from "../components/ExportButtons";

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
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<MyItem | null>(null);
  const [sentMsg, setSentMsg] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();

  const refresh = () =>
    myItemsApi
      .list()
      .then((data) => setItems(data.assigned_items))
      .catch(() => setItems([]))
      .finally(() => setLoading(false));

  useEffect(() => {
    refresh();
  }, []);

  // Deep link from the Notification Bell (?extend=<checkout_id>) -- opens
  // the Request Extension modal straight away for that item, same
  // click-through as legacy notifications.js's personal alert rows.
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
          <p className="text-text-muted text-sm mt-1">{items.length} item(s) currently checked out to you</p>
        </div>
        <ExportButtons
          disabled={items.length === 0}
          urlFor={(format) => myItemsApi.exportUrl(format)}
          filenameFor={(format) => `my_properties.${format}`}
        />
      </motion.div>

      {sentMsg && <div className="max-w-xl bg-moss/10 border border-moss/30 text-moss-soft text-[13px] rounded-[3px] px-4 py-3 mb-4">{sentMsg}</div>}

      <div className="border border-border-soft bg-surface rounded-[3px] overflow-hidden">
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
                </td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{item.quantity}</td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{formatDate(item.checkout_date)}</td>
                <td className="hidden sm:table-cell px-5 py-3 font-mono text-text-muted">{formatDate(item.due_date)}</td>
                <td className="px-5 py-3">
                  {item.due_soon ? (
                    <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-brass-soft"><span className="w-1.5 h-1.5 rounded-full bg-brass" />Due soon</span>
                  ) : (
                    <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-sky"><span className="w-1.5 h-1.5 rounded-full bg-sky" />On loan</span>
                  )}
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
