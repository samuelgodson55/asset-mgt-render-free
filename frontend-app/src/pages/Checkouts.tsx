import { useCallback, useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { X, Send, QrCode } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { checkoutsApi, extensionsApi, getDueSoonReminderDays, relativeTime, formatDate } from "../lib/api";
import type { Checkout, ExtensionRequest } from "../lib/types";
import { StatusPill } from "../components/StatusPill";
import { PaginationBar, RowsPerPageSelect } from "../components/PaginationBar";
import { DEFAULT_PAGE_SIZE } from "../lib/pagination";
import { useRequestGuard } from "../lib/useRequestGuard";
import { useCustody } from "../lib/useCustody";
import { ReceiptModal } from "../components/ReceiptModal";
import type { ReceiptTarget } from "../lib/receipt";

const tabs = ["All", "Overdue", "Due Soon", "Active"] as const;

// Maps each Checkouts page tab to the `filter` query param GET /checkouts
// narrows its SQL query with server-side (see backend/services/
// checkout_service.py's list_active_checkouts() `status_filter` param) --
// a module-level constant (not component state) since it never changes.
const FILTER_FOR_TAB: Record<(typeof tabs)[number], "overdue" | "due_soon" | "active" | undefined> = {
  All: undefined,
  Overdue: "overdue",
  "Due Soon": "due_soon",
  Active: "active",
};

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
  // Deep-link support (?tab=Overdue etc.) -- lets the Dashboard's "Overdue
  // returns" StatCard (and any other link into this page) land straight on
  // the filtered tab it promised instead of the unfiltered "All" list.
  const [searchParams, setSearchParams] = useSearchParams();
  const initialTab = tabs.find((t) => t === searchParams.get("tab")) ?? "All";
  const [tab, setTab] = useState<(typeof tabs)[number]>(initialTab);
  const [denying, setDenying] = useState<ExtensionRequest | null>(null);
  // Regenerate-a-lost-receipt / hand-someone-a-ticket-on-the-spot from the
  // system-wide list -- a single-checkout ReceiptTarget built straight
  // from the row that was clicked, same as MyItems.tsx's per-row receipt.
  const [receipt, setReceipt] = useState<ReceiptTarget | null>(null);
  // Same click-through the Notification Bell's grouped rows and legacy
  // custody.js's openCustodyModal() use: a checkout row IS a person
  // holding something, so clicking it should jump straight to that
  // person's Custody Ledger rather than going nowhere.
  const { openCustody } = useCustody();
  // TRUE server-side pagination -- each tab/page-size/offset combination
  // round-trips to GET /checkouts with `limit`/`offset` and (for
  // Overdue/Due Soon/Active) a `filter` narrowing the query itself (see
  // lib/api.ts's checkoutsApi.list() and backend/services/
  // checkout_service.py's list_active_checkouts() `status_filter` param),
  // instead of fetching every active checkout in one shot and slicing an
  // in-memory array. This matters specifically BECAUSE the tabs are
  // real-time-sensitive: "Overdue"/"Due Soon" are computed against the
  // current moment on every request (due_date vs `now`), never cached, so
  // the filter is re-evaluated fresh on every page turn/tab switch/refresh
  // -- a checkout can't be stuck showing a stale bucket the way a
  // client-side snapshot fetched once at mount could.
  const [checkoutsTotal, setCheckoutsTotal] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [offset, setOffset] = useState(0);
  // Same server-side pagination for the "Extension requests" side panel --
  // GET /checkouts/extension-requests already accepts limit/offset (see
  // checkoutsApi.list's sibling, extensionsApi.list), so this pages
  // against the server too rather than fetching up to 100 pending
  // requests and slicing that in-memory list.
  const [extensionsTotal, setExtensionsTotal] = useState(0);
  const [extPerPage, setExtPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [extOffset, setExtOffset] = useState(0);
  // Read after checkoutsApi.list() resolves below (not at mount) --
  // lib/api.ts only learns the real, .env-configured
  // settings.DUE_SOON_REMINDER_DAYS value from GET /checkouts' own
  // response, so this has to be re-read once that request lands rather
  // than assumed up front.
  const [dueSoonDays, setDueSoonDays] = useState(getDueSoonReminderDays());
  const beginCheckoutsRequest = useRequestGuard();
  const beginExtensionsRequest = useRequestGuard();

  const refreshCheckouts = useCallback(() => {
    const isCurrent = beginCheckoutsRequest();
    checkoutsApi.list(perPage, offset, FILTER_FOR_TAB[tab])
      .then(({ items, total }) => {
        if (!isCurrent()) return;
        setCheckouts(items);
        setCheckoutsTotal(total);
        setDueSoonDays(getDueSoonReminderDays());
      })
      .catch((err) => {
        if (isCurrent()) console.error("Failed to load checkouts:", err);
      });
  }, [beginCheckoutsRequest, perPage, offset, tab]);
  const refreshExtensions = useCallback(() => {
    const isCurrent = beginExtensionsRequest();
    extensionsApi.list(extPerPage, extOffset)
      .then(({ items, total }) => {
        if (!isCurrent()) return;
        setExtensions(items);
        setExtensionsTotal(total);
      })
      .catch((err) => {
        if (isCurrent()) console.error("Failed to load extension requests:", err);
      });
  }, [beginExtensionsRequest, extPerPage, extOffset]);

  // Re-fetch whenever the tab, page size, or offset changes -- each of
  // those changes what the server should return, so there's no
  // client-side list left to just slice.
  useEffect(refreshCheckouts, [refreshCheckouts]);
  useEffect(refreshExtensions, [refreshExtensions]);

  const approve = async (id: number) => {
    await extensionsApi.decide(id, true, null);
    refreshExtensions();
  };

  // Switching tabs changes the underlying (server-side) result set, so
  // always jump back to the first page -- same behavior as every other
  // paginated table's "rows per page" / search change (see Assets.tsx's
  // handlePerPageChange). Resetting `offset` here also triggers the
  // refetch above via its own effect.
  const changeTab = (t: (typeof tabs)[number]) => {
    setTab(t);
    setOffset(0);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (t === "All") next.delete("tab");
      else next.set("tab", t);
      return next;
    }, { replace: true });
  };
  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };
  const handleExtPerPageChange = (n: number) => {
    setExtPerPage(n);
    setExtOffset(0);
  };

  // An approve/deny (or a background refresh) can shrink the list out from
  // under whatever page was open -- snap back to the first page rather
  // than stranding the panel on a now-empty page. Resetting `extOffset`
  // here re-triggers the refetch above via its own effect.
  useEffect(() => {
    if (offset > 0 && offset >= checkoutsTotal) setOffset(Math.max(0, Math.floor(Math.max(checkoutsTotal - 1, 0) / perPage) * perPage));
  }, [checkoutsTotal, offset, perPage]);

  useEffect(() => {
    if (extOffset > 0 && extOffset >= extensionsTotal) setExtOffset(Math.max(0, Math.floor(Math.max(extensionsTotal - 1, 0) / extPerPage) * extPerPage));
  }, [extensionsTotal, extOffset, extPerPage]);

  const paged = checkouts;
  const pagedExtensions = extensions;

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6">
        <h1 className="font-display text-2xl font-semibold text-text">Checkouts</h1>
        <p className="text-text-muted text-sm mt-1">Track who has what, and who needs a nudge.</p>
      </motion.div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2">
          <div className="flex items-center justify-between gap-2 mb-4 border-b border-border-soft flex-wrap">
            <div className="flex items-center gap-1">
              {tabs.map((t) => (
                <button
                  key={t}
                  onClick={() => changeTab(t)}
                  className={`relative px-3 py-2 text-[12.5px] font-medium transition-colors ${
                    tab === t ? "text-text" : "text-text-muted hover:text-text"
                  }`}
                >
                  {t === "Due Soon" ? `Due Soon (≤${dueSoonDays}d)` : t}
                  {tab === t && (
                    <motion.div layoutId="checkout-tab" className="absolute left-0 right-0 -bottom-px h-[2px] bg-brass" transition={{ type: "spring", stiffness: 500, damping: 40 }} />
                  )}
                </button>
              ))}
            </div>
            <div className="pb-2">
              <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
            </div>
          </div>

          <div className="border border-border-soft rounded-[3px] bg-surface overflow-hidden">
            <div className="grid grid-cols-[1fr_auto_auto] gap-3 px-4 py-2.5 border-b border-border-soft text-[10.5px] uppercase tracking-wider text-text-faint">
              <span>Asset / holder</span>
              <span>Due</span>
              <span className="w-16 text-right">Status</span>
            </div>
            <div className="divide-y divide-border-soft">
              {paged.map((c, i) => {
                const clickable = c.entity_id != null;
                return (
                <motion.div
                  key={c.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ duration: 0.3, delay: i * 0.03 }}
                  onClick={clickable ? () => openCustody(c.entity_type ?? "user", c.entity_id as number, c.checked_out_to) : undefined}
                  role={clickable ? "button" : undefined}
                  tabIndex={clickable ? 0 : undefined}
                  title={clickable ? `View ${c.checked_out_to}'s Custody Ledger` : undefined}
                  className={`grid grid-cols-[1fr_auto_auto] gap-3 px-4 py-3 hover:bg-surface-raised transition-colors ${clickable ? "cursor-pointer" : ""}`}
                >
                  <div className="min-w-0">
                    <p className="text-[13px] text-text truncate">{c.asset_name}</p>
                    <p className="text-[11px] text-text-faint font-mono">{c.tag} · {c.checked_out_to} · qty {c.quantity}</p>
                  </div>
                  <div className="text-right">
                    {c.due_at ? (
                      <>
                        <p className="text-[12px] text-text">{formatDate(c.due_at)}</p>
                        <p className="text-[10.5px] text-text-faint">{relativeTime(c.due_at)}</p>
                      </>
                    ) : (
                      <p className="text-[12px] text-text-faint">No due date</p>
                    )}
                  </div>
                  <div className="flex items-center justify-end gap-2">
                    <StatusPill status={c.due_soon ? "due_soon" : c.status === "overdue" ? "overdue" : "active"} />
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setReceipt({
                          holderName: c.checked_out_to,
                          note: "Regenerated from Checkouts",
                          items: [{ checkout_id: c.id, asset_name: c.asset_name, tag: c.tag, quantity: c.quantity, due_date: c.due_at, checked_out_at: c.checked_out_at }],
                        });
                      }}
                      title="View/print receipt"
                      className="text-text-faint hover:text-brass-soft transition-colors"
                    >
                      <QrCode size={13} />
                    </button>
                  </div>
                </motion.div>
                );
              })}
              {checkoutsTotal === 0 && <p className="text-center text-text-faint text-[12px] py-10">No checkouts in this view.</p>}
            </div>
          </div>

          <div className="mt-4">
            <PaginationBar total={checkoutsTotal} perPage={perPage} offset={offset} onOffsetChange={setOffset} />
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between gap-2 mb-3 flex-wrap">
            <h2 className="font-display text-[15px] font-medium text-text">Extension requests</h2>
            {extensionsTotal > extPerPage && <RowsPerPageSelect value={extPerPage} onChange={handleExtPerPageChange} />}
          </div>
          <div className="flex flex-col gap-3">
            {pagedExtensions.map((e, i) => (
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
            {extensionsTotal === 0 && <p className="text-text-faint text-[12px]">No pending requests.</p>}
          </div>
          {extensionsTotal > extPerPage && (
            <div className="mt-3">
              <PaginationBar total={extensionsTotal} perPage={extPerPage} offset={extOffset} onOffsetChange={setExtOffset} />
            </div>
          )}
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

      <ReceiptModal target={receipt} onClose={() => setReceipt(null)} />
    </div>
  );
}
