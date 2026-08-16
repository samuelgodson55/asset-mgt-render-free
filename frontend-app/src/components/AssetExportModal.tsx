// Modal used to choose the export format and optional category filter.
// It builds the download URL but leaves authentication to the same-origin
// browser session established by the backend.
import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { X, Download } from "lucide-react";
import { assetsApi } from "../lib/api";

export function AssetExportModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [categories, setCategories] = useState<string[]>([]);
  const [category, setCategory] = useState("all");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setCategory("all");
    assetsApi
      .categories()
      .then((res) => { if (!cancelled) setCategories(res.categories ?? []); })
      .catch(() => { if (!cancelled) setCategories([]); });
    return () => { cancelled = true; };
  }, [open]);

  if (!open) return null;

  const download = (format: "csv" | "pdf") => {
    const a = document.createElement("a");
    a.href = assetsApi.exportUrl(format, category === "all" ? undefined : category);
    a.download = `asset_inventory.${format}`;
    a.click();
  };

  const inputClass = "bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2.5 text-[13px] text-text focus:border-brass/50 focus:outline-none";

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel"
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6"
      >
        <div className="flex items-start justify-between mb-4">
          <h2 className="font-display text-lg font-semibold text-text">Export asset inventory</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <label className="text-[11px] uppercase tracking-wider text-text-faint">Scope</label>
            <select value={category} onChange={(e) => setCategory(e.target.value)} className={inputClass}>
              <option value="all">Download all</option>
              {categories.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="flex gap-2">
            <button onClick={() => download("csv")} className="flex-1 flex items-center justify-center gap-1.5 bg-brass hover:bg-brass-soft text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
              <Download size={13} /> CSV
            </button>
            <button onClick={() => download("pdf")} className="flex-1 flex items-center justify-center gap-1.5 border border-border-soft hover:border-brass/40 text-text text-[13px] rounded-[3px] py-2.5 transition-colors">
              <Download size={13} /> PDF
            </button>
          </div>
        </div>
      </motion.div>
    </AnimatePresence>
  );
}
