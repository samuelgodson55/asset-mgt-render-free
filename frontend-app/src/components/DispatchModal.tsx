import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { X, Loader2, QrCode, CheckCircle2 } from "lucide-react";
import { assetsApi, ApiError, formatDate } from "../lib/api";
import type { RosterUser, OutsiderRow } from "../lib/types";
import { ReceiptModal } from "./ReceiptModal";
import type { ReceiptTarget } from "../lib/receipt";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

// Same rule as backend/schemas/assets_schema.py's MAX_DUE_DATE_YEARS_AHEAD --
// this client-side bound is purely a UX nicety (stops the date picker from
// ever offering an invalid date); the backend re-validates for real.
const MAX_DUE_DATE_YEARS_AHEAD = 5;

function todayInputValue(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function maxDueDateValue(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() + MAX_DUE_DATE_YEARS_AHEAD);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

type Route = "staff" | "customer" | "adhoc";

export function DispatchModal({
  asset,
  onClose,
  onDispatched,
}: {
  asset: { id: number; name: string; available_quantity: number } | null;
  onClose: () => void;
  onDispatched: () => void;
}) {
  const [route, setRoute] = useState<Route>("staff");
  const [staff, setStaff] = useState<RosterUser[]>([]);
  const [customers, setCustomers] = useState<RosterUser[]>([]);
  const [outsiders, setOutsiders] = useState<OutsiderRow[]>([]);
  const [rosterLoading, setRosterLoading] = useState(true);

  const [quantity, setQuantity] = useState(1);
  const [dueDate, setDueDate] = useState("");
  const [staffId, setStaffId] = useState("");
  const [customerId, setCustomerId] = useState("");
  const [adhocExistingId, setAdhocExistingId] = useState("new");
  const [adhocName, setAdhocName] = useState("");
  const [adhocCompany, setAdhocCompany] = useState("");
  const [adhocEmail, setAdhocEmail] = useState("");
  const [adhocPhone, setAdhocPhone] = useState("");

  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // Post-dispatch success state -- replaces the old `alert(result.message)`
  // with an inline confirmation that offers a scannable receipt on the
  // spot, since this dispatch is (almost always) the moment the receipt
  // is actually needed. `assigneeName` is captured at submit time from
  // whichever route was used, since the roster/form state it came from
  // gets cleared the next time this modal opens for a new asset.
  const [dispatched, setDispatched] = useState<{ assigneeName: string; quantity: number; dueDate: string; message?: string } | null>(null);
  const [receiptOpen, setReceiptOpen] = useState(false);

  useEffect(() => {
    if (!asset) return;
    setRoute("staff");
    setQuantity(1);
    setDueDate("");
    setStaffId("");
    setCustomerId("");
    setAdhocExistingId("new");
    setAdhocName("");
    setAdhocCompany("");
    setAdhocEmail("");
    setAdhocPhone("");
    setError(null);
    setDispatched(null);
    setReceiptOpen(false);
    setRosterLoading(true);
    Promise.all([assetsApi.staffRoster(), assetsApi.customerRoster(), assetsApi.outsiderRoster()])
      .then(([s, c, o]) => {
        setStaff(s);
        setCustomers(c);
        setOutsiders(o);
      })
      .catch(() => {
        // Non-fatal -- the dispatch form still renders, just with empty rosters.
      })
      .finally(() => setRosterLoading(false));
  }, [asset]);

  if (!asset) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (route === "adhoc" && !dueDate) {
      setError("Due date is mandatory for external unauthenticated allocations.");
      return;
    }

    let payload: Parameters<typeof assetsApi.checkoutAdvanced>[1];
    if (route === "staff") {
      if (!staffId) {
        setError("Choose a staff member to dispatch to.");
        return;
      }
      payload = { assignee_type: "user", quantity, due_date: dueDate || null, user_id: Number(staffId) };
    } else if (route === "customer") {
      if (!customerId) {
        setError("No linked customer accounts are on file yet. Create one from the User Directory first.");
        return;
      }
      payload = { assignee_type: "user", quantity, due_date: dueDate || null, user_id: Number(customerId) };
    } else {
      payload = { assignee_type: "outsider", quantity, due_date: dueDate || null };
      if (adhocExistingId !== "new") {
        payload.outsider_id = Number(adhocExistingId);
      } else {
        if (!adhocName || (!adhocEmail && !adhocPhone)) {
          setError("Name and at least one of email/phone are required for outsiders.");
          return;
        }
        payload.outsider_name = adhocName;
        payload.outsider_company = adhocCompany || undefined;
        payload.outsider_email = adhocEmail || null;
        payload.outsider_phone = adhocPhone || null;
      }
    }

    // Captured before the async call resolves -- reflects the roster
    // pick/typed name for whichever route was used, for the receipt's
    // "Held by" line (the raw payload only carries IDs).
    const assigneeName =
      route === "staff" ? staff.find((u) => String(u.id) === staffId)?.name ?? "Staff member"
      : route === "customer" ? customers.find((u) => String(u.id) === customerId)?.name ?? "Customer"
      : adhocExistingId !== "new" ? outsiders.find((o) => String(o.id) === adhocExistingId)?.name ?? adhocName
      : adhocName;

    setSubmitting(true);
    try {
      const result = await assetsApi.checkoutAdvanced(asset.id, payload);
      setDispatched({ assigneeName, quantity, dueDate, message: result.message });
    } catch (err) {
      setError(errMsg(err, "Dispatch failed."));
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
        <div className="flex items-start justify-between mb-1">
          <h2 className="font-display text-lg font-semibold text-text">Issue / Dispatch</h2>
          <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={16} /></button>
        </div>
        <p className="text-[12.5px] text-text-muted mb-4">{asset.name} · {asset.available_quantity} available</p>

        {dispatched ? (
          // Success state -- replaces the old blocking `alert(result.message)`.
          // A checkout receipt is most useful right at the moment it's
          // issued (nothing to look up later), so this is the primary
          // action here rather than something to hunt for afterward.
          <div className="flex flex-col gap-3">
            <div className="flex items-start gap-2.5 bg-moss/10 border border-moss/30 rounded-[3px] px-3.5 py-3">
              <CheckCircle2 size={16} className="text-moss-soft shrink-0 mt-0.5" />
              <div className="min-w-0">
                <p className="text-[13px] text-text font-medium">Dispatched to {dispatched.assigneeName}</p>
                <p className="text-[11.5px] text-text-muted mt-0.5">
                  {dispatched.quantity} × {asset.name}{dispatched.dueDate ? ` · due ${formatDate(dispatched.dueDate)}` : ""}
                </p>
                {dispatched.message && <p className="text-[11px] text-text-faint mt-1">{dispatched.message}</p>}
              </div>
            </div>

            <button
              onClick={() => setReceiptOpen(true)}
              className="flex items-center justify-center gap-1.5 border border-border-soft hover:border-brass/50 text-text-muted hover:text-brass-soft text-[13px] font-medium rounded-[3px] py-2.5 transition-colors"
            >
              <QrCode size={13} /> View / print receipt
            </button>
            <button
              onClick={onDispatched}
              className="flex items-center justify-center gap-1.5 bg-brass hover:bg-brass-soft text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors"
            >
              Done
            </button>
          </div>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] uppercase tracking-wider text-text-faint">Assign to</label>
              <select value={route} onChange={(e) => setRoute(e.target.value as Route)} className={inputClass}>
                <option value="staff">Staff member</option>
                <option value="customer">Linked customer account</option>
                <option value="adhoc">Ad-Hoc (unlinked) individual</option>
              </select>
            </div>

            {route === "staff" && (
              <select required value={staffId} onChange={(e) => setStaffId(e.target.value)} className={inputClass}>
                <option value="" disabled>{rosterLoading ? "Loading…" : staff.length ? "Select a staff account" : "No staff accounts on file"}</option>
                {staff.map((u) => (
                  <option key={u.id} value={u.id}>{u.name} ({u.department_role || u.role})</option>
                ))}
              </select>
            )}

            {route === "customer" && (
              <select required value={customerId} onChange={(e) => setCustomerId(e.target.value)} className={inputClass}>
                <option value="" disabled>{rosterLoading ? "Loading…" : customers.length ? "Select a linked customer" : "No linked customer accounts on file"}</option>
                {customers.map((u) => (
                  <option key={u.id} value={u.id}>{u.name} ({u.email})</option>
                ))}
              </select>
            )}

            {route === "adhoc" && (
              <>
                <select value={adhocExistingId} onChange={(e) => setAdhocExistingId(e.target.value)} className={inputClass}>
                  <option value="new">+ Create new unlinked profile</option>
                  {outsiders.map((o) => (
                    <option key={o.id} value={o.id}>{o.name}{o.company ? ` (${o.company})` : ""}</option>
                  ))}
                </select>
                {adhocExistingId === "new" && (
                  <>
                    <input required value={adhocName} onChange={(e) => setAdhocName(e.target.value)} placeholder="Name" className={inputClass} />
                    <input value={adhocCompany} onChange={(e) => setAdhocCompany(e.target.value)} placeholder="Company (optional)" className={inputClass} />
                    <input type="email" value={adhocEmail} onChange={(e) => setAdhocEmail(e.target.value)} placeholder="Email" className={inputClass} />
                    <input value={adhocPhone} onChange={(e) => setAdhocPhone(e.target.value)} placeholder="Phone" className={inputClass} />
                    <p className="text-[11px] text-text-faint -mt-1">At least one of email or phone is required.</p>
                  </>
                )}
              </>
            )}

            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] uppercase tracking-wider text-text-faint">Quantity</label>
                <input type="number" min={1} max={asset.available_quantity} value={quantity} onChange={(e) => setQuantity(Math.max(1, parseInt(e.target.value, 10) || 1))} className={inputClass} />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] uppercase tracking-wider text-text-faint">Due date{route === "adhoc" ? "" : " (optional)"}</label>
                <input type="date" required={route === "adhoc"} min={todayInputValue()} max={maxDueDateValue()} value={dueDate} onChange={(e) => setDueDate(e.target.value)} className={inputClass} />
              </div>
            </div>

            {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5">{error}</div>}

            <button type="submit" disabled={submitting} className="mt-1 flex items-center justify-center gap-1.5 bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors">
              {submitting && <Loader2 size={13} className="animate-spin" />}
              {submitting ? "Dispatching…" : "Dispatch"}
            </button>
          </form>
        )}
      </motion.div>

      <ReceiptModal
        target={
          receiptOpen && dispatched
            ? ({
                holderName: dispatched.assigneeName,
                note: "Issued at dispatch",
                items: [{ asset_name: asset.name, quantity: dispatched.quantity, due_date: dispatched.dueDate || null }],
              } satisfies ReceiptTarget)
            : null
        }
        onClose={() => setReceiptOpen(false)}
      />
    </AnimatePresence>
  );
}
