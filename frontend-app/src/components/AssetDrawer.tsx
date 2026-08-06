import { AnimatePresence, motion } from "framer-motion";
import { X, Tag, DollarSign, Layers, Clock } from "lucide-react";
import type { AssetType } from "../lib/types";
import { StatusPill } from "./StatusPill";
import { formatDate } from "../lib/api";

export function AssetDrawer({ asset, onClose }: { asset: AssetType | null; onClose: () => void }) {
  return (
    <AnimatePresence>
      {asset && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
            className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40"
          />
          <motion.div
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ type: "spring", stiffness: 320, damping: 34 }}
            className="fixed top-0 right-0 h-screen w-full max-w-md bg-surface border-l border-border-soft z-50 overflow-y-auto"
          >
            <div className="p-6">
              <div className="flex items-start justify-between">
                <div>
                  <p className="font-mono text-[11px] tracking-widest text-brass-soft">{asset.tag}</p>
                  <h2 className="font-display text-xl font-semibold text-text mt-1">{asset.name}</h2>
                </div>
                <button onClick={onClose} className="p-1.5 rounded-full hover:bg-surface-raised text-text-muted hover:text-text transition-colors">
                  <X size={16} />
                </button>
              </div>

              <div className="mt-3">
                <StatusPill status={asset.status} />
              </div>

              <div className="grid grid-cols-2 gap-3 mt-6">
                <div className="border border-border-soft rounded-[3px] p-3">
                  <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5"><Layers size={11} />Available</p>
                  <p className="font-mono text-xl text-text mt-1">{asset.available_quantity}<span className="text-text-faint text-sm">/{asset.total_quantity}</span></p>
                </div>
                <div className="border border-border-soft rounded-[3px] p-3">
                  <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5"><DollarSign size={11} />Unit price</p>
                  <p className="font-mono text-xl text-text mt-1">{asset.price != null ? `$${asset.price.toLocaleString()}` : "—"}</p>
                </div>
                <div className="border border-border-soft rounded-[3px] p-3">
                  <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5"><Tag size={11} />Category</p>
                  <p className="text-sm text-text mt-1">{asset.category ?? "Uncategorized"}</p>
                </div>
                <div className="border border-border-soft rounded-[3px] p-3">
                  <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5"><Clock size={11} />Updated</p>
                  <p className="text-sm text-text mt-1">{formatDate(asset.updated_at)}</p>
                </div>
              </div>

              <div className="mt-6 flex gap-2">
                <button className="flex-1 bg-brass hover:bg-brass-soft text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
                  Check out
                </button>
                <button className="flex-1 border border-border-soft hover:border-border text-text text-[13px] rounded-[3px] py-2.5 transition-colors">
                  Flag exception
                </button>
              </div>

              <div className="mt-8">
                <p className="text-[11px] uppercase tracking-wider text-text-faint mb-3">Recent activity</p>
                <div className="flex flex-col gap-3">
                  {["Checked out 2 units to T. Adeyemi", "Returned 1 unit from S. Kowalski", "Quantity adjusted +5 by Super Admin"].map((line, i) => (
                    <div key={i} className="flex items-start gap-2.5 text-[12.5px] text-text-muted">
                      <span className="w-1 h-1 rounded-full bg-border mt-1.5 shrink-0" />
                      {line}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
