import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, Send } from "lucide-react";
import { api, extensionsApi, relativeTime, formatDate } from "../lib/api";
import type { Checkout, ExtensionRequest } from "../lib/types";
import { StatusPill } from "../components/StatusPill";

const tabs = ["All", "Overdue", "Active"] as const;

function DenyReasonModal({ request, onClose, onDenied }: { request: ExtensionRequest | null; onClose: () => void; onDenied: () => void }) {
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => setNote(""), [request]);
  if (!request) return null;

  const submit = async () => {
    setSubmitting(true);
    try {
      await extensionsApi.decide(request.id, false, note.trim() || null);
      onDenied();
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel"
        initial={{ opacity: 0, y: 12, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 8, scale: 0.98 }}
        className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-full max-w-sm bg-surface border border-border-soft rounded-[4px] p-6"
      >
        <div className="flex items-start justify-between mb-1">
          <h2 className="font-display text-lg font-semibold text-text">Deny extension</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <p className="text-[12.5px] text-text-muted mb-4">{request.asset_name} — requested by {request.requested_by}</p>
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

export function Checkouts() {
  const [checkouts, setCheckouts] = useState<Checkout[]>([]);
  const [extensions, setExtensions] = useState<ExtensionRequest[]>([]);
  const [tab, setTab] = useState<(typeof tabs)[number]>("All");
  const [denying, setDenying] = useState<ExtensionRequest | null>(null);

  const refreshExtensions = () => api.getExtensionRequests().then(setExtensions).catch((err) => console.error("Failed to load extension requests:", err));

  useEffect(() => {
    api.getCheckouts(true).then(setCheckouts).catch((err) => console.error("Failed to load checkouts:", err));
    refreshExtensions();
  }, []);

  const approve = async (id: number) => {
    await extensionsApi.decide(id, true, null);
    refreshExtensions();
  };

  const filtered = checkouts.filter((c) => {
    if (tab === "All") return true;
    if (tab === "Overdue") return c.status === "overdue";
    return c.status === "active";
  });

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6">
        <h1 className="font-display text-2xl font-semibold text-text">Checkouts</h1>
        <p className="text-text-muted text-sm mt-1">Track who has what, and who needs a nudge.</p>
      </motion.div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2">
          <div className="flex items-center gap-1 mb-4 border-b border-border-soft">
            {tabs.map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`relative px-3 py-2 text-[12.5px] font-medium transition-colors ${
                  tab === t ? "text-text" : "text-text-muted hover:text-text"
                }`}
              >
                {t}
                {tab === t && (
                  <motion.div layoutId="checkout-tab" className="absolute left-0 right-0 -bottom-px h-[2px] bg-brass" transition={{ type: "spring", stiffness: 500, damping: 40 }} />
                )}
              </button>
            ))}
          </div>

          <div className="border border-border-soft rounded-[3px] bg-surface overflow-hidden">
            <div className="grid grid-cols-[1fr_auto_auto] gap-3 px-4 py-2.5 border-b border-border-soft text-[10.5px] uppercase tracking-wider text-text-faint">
              <span>Asset / holder</span>
              <span>Due</span>
              <span className="w-16 text-right">Status</span>
            </div>
            <div className="divide-y divide-border-soft">
              {filtered.map((c, i) => (
                <motion.div
                  key={c.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ duration: 0.3, delay: i * 0.03 }}
                  className="grid grid-cols-[1fr_auto_auto] gap-3 px-4 py-3 hover:bg-surface-raised transition-colors"
                >
                  <div className="min-w-0">
                    <p className="text-[13px] text-text truncate">{c.asset_name}</p>
                    <p className="text-[11px] text-text-faint font-mono">{c.tag} · {c.checked_out_to} · qty {c.quantity}</p>
                  </div>
                  <div className="text-right">
                    <p className="text-[12px] text-text">{formatDate(c.due_at)}</p>
                    <p className="text-[10.5px] text-text-faint">{relativeTime(c.due_at)}</p>
                  </div>
                  <div className="w-16 flex justify-end">
                    <StatusPill status={c.status === "overdue" ? "overdue" : "active"} />
                  </div>
                </motion.div>
              ))}
              {filtered.length === 0 && <p className="text-center text-text-faint text-[12px] py-10">No checkouts in this view.</p>}
            </div>
          </div>
        </div>

        <div>
          <h2 className="font-display text-[15px] font-medium text-text mb-3">Extension requests</h2>
          <div className="flex flex-col gap-3">
            {extensions.map((e, i) => (
              <motion.div
                key={e.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.35, delay: i * 0.06 }}
                className="border border-border-soft bg-surface rounded-[3px] p-4"
              >
                <div className="flex items-start justify-between">
                  <p className="text-[13px] text-text font-medium">{e.asset_name}</p>
                  <StatusPill status="pending" />
                </div>
                <p className="text-[11.5px] text-text-muted mt-1">{e.requested_by} → until {formatDate(e.requested_until)}</p>
                <p className="text-[11.5px] text-text-faint mt-2 italic">"{e.reason}"</p>
                <div className="flex gap-2 mt-3">
                  <button onClick={() => approve(e.id)} className="flex-1 bg-moss/15 hover:bg-moss/25 text-moss-soft text-[11.5px] font-medium rounded-[3px] py-1.5 transition-colors">
                    Approve
                  </button>
                  <button onClick={() => setDenying(e)} className="flex-1 bg-rust/10 hover:bg-rust/20 text-rust-soft text-[11.5px] font-medium rounded-[3px] py-1.5 transition-colors">
                    Deny
                  </button>
                </div>
              </motion.div>
            ))}
            {extensions.length === 0 && <p className="text-text-faint text-[12px]">No pending requests.</p>}
          </div>
        </div>
      </div>

      <DenyReasonModal
        request={denying}
        onClose={() => setDenying(null)}
        onDenied={() => {
          setDenying(null);
          refreshExtensions();
        }}
      />
    </div>
  );
}
