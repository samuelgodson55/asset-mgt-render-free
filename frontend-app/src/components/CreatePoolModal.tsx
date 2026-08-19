// Modal for creating a new asset pool. UI state lives here; the actual API
// request is delegated to the shared assets API so backend rules stay central.
import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { X, Loader2 } from "lucide-react";
import { assetsApi, ApiError } from "../lib/api";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

export function CreatePoolModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [qty, setQty] = useState("1");
  const [category, setCategory] = useState("");
  const [department, setDepartment] = useState("");
  const [price, setPrice] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (open) {
      setName("");
      setQty("1");
      setCategory("");
      setDepartment("");
      setPrice("");
      setError(null);
    }
  }, [open]);

  if (!open) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const result = await assetsApi.create({
        name,
        total_quantity: parseInt(qty, 10) || 0,
        category: category.trim() || null,
        department: department.trim() || null,
        price: price.trim() ? Number(price.trim()) : null,
      });
      onCreated();
      alert(result.message ? `${result.message}: "${name}"` : `Stock pool "${name}" created successfully.`);
    } catch (err) {
      setError(errMsg(err, "Couldn't create that pool."));
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass = "bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none";

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel"
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-md bg-surface border border-border-soft rounded-[4px] p-6 max-h-[85vh] overflow-y-auto"
      >
        <div className="flex items-start justify-between mb-4">
          <h2 className="font-display text-lg font-semibold text-text">Register new inventory pool</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <form onSubmit={submit} className="flex flex-col gap-3">
          <input required value={name} onChange={(e) => setName(e.target.value)} placeholder="Pool name (e.g. Dell Latitude 5440)" className={inputClass} />
          <input required type="number" min={0} value={qty} onChange={(e) => setQty(e.target.value)} placeholder="Initial total quantity" className={inputClass} />
          <input value={category} onChange={(e) => setCategory(e.target.value)} placeholder="Category (optional)" className={inputClass} />
          <input list="asset-departments" value={department} onChange={(e) => setDepartment(e.target.value)} placeholder="Department (e.g. Camera, Lighting, Grip)" className={inputClass} />
          <datalist id="asset-departments">
            <option value="Camera" />
            <option value="Lighting" />
            <option value="Grip" />
            <option value="Audio" />
            <option value="Power" />
            <option value="Production" />
          </datalist>
          <input type="number" min={0} step="0.01" value={price} onChange={(e) => setPrice(e.target.value)} placeholder="Unit price (optional)" className={inputClass} />
          {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}
          <button data-otel-action="asset.create" type="submit" disabled={submitting} className="flex items-center justify-center gap-1.5 bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
            {submitting && <Loader2 size={13} className="animate-spin" />}
            {submitting ? "Creating…" : "Create pool"}
          </button>
        </form>
      </motion.div>
    </AnimatePresence>
  );
}
