import { useEffect, useState } from "react";
import { useRequestGuard } from "../lib/useRequestGuard";
import { motion, AnimatePresence } from "framer-motion";
import { X, Loader2, Trash2, Plus, Search, UserPlus, CheckCircle2, CreditCard } from "lucide-react";
import { quotationsApi, usersApi, outsidersApi, formatPrice, formatDate, ApiError } from "../lib/api";
import type { QuotationCartOrDetail, CatalogAsset, UserRow, OutsiderRow } from "../lib/types";
import { StatusPill } from "./StatusPill";
import { ExportButtons } from "./ExportButtons";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

// Real quote lifecycle wording -- submitted -> approved -> fulfilled --
// same reasoning as pages/Quotations.tsx's own statusPillFor(): this used
// to recycle the checkout status pill's "Active"/"Returned"/"Pending"
// labels, which describe a physical loan, not a quote. See
// components/StatusPill.tsx's submitted/approved/fulfilled entries.
function statusPillFor(status: string | undefined) {
  switch (status) {
    case "approved":
      return <StatusPill status="approved" />;
    case "fulfilled":
      return <StatusPill status="fulfilled" />;
    case "paid":
      return <StatusPill status="paid" />;
    case "submitted":
      return <StatusPill status="submitted" />;
    default:
      return <StatusPill status="submitted" />;
  }
}

const today = () => new Date().toISOString().slice(0, 10);

/**
 * One drawer, two modes -- mirrors the legacy frontend's split between
 * "My Quote Detail" (js/components/quotation.js's renderMyQuoteDetail(),
 * self-service: qty/remove/add while status === "submitted", no notes/
 * discount/assign) and the Admin/Manager "Quote Detail" modal
 * (renderQuoteDetail(): same item editing but unlocked through
 * "approved" too, plus notes/discount/assign/approve/delete). Kept as
 * one component (not two) so the item-editing half -- the part both
 * sides actually share -- can't drift out of sync between them.
 */
export function QuoteDetailDrawer({
  mode,
  quotationId,
  catalog,
  onClose,
  onChanged,
}: {
  mode: "self" | "admin";
  quotationId: number | null;
  catalog: CatalogAsset[];
  onClose: () => void;
  onChanged?: () => void;
}) {
  const [data, setData] = useState<QuotationCartOrDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [discount, setDiscount] = useState("0");
  const [paymentMethod, setPaymentMethod] = useState("bank_transfer");
  const [paymentReference, setPaymentReference] = useState("");
  const [paymentConfirmed, setPaymentConfirmed] = useState(false);
  const [assetQuery, setAssetQuery] = useState("");
  const [selectedAssetId, setSelectedAssetId] = useState<number | null>(null);
  const [addQty, setAddQty] = useState("1");
  const [addStart, setAddStart] = useState(today());
  const [addDue, setAddDue] = useState(today());

  // Admin/Manager-only: "not currently in inventory" outsourced line --
  // mirrors the legacy frontend's #addOutsourcedItemForm (see
  // quotation.js's "Add outsourced (not currently in inventory) item"
  // section) but as its own toggleable panel alongside "Add another asset".
  const [addMode, setAddMode] = useState<"catalog" | "outsourced">("catalog");
  const [outName, setOutName] = useState("");
  const [outDescription, setOutDescription] = useState("");
  const [outPrice, setOutPrice] = useState("");
  const [outQty, setOutQty] = useState("1");
  const [outSourcedFrom, setOutSourcedFrom] = useState("");
  const [outStart, setOutStart] = useState(today());
  const [outDue, setOutDue] = useState(today());

  // Admin/Manager-only: assignment (Staff/Customer account vs Ad-Hoc/
  // unlinked individual) -- mirrors the legacy frontend's Issue/Dispatch-
  // style Staff/Customer/Ad-Hoc split (ui.js's toggleRoute()), simplified
  // to a single search-select for any linked User plus a separate Ad-Hoc
  // existing-or-new-profile form.
  const [assignOpen, setAssignOpen] = useState(false);
  const [assignTab, setAssignTab] = useState<"user" | "outsider">("user");
  const [userQuery, setUserQuery] = useState("");
  const [userMatches, setUserMatches] = useState<UserRow[]>([]);
  const [existingOutsiders, setExistingOutsiders] = useState<OutsiderRow[]>([]);
  const [outsiderChoice, setOutsiderChoice] = useState<"new" | number>("new");
  const [adhocName, setAdhocName] = useState("");
  const [adhocCompany, setAdhocCompany] = useState("");
  const [adhocEmail, setAdhocEmail] = useState("");
  const [adhocPhone, setAdhocPhone] = useState("");
  const beginRequest = useRequestGuard();

  const refresh = async () => {
    if (quotationId == null) return;
    const isCurrent = beginRequest();
    setLoading(true);
    setError(null);
    try {
      const detail = mode === "self" ? await quotationsApi.myQuoteDetail(quotationId) : await quotationsApi.detail(quotationId);
      if (!isCurrent()) return;
      setData(detail);
      setNotes(detail.notes ?? "");
      setDiscount(String(detail.discount_percent ?? 0));
      setPaymentMethod(detail.payment_method ?? "bank_transfer");
      setPaymentReference(detail.payment_reference ?? "");
      setPaymentConfirmed(false);
    } catch (err) {
      if (isCurrent()) setError(errMsg(err, "Couldn't load this quotation."));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  };

  useEffect(() => {
    setData(null);
    setAssetQuery("");
    setSelectedAssetId(null);
    setAddQty("1");
    setAddStart(today());
    setAddDue(today());
    setPaymentMethod("bank_transfer");
    setPaymentReference("");
    setPaymentConfirmed(false);
    setAddMode("catalog");
    setOutName(""); setOutDescription(""); setOutPrice(""); setOutQty("1"); setOutSourcedFrom("");
    setOutStart(today()); setOutDue(today());
    setAssignOpen(false); setAssignTab("user"); setUserQuery(""); setUserMatches([]);
    setOutsiderChoice("new"); setAdhocName(""); setAdhocCompany(""); setAdhocEmail(""); setAdhocPhone("");
    if (quotationId != null) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quotationId, mode]);

  // Search-as-you-type for the "assign to a linked user" tab, same
  // debounce-free pattern as the asset search below (small directory,
  // acceptable to just re-query on every keystroke).
  useEffect(() => {
    if (mode !== "admin" || assignTab !== "user" || !userQuery.trim()) { setUserMatches([]); return; }
    let cancelled = false;
    usersApi.list(8, 0, userQuery.trim()).then((page) => !cancelled && setUserMatches(page.items)).catch(() => {});
    return () => { cancelled = true; };
  }, [mode, assignTab, userQuery]);

  // Populate the "reuse an existing Ad-Hoc profile" dropdown once, the
  // first time the assign panel is opened in the Ad-Hoc tab.
  useEffect(() => {
    if (mode !== "admin" || assignTab !== "outsider" || existingOutsiders.length > 0) return;
    let cancelled = false;
    outsidersApi.list(100, 0, "").then((page) => { if (!cancelled) setExistingOutsiders(page.items); }).catch(() => {});
    return () => { cancelled = true; };
  }, [mode, assignTab, existingOutsiders.length]);

  if (quotationId == null) return null;

  // Self-service is only ever editable while "submitted". Admin/Manager
  // can make operational corrections through "fulfilled". A paid quote is
  // terminal and immutable. The backend enforces the same rule.
  const editable = mode === "self" ? data?.status === "submitted" : !data?.locked;

  const withBusy = async (key: string, fn: () => Promise<void>) => {
    setBusyKey(key);
    setError(null);
    try {
      await fn();
      onChanged?.();
    } catch (err) {
      setError(errMsg(err, "That didn't work."));
    } finally {
      setBusyKey(null);
    }
  };

  const changeQty = (item: NonNullable<QuotationCartOrDetail["items"]>[number], quantity: number) => {
    if (!quotationId || !item.item_id || quantity < 1) return;
    withBusy(`qty-${item.item_id}`, async () => {
      const updated = mode === "self"
        ? await quotationsApi.updateMyQuoteItem(quotationId, item.item_id!, quantity)
        : await quotationsApi.updateItem(quotationId, item.item_id!, quantity);
      setData(updated);
    });
  };

  const removeLine = (item: NonNullable<QuotationCartOrDetail["items"]>[number]) => {
    if (!quotationId || !item.item_id) return;
    withBusy(`remove-${item.item_id}`, async () => {
      const updated = mode === "self"
        ? await quotationsApi.removeMyQuoteItem(quotationId, item.item_id!)
        : await quotationsApi.removeItem(quotationId, item.item_id!);
      setData(updated);
    });
  };

  const addLine = () => {
    if (!quotationId || !selectedAssetId) { setError("Search for and select an asset to add."); return; }
    const quantity = parseInt(addQty, 10);
    if (!quantity || quantity < 1) { setError("Enter a quantity of at least 1."); return; }
    if (!addStart || !addDue || addDue < addStart) { setError("Pick a valid start/due date range."); return; }
    withBusy("add-line", async () => {
      const updated = mode === "self"
        ? await quotationsApi.addMyQuoteItem(quotationId, selectedAssetId, quantity, addStart, addDue)
        : await quotationsApi.addItem(quotationId, selectedAssetId, quantity, addStart, addDue);
      setData(updated);
      setAssetQuery("");
      setSelectedAssetId(null);
      setAddQty("1");
    });
  };

  const addOutsourcedLine = () => {
    if (!quotationId) return;
    const name = outName.trim();
    const price = Number(outPrice);
    const quantity = parseInt(outQty, 10);
    if (!name) { setError("Enter a name for the outsourced item."); return; }
    if (Number.isNaN(price) || price < 0) { setError("Enter a valid price per day."); return; }
    if (!quantity || quantity < 1) { setError("Enter a quantity of at least 1."); return; }
    if (!outStart || !outDue || outDue < outStart) { setError("Pick a valid start/due date range."); return; }
    withBusy("add-outsourced", async () => {
      const updated = await quotationsApi.addOutsourcedItem(quotationId, {
        name,
        description: outDescription.trim() || null,
        unit_price: price,
        quantity,
        sourced_from: outSourcedFrom.trim() || null,
        start_date: outStart,
        due_date: outDue,
      });
      setData(updated);
      setOutName(""); setOutDescription(""); setOutPrice(""); setOutQty("1"); setOutSourcedFrom("");
    });
  };

  const removeOutsourcedLine = (item: NonNullable<QuotationCartOrDetail["items"]>[number]) => {
    if (!quotationId || item.outsourced_item_id == null) return;
    withBusy(`remove-out-${item.outsourced_item_id}`, async () => {
      const updated = await quotationsApi.removeOutsourcedItem(quotationId, item.outsourced_item_id!);
      setData(updated);
    });
  };

  const assignToUser = (user: UserRow) => {
    if (!quotationId) return;
    withBusy("assign", async () => {
      const updated = await quotationsApi.assign(quotationId, { assignee_type: "user", user_id: user.id });
      setData(updated);
      setAssignOpen(false);
      setUserQuery("");
    });
  };

  const clearAssignment = () => {
    if (!quotationId) return;
    withBusy("assign", async () => setData(await quotationsApi.assign(quotationId, { assignee_type: null })));
  };

  const submitAdhocAssign = () => {
    if (!quotationId) return;
    const payload: Record<string, unknown> = { assignee_type: "outsider" };
    if (outsiderChoice !== "new") {
      payload.outsider_id = outsiderChoice;
    } else {
      const name = adhocName.trim();
      const email = adhocEmail.trim();
      const phone = adhocPhone.trim();
      if (!name || (!email && !phone)) { setError("Name and at least one of email/phone are required for an Ad-Hoc individual."); return; }
      payload.outsider_name = name;
      payload.outsider_email = email || null;
      payload.outsider_phone = phone || null;
      payload.outsider_company = adhocCompany.trim() || null;
    }
    withBusy("assign", async () => {
      const updated = await quotationsApi.assign(quotationId, payload);
      setData(updated);
      setAssignOpen(false);
      setOutsiderChoice("new");
      setAdhocName(""); setAdhocCompany(""); setAdhocEmail(""); setAdhocPhone("");
    });
  };

  const saveNotes = () => {
    if (!quotationId) return;
    withBusy("notes", async () => setData(await quotationsApi.saveNotes(quotationId, notes)));
  };

  const saveDiscount = () => {
    if (!quotationId) return;
    const pct = Number(discount);
    if (Number.isNaN(pct) || pct < 0 || pct > 100) { setError("Discount must be a number between 0 and 100."); return; }
    withBusy("discount", async () => setData(await quotationsApi.saveDiscount(quotationId, pct)));
  };

  const approve = () => {
    if (!quotationId) return;
    withBusy("approve", async () => setData(await quotationsApi.approve(quotationId)));
  };

  const markPaid = () => {
    if (!quotationId || data?.status !== "fulfilled") return;
    if (!paymentConfirmed) { setError("Confirm that payment has been received before marking this quotation as paid."); return; }
    if (!paymentMethod) { setError("Select a payment method."); return; }
    withBusy("paid", async () => {
      setData(await quotationsApi.markPaid(quotationId, paymentMethod, paymentReference.trim() || null));
      setPaymentConfirmed(false);
    });
  };

  const remove = () => {
    if (!quotationId) return;
    if (!confirm(`Permanently delete ${data?.reference_number ?? "this quotation"}? This cannot be undone.`)) return;
    withBusy("delete", async () => {
      await quotationsApi.remove(quotationId);
      onChanged?.();
      onClose();
    });
  };

  const matches = assetQuery.trim()
    ? catalog.filter((a) => a.name.toLowerCase().includes(assetQuery.trim().toLowerCase()) || (a.category ?? "").toLowerCase().includes(assetQuery.trim().toLowerCase())).slice(0, 8)
    : [];

  return (
    <AnimatePresence>
      <motion.div key="backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40" />
      <motion.div key="panel"
        initial={{ opacity: 0, x: 24 }}
        animate={{ opacity: 1, x: 0 }}
        exit={{ opacity: 0, x: 16 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="fixed right-0 top-0 bottom-0 z-50 w-full max-w-lg bg-surface border-l border-border-soft p-6 overflow-y-auto"
      >
        <div className="flex items-start justify-between mb-1">
          <div>
            <p className="text-[11px] uppercase tracking-wider text-text-faint">Quotation</p>
            <h2 className="font-display text-lg font-semibold text-text">{data?.reference_number ?? "Loading…"}</h2>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {quotationId != null && (
              <ExportButtons
                compact
                formats={["pdf"]}
                urlFor={() => (mode === "self" ? quotationsApi.exportMyQuoteUrl(quotationId) : quotationsApi.exportQuoteUrl(quotationId))}
                filenameFor={() => `quotation_${quotationId}.pdf`}
              />
            )}
            <button onClick={onClose} className="text-text-faint hover:text-text transition-colors"><X size={18} /></button>
          </div>
        </div>

        {loading && <p className="text-[12px] text-text-faint text-center py-10">Loading…</p>}
        {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5 my-3">{error}</div>}

        {data && !loading && (
          <>
            <div className="flex items-center gap-3 mt-2 mb-4">
              {statusPillFor(data.status)}
              {data.submitted_at && <span className="text-[11.5px] text-text-faint">Submitted {formatDate(data.submitted_at)}</span>}
            </div>

            {mode === "admin" && data.requester && (
              <p className="text-[12.5px] text-text-muted mb-1">
                Requested by <span className="text-text">{data.requester.name}</span> {data.requester.email ? `(${data.requester.email})` : ""}
              </p>
            )}
            {mode === "admin" && (
              <div className="mb-4">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-[12.5px] text-text-muted">
                    Assigned to{" "}
                    <span className="text-text">
                      {data.assigned_to ? data.assigned_to.name : data.assigned_outsider ? `${data.assigned_outsider.name} (Ad-Hoc)` : "Unassigned"}
                    </span>
                  </p>
                  {!data.locked && (
                    <div className="flex items-center gap-2 shrink-0">
                      <button onClick={() => setAssignOpen((v) => !v)} className="flex items-center gap-1 text-[11.5px] font-medium text-brass-soft hover:underline">
                        <UserPlus size={11} /> {assignOpen ? "Cancel" : "Change"}
                      </button>
                      {(data.assigned_to || data.assigned_outsider) && (
                        <button onClick={clearAssignment} disabled={busyKey === "assign"} className="text-[11.5px] font-medium text-text-faint hover:text-rust-soft">
                          Unassign
                        </button>
                      )}
                    </div>
                  )}
                </div>

                {assignOpen && !data.locked && (
                  <div className="border border-border-soft rounded-[3px] p-3 mt-2">
                    <div className="flex items-center gap-1 mb-2.5">
                      <button
                        onClick={() => setAssignTab("user")}
                        className={`px-2.5 py-1 text-[11.5px] font-medium rounded-[3px] transition-colors ${assignTab === "user" ? "bg-brass/15 text-brass-soft" : "text-text-faint hover:text-text"}`}
                      >
                        Staff / Customer
                      </button>
                      <button
                        onClick={() => setAssignTab("outsider")}
                        className={`px-2.5 py-1 text-[11.5px] font-medium rounded-[3px] transition-colors ${assignTab === "outsider" ? "bg-brass/15 text-brass-soft" : "text-text-faint hover:text-text"}`}
                      >
                        Ad-Hoc individual
                      </button>
                    </div>

                    {assignTab === "user" ? (
                      <div className="relative">
                        <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
                        <input
                          value={userQuery}
                          onChange={(e) => setUserQuery(e.target.value)}
                          placeholder="Search staff or customers by name/email…"
                          className="w-full bg-ink-soft border border-border-soft rounded-[3px] pl-8 pr-3 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none"
                        />
                        {userMatches.length > 0 && (
                          <div className="border border-border-soft rounded-[3px] mt-2 overflow-hidden">
                            {userMatches.map((u) => (
                              <button
                                key={u.id}
                                type="button"
                                onClick={() => assignToUser(u)}
                                disabled={busyKey === "assign"}
                                className="block w-full px-3 py-2 text-left text-[12px] hover:bg-ink-soft transition-colors disabled:opacity-50"
                              >
                                <span className="text-text font-medium">{u.name}</span>
                                <span className="text-text-faint"> · {u.email} · {u.role}</span>
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="flex flex-col gap-2">
                        <select
                          value={String(outsiderChoice)}
                          onChange={(e) => setOutsiderChoice(e.target.value === "new" ? "new" : Number(e.target.value))}
                          className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none"
                        >
                          <option value="new">+ Create new unlinked profile</option>
                          {existingOutsiders.map((o) => (
                            <option key={o.id} value={o.id}>{o.name}{o.company ? ` (${o.company})` : ""}</option>
                          ))}
                        </select>
                        {outsiderChoice === "new" && (
                          <>
                            <input value={adhocName} onChange={(e) => setAdhocName(e.target.value)} placeholder="Name" className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
                            <input value={adhocCompany} onChange={(e) => setAdhocCompany(e.target.value)} placeholder="Company (optional)" className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
                            <div className="grid grid-cols-2 gap-2">
                              <input value={adhocEmail} onChange={(e) => setAdhocEmail(e.target.value)} placeholder="Email" className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
                              <input value={adhocPhone} onChange={(e) => setAdhocPhone(e.target.value)} placeholder="Phone" className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
                            </div>
                            <p className="text-[10.5px] text-text-faint">Name plus at least one of email/phone is required.</p>
                          </>
                        )}
                        <button
                          onClick={submitAdhocAssign}
                          disabled={busyKey === "assign"}
                          className="w-full flex items-center justify-center gap-1.5 bg-brass/15 hover:bg-brass/25 disabled:opacity-60 text-brass-soft text-[12px] font-medium rounded-[3px] py-1.5 transition-colors"
                        >
                          {busyKey === "assign" ? <Loader2 size={12} className="animate-spin" /> : <UserPlus size={12} />} Assign to Ad-Hoc individual
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {!editable && (
              <div className="border border-border-soft bg-ink-soft text-text-faint text-[12px] rounded-[3px] px-3 py-2.5 mb-4">
                {data.status === "paid"
                  ? `Paid ${data.paid_at ? formatDate(data.paid_at) : ""} — this quotation is locked as a financial record.`
                  : mode === "self" && data.status === "fulfilled"
                    ? `Fulfilled ${data.fulfilled_at ? formatDate(data.fulfilled_at) : ""}.`
                    : "This quote can no longer be edited from here."}
              </div>
            )}

            <div className="flex flex-col gap-2.5 mb-4">
              {data.items.length === 0 && <p className="text-[12px] text-text-faint text-center py-6">No items on this quote.</p>}
              {data.items.map((li) => {
                const key = li.item_id ?? `out-${li.outsourced_item_id}`;
                return (
                  <div key={key} className="border border-border-soft rounded-[3px] p-3.5">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-[13px] text-text font-medium truncate">
                          {li.asset_name}
                          {/* Whether a line was sourced from inventory or
                              outsourced to an external vendor is internal
                              fulfillment detail -- a customer/staff
                              requester doesn't need (and shouldn't see) the
                              sourcing explanation, just the item, qty, and
                              price they asked for. Matches the legacy
                              frontend's self-service quote view (js/
                              components/quotation.js's renderMyQuoteDetail()),
                              which deliberately omits this badge entirely
                              rather than just hiding the vendor name. */}
                          {mode === "admin" && li.is_outsourced && (
                            <span className="ml-1.5 inline-flex items-center rounded-full bg-brass/15 px-1.5 py-0.5 text-[10px] font-medium text-brass-soft">
                              Outsourced{li.sourced_from ? ` · ${li.sourced_from}` : ""}
                            </span>
                          )}
                        </p>
                        <p className="text-[11px] text-text-faint mt-0.5">{li.start_date} → {li.due_date} ({li.days}d)</p>
                      </div>
                      {editable && !li.is_outsourced && li.item_id != null && (
                        <button
                          onClick={() => removeLine(li)}
                          disabled={busyKey === `remove-${li.item_id}`}
                          title="Remove from quote"
                          className="shrink-0 rounded-[3px] border border-border-soft p-1.5 text-text-faint hover:border-rust/40 hover:text-rust-soft transition-colors disabled:opacity-50"
                        >
                          {busyKey === `remove-${li.item_id}` ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                        </button>
                      )}
                      {/* Outsourced lines can only be removed by an Admin/Manager
                          (mode === "self" has no route for this -- see the
                          requester-visible-but-not-editable note above). */}
                      {mode === "admin" && editable && li.is_outsourced && li.outsourced_item_id != null && (
                        <button
                          onClick={() => removeOutsourcedLine(li)}
                          disabled={busyKey === `remove-out-${li.outsourced_item_id}`}
                          title="Remove outsourced line"
                          className="shrink-0 rounded-[3px] border border-border-soft p-1.5 text-text-faint hover:border-rust/40 hover:text-rust-soft transition-colors disabled:opacity-50"
                        >
                          {busyKey === `remove-out-${li.outsourced_item_id}` ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                        </button>
                      )}
                    </div>
                    <div className="flex items-center justify-between gap-3 mt-2.5">
                      {editable && !li.is_outsourced && li.item_id != null ? (
                        <input
                          type="number"
                          min={1}
                          value={li.quantity}
                          onChange={(e) => changeQty(li, parseInt(e.target.value, 10) || 1)}
                          disabled={busyKey === `qty-${li.item_id}`}
                          className="w-16 rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1 text-[12px] text-text focus:border-brass/50 focus:outline-none"
                        />
                      ) : (
                        <span className="text-[12px] font-mono text-text-muted">qty {li.quantity}</span>
                      )}
                      <p className="font-mono text-[13px] text-text">{formatPrice(li.line_total)}</p>
                    </div>
                  </div>
                );
              })}
            </div>

            {editable && (
              <div className="border border-border-soft rounded-[3px] p-3.5 mb-4">
                <div className="flex items-center justify-between mb-2">
                  <p className="text-[11px] uppercase tracking-wider text-text-faint">
                    {addMode === "catalog" ? "Add another asset" : "Add outsourced line"}
                  </p>
                  {mode === "admin" && (
                    <button
                      onClick={() => setAddMode((m) => (m === "catalog" ? "outsourced" : "catalog"))}
                      className="text-[11px] font-medium text-brass-soft hover:underline"
                    >
                      {addMode === "catalog" ? "Not in inventory?" : "Pick from catalog instead"}
                    </button>
                  )}
                </div>
                {addMode === "outsourced" && mode === "admin" ? (
                  <div className="flex flex-col gap-2">
                    <p className="text-[11px] text-text-faint -mt-1 mb-0.5">
                      A one-off line for equipment sourced outside inventory -- the requester can see it but only a Manager/Admin can edit or remove it.
                    </p>
                    <input value={outName} onChange={(e) => setOutName(e.target.value)} placeholder="Item name" className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
                    <input value={outDescription} onChange={(e) => setOutDescription(e.target.value)} placeholder="Description (optional)" className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
                    <div className="grid grid-cols-2 gap-2">
                      <input type="number" min={0} step={0.01} value={outPrice} onChange={(e) => setOutPrice(e.target.value)} placeholder="Price/day" className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
                      <input type="number" min={1} value={outQty} onChange={(e) => setOutQty(e.target.value)} placeholder="Qty" className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
                    </div>
                    <input value={outSourcedFrom} onChange={(e) => setOutSourcedFrom(e.target.value)} placeholder="Sourced from (optional vendor note)" className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
                    <div className="grid grid-cols-2 gap-2">
                      <input type="date" min={today()} value={outStart} onChange={(e) => setOutStart(e.target.value)} className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none" />
                      <input type="date" min={today()} value={outDue} onChange={(e) => setOutDue(e.target.value)} className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none" />
                    </div>
                    <button
                      onClick={addOutsourcedLine}
                      disabled={busyKey === "add-outsourced"}
                      className="w-full flex items-center justify-center gap-1.5 bg-brass/15 hover:bg-brass/25 disabled:opacity-60 text-brass-soft text-[12px] font-medium rounded-[3px] py-2 transition-colors"
                    >
                      {busyKey === "add-outsourced" ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />} Add outsourced line
                    </button>
                  </div>
                ) : (
                <>
                <div className="relative mb-2">
                  <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
                  <input
                    value={selectedAssetId ? catalog.find((a) => a.id === selectedAssetId)?.name ?? assetQuery : assetQuery}
                    onChange={(e) => { setAssetQuery(e.target.value); setSelectedAssetId(null); }}
                    placeholder="Search assets…"
                    className="w-full bg-ink-soft border border-border-soft rounded-[3px] pl-8 pr-3 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none"
                  />
                </div>
                {matches.length > 0 && !selectedAssetId && (
                  <div className="border border-border-soft rounded-[3px] mb-2 overflow-hidden">
                    {matches.map((a) => (
                      <button
                        key={a.id}
                        type="button"
                        onClick={() => { setSelectedAssetId(a.id); setAssetQuery(""); }}
                        className="block w-full px-3 py-2 text-left text-[12px] hover:bg-ink-soft transition-colors"
                      >
                        <span className="text-text font-medium">{a.name}</span>
                        <span className="text-text-faint"> · {a.category ?? "—"}{a.price != null ? ` · ${formatPrice(a.price)}/day` : ""}</span>
                      </button>
                    ))}
                  </div>
                )}
                <div className="grid grid-cols-3 gap-2 mb-2">
                  <input type="number" min={1} value={addQty} onChange={(e) => setAddQty(e.target.value)} placeholder="Qty" className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none" />
                  <input type="date" min={today()} value={addStart} onChange={(e) => setAddStart(e.target.value)} className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none" />
                  <input type="date" min={today()} value={addDue} onChange={(e) => setAddDue(e.target.value)} className="rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none" />
                </div>
                <button
                  onClick={addLine}
                  disabled={busyKey === "add-line"}
                  className="w-full flex items-center justify-center gap-1.5 bg-brass/15 hover:bg-brass/25 disabled:opacity-60 text-brass-soft text-[12px] font-medium rounded-[3px] py-2 transition-colors"
                >
                  {busyKey === "add-line" ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />} Add line
                </button>
                </>
                )}
              </div>
            )}

            {mode === "admin" && (
              <div className="border border-border-soft rounded-[3px] p-3.5 mb-4">
                <label className="block mb-3">
                  <span className="text-[11px] uppercase tracking-wider text-text-faint">Notes</span>
                  <textarea
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    disabled={data.locked}
                    rows={2}
                    className="w-full mt-1.5 bg-ink-soft border border-border-soft rounded-[3px] px-3 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none resize-none disabled:opacity-60"
                  />
                </label>
                {!data.locked && (
                  <button onClick={saveNotes} disabled={busyKey === "notes"} className="text-[11.5px] font-medium text-brass-soft hover:underline mb-3">
                    {busyKey === "notes" ? "Saving…" : "Save notes"}
                  </button>
                )}
                <label className="flex items-center gap-2 text-[12.5px] text-text-muted">
                  Discount %
                  <input
                    type="number"
                    min={0}
                    max={100}
                    value={discount}
                    onChange={(e) => setDiscount(e.target.value)}
                    disabled={data.locked}
                    className="w-20 rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1 text-[12px] text-text focus:border-brass/50 focus:outline-none disabled:opacity-60"
                  />
                  {!data.locked && (
                    <button onClick={saveDiscount} disabled={busyKey === "discount"} className="text-[11.5px] font-medium text-brass-soft hover:underline">
                      {busyKey === "discount" ? "Saving…" : "Save"}
                    </button>
                  )}
                </label>
              </div>
            )}

            {mode === "admin" && data.status === "fulfilled" && (
              <div className="border border-moss/30 bg-moss/5 rounded-[4px] p-3.5 mb-4">
                <div className="flex items-start gap-2 mb-3">
                  <CreditCard size={15} className="text-moss-soft mt-0.5 shrink-0" />
                  <div>
                    <p className="text-[12.5px] font-medium text-text">Record payment</p>
                    <p className="text-[11px] text-text-faint mt-0.5">Payment is the final step. Once marked paid, this quotation becomes read-only.</p>
                  </div>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-2.5">
                  <label className="text-[11px] text-text-faint">
                    Payment method
                    <select value={paymentMethod} onChange={(e) => setPaymentMethod(e.target.value)} className="w-full mt-1 rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none">
                      <option value="bank_transfer">Bank transfer</option>
                      <option value="card">Card</option>
                      <option value="pos">POS</option>
                      <option value="cash">Cash</option>
                      <option value="other">Other</option>
                    </select>
                  </label>
                  <label className="text-[11px] text-text-faint">
                    Payment reference <span className="normal-case">(optional)</span>
                    <input value={paymentReference} onChange={(e) => setPaymentReference(e.target.value)} placeholder="Transfer ID / receipt no." className="w-full mt-1 rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
                  </label>
                </div>
                <label className="flex items-start gap-2 text-[11.5px] text-text-muted mb-3 cursor-pointer">
                  <input type="checkbox" checked={paymentConfirmed} onChange={(e) => setPaymentConfirmed(e.target.checked)} className="mt-0.5" />
                  <span>I confirm the customer payment has been received and verified.</span>
                </label>
                <button onClick={markPaid} disabled={!paymentConfirmed || busyKey === "paid"} className="w-full flex items-center justify-center gap-1.5 bg-moss/15 hover:bg-moss/25 disabled:opacity-50 text-moss-soft text-[12px] font-medium rounded-[3px] py-2 transition-colors">
                  {busyKey === "paid" ? <Loader2 size={13} className="animate-spin" /> : <CheckCircle2 size={13} />}
                  {busyKey === "paid" ? "Recording payment…" : "Mark as paid"}
                </button>
              </div>
            )}

            {mode === "admin" && data.status === "paid" && (
              <div className="border border-moss/30 bg-moss/5 rounded-[4px] p-3 mb-4">
                <div className="flex items-center gap-2 text-[12px] text-moss-soft"><CheckCircle2 size={14} /> Payment recorded</div>
                <p className="text-[11px] text-text-faint mt-1.5">{data.paid_at ? `Paid ${formatDate(data.paid_at)}` : "Paid"}{data.payment_method ? ` · ${data.payment_method.replace("_", " ")}` : ""}{data.payment_reference ? ` · ${data.payment_reference}` : ""}</p>
                {data.paid_by?.name && <p className="text-[10.5px] text-text-faint mt-1">Recorded by {data.paid_by.name}</p>}
              </div>
            )}

            <div className="border-t border-border-soft pt-3 mb-4 flex flex-col gap-1.5 text-[12.5px]">
              <div className="flex justify-between text-text-muted"><span>Subtotal</span><span className="font-mono">{formatPrice(data.subtotal)}</span></div>
              {!!data.discount_percent && (
                <div className="flex justify-between text-text-muted"><span>Discount ({data.discount_percent}%)</span><span className="font-mono">-{formatPrice(data.discount_amount)}</span></div>
              )}
              <div className="flex justify-between text-text-muted"><span>VAT ({data.vat_percent}%)</span><span className="font-mono">{formatPrice(data.vat_amount)}</span></div>
              <div className="flex justify-between text-text font-medium text-[14px] pt-1"><span>Total</span><span className="font-mono">{formatPrice(data.total)}</span></div>
            </div>

            {mode === "admin" && (
              <div className="flex gap-2">
                {data.status === "submitted" && (
                  <button onClick={approve} disabled={busyKey === "approve"} className="flex-1 bg-moss/15 hover:bg-moss/25 disabled:opacity-60 text-moss-soft text-[12.5px] font-medium rounded-[3px] py-2 transition-colors">
                    {busyKey === "approve" ? "Approving…" : "Approve"}
                  </button>
                )}
                {(data.status === "submitted" || data.status === "approved") && (
                  <button onClick={remove} disabled={busyKey === "delete"} className="flex-1 bg-rust/10 hover:bg-rust/20 disabled:opacity-60 text-rust-soft text-[12.5px] font-medium rounded-[3px] py-2 transition-colors">
                    {busyKey === "delete" ? "Deleting…" : "Delete quotation"}
                  </button>
                )}
              </div>
            )}
          </>
        )}
      </motion.div>
    </AnimatePresence>
  );
}
