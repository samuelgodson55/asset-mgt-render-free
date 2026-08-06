import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, PackageCheck, Loader2 } from "lucide-react";
import { usersApi, outsidersApi, checkoutsApi, ApiError, formatDate } from "../lib/api";
import type { CustodyItem } from "../lib/types";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

export function CustodyDrawer({
  target,
  onClose,
}: {
  target: { type: "user" | "outsider"; id: number; name: string } | null;
  onClose: () => void;
}) {
  const [items, setItems] = useState<CustodyItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [returningId, setReturningId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    if (!target) return;
    setLoading(true);
    const call = target.type === "user" ? usersApi.items(target.id) : outsidersApi.items(target.id);
    call
      .then((data) => setItems(data.assigned_items))
      .catch((err) => setError(errMsg(err, "Couldn't load custody items.")))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    setItems([]);
    setError(null);
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.id, target?.type]);

  if (!target) return null;

  const returnItem = async (item: CustodyItem) => {
    setReturningId(item.checkout_id);
    setError(null);
    try {
      await checkoutsApi.returnItem(item.checkout_id, item.outstanding);
      refresh();
    } catch (err) {
      setError(errMsg(err, "Return failed."));
    } finally {
      setReturningId(null);
    }
  };

  return (
    <AnimatePresence>
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div
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
        <p className="text-[12.5px] text-text-muted mt-2 mb-5">{items.length} item(s) currently in custody.</p>

        {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5 mb-4">{error}</div>}

        <div className="flex flex-col gap-2.5">
          {loading && <p className="text-[12px] text-text-faint text-center py-8">Loading…</p>}
          {!loading && items.length === 0 && <p className="text-[12px] text-text-faint text-center py-8">Nothing currently checked out.</p>}
          {items.map((item) => (
            <div key={item.checkout_id} className="border border-border-soft rounded-[3px] p-3.5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-[13px] text-text font-medium flex items-center gap-1.5"><PackageCheck size={12} className="text-moss-soft shrink-0" />{item.asset_name}</p>
                  <p className="text-[11px] text-text-faint font-mono mt-0.5">qty {item.outstanding} of {item.quantity} · due {formatDate(item.due_date)}</p>
                </div>
                {item.overdue && <span className="shrink-0 text-[10px] font-medium text-rust-soft">Overdue</span>}
                {!item.overdue && item.due_soon && <span className="shrink-0 text-[10px] font-medium text-brass-soft">Due soon</span>}
              </div>
              <button
                onClick={() => returnItem(item)}
                disabled={returningId === item.checkout_id}
                className="mt-2.5 w-full flex items-center justify-center gap-1.5 border border-border-soft hover:border-moss/50 hover:text-moss-soft disabled:opacity-60 text-text-muted text-[11.5px] font-medium rounded-[3px] py-1.5 transition-colors"
              >
                {returningId === item.checkout_id ? <Loader2 size={11} className="animate-spin" /> : null}
                {returningId === item.checkout_id ? "Processing…" : `Return all ${item.outstanding}`}
              </button>
            </div>
          ))}
        </div>
      </motion.div>
    </AnimatePresence>
  );
}
