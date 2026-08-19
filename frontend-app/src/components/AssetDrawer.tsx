import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Link } from "react-router-dom";
import { X, Tag, DollarSign, Layers, Pencil, Check, Loader2, Trash2, PackageCheck, Wrench, ShieldAlert, ShoppingCart } from "lucide-react";
import type { AssetDetails, AssetActiveAssignment, AssetExceptionItem } from "../lib/types";
import { assetsApi, checkoutsApi, quotationsApi, formatPrice, ApiError } from "../lib/api";
import { useRequestGuard } from "../lib/useRequestGuard";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

const today = () => new Date().toISOString().slice(0, 10);

// ---------------------------------------------------------------------------
// Small "display value / edit field" toggle shared by rename, category,
// price, and capacity -- mirrors legacy js/ui.js's toggleNameEdit() /
// toggleCategoryEdit() / togglePriceEdit() / toggleCapacityEdit() pattern,
// each field independently switching between a read-only line and an
// inline `<input>` + Save/Cancel.
// ---------------------------------------------------------------------------
function InlineField({
  label,
  icon,
  display,
  onSave,
  renderInput,
}: {
  label: string;
  icon: React.ReactNode;
  display: React.ReactNode;
  onSave: () => Promise<void>;
  renderInput: () => React.ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await onSave();
      setEditing(false);
    } catch (err) {
      setError(errMsg(err, "Couldn't save that change."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="border border-border-soft rounded-[3px] p-3">
      <div className="flex items-center justify-between">
        <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5">{icon}{label}</p>
        {!editing && (
          <button onClick={() => setEditing(true)} className="text-text-faint hover:text-brass-soft transition-colors">
            <Pencil size={11} />
          </button>
        )}
      </div>
      {!editing ? (
        <p className="text-sm text-text mt-1">{display}</p>
      ) : (
        <div className="mt-1.5 flex items-center gap-1.5">
          {renderInput()}
          <button onClick={save} disabled={saving} className="shrink-0 text-moss-soft hover:text-moss disabled:opacity-50 transition-colors">
            {saving ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
          </button>
          <button onClick={() => { setEditing(false); setError(null); }} disabled={saving} className="shrink-0 text-text-faint hover:text-text transition-colors">
            <X size={13} />
          </button>
        </div>
      )}
      {error && <p className="text-[11px] text-rust-soft mt-1.5">{error}</p>}
    </div>
  );
}

function AssignmentRow({ item, onReturned }: { item: AssetActiveAssignment; onReturned: () => void }) {
  const [qty, setQty] = useState(item.outstanding);
  const [processing, setProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const processReturn = async () => {
    setProcessing(true);
    setError(null);
    try {
      await checkoutsApi.returnItem(item.checkout_id, qty);
      onReturned();
    } catch (err) {
      setError(errMsg(err, "Return failed."));
    } finally {
      setProcessing(false);
    }
  };

  return (
    <div className="border border-border-soft rounded-[3px] p-3">
      <p className="text-[13px] text-text font-medium">{item.assignee_name} <span className="text-text-faint font-normal">({item.assignee_type})</span></p>
      <p className="text-[11px] text-text-faint font-mono mt-0.5">outstanding {item.outstanding} / {item.quantity} · due {item.due_date}</p>
      <div className="mt-2 flex items-center gap-2">
        <input
          type="number"
          min={1}
          max={item.outstanding}
          value={qty}
          onChange={(e) => setQty(Math.min(item.outstanding, Math.max(1, parseInt(e.target.value, 10) || 1)))}
          className="w-16 bg-ink-soft border border-border-soft rounded-[3px] px-2 py-1.5 text-[12px] text-text focus:border-moss/50 focus:outline-none"
        />
        <button
          onClick={processReturn}
          disabled={processing}
          className="flex-1 flex items-center justify-center gap-1.5 bg-moss/90 hover:bg-moss disabled:opacity-60 text-ink text-[11.5px] font-medium rounded-[3px] py-1.5 transition-colors"
        >
          {processing ? <Loader2 size={11} className="animate-spin" /> : <PackageCheck size={11} />}
          {processing ? "Processing…" : "Process return"}
        </button>
      </div>
      {error && <p className="text-[11px] text-rust-soft mt-1.5">{error}</p>}
    </div>
  );
}

function IsolatedRow({ assetId, item, onRecalled }: { assetId: number; item: AssetExceptionItem; onRecalled: () => void }) {
  const [recalling, setRecalling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const recall = async () => {
    setRecalling(true);
    setError(null);
    try {
      await assetsApi.recallException(assetId, item.exception_id);
      onRecalled();
    } catch (err) {
      setError(errMsg(err, "Recall failed."));
    } finally {
      setRecalling(false);
    }
  };

  return (
    <div className="border border-brass/30 bg-brass/5 rounded-[3px] p-3 flex items-center justify-between gap-3">
      <div className="min-w-0">
        <p className="font-mono text-[12px] text-text font-medium">{item.serial}</p>
        <p className="text-[11px] text-text-faint">{item.notes || "No notes on file"}</p>
        {error && <p className="text-[11px] text-rust-soft mt-1">{error}</p>}
      </div>
      <button
        onClick={recall}
        disabled={recalling}
        className="shrink-0 flex items-center gap-1.5 border border-moss/40 hover:bg-moss/10 disabled:opacity-60 text-moss-soft text-[11px] font-medium rounded-[3px] px-2.5 py-1.5 transition-colors"
      >
        {recalling ? <Loader2 size={11} className="animate-spin" /> : null}
        {recalling ? "Recalling…" : "Recall to service"}
      </button>
    </div>
  );
}

function ExceptionForm({ assetId, onLogged }: { assetId: number; onLogged: () => void }) {
  const [serial, setSerial] = useState("");
  const [status, setStatus] = useState("Under Repair");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await assetsApi.flagException(assetId, { serial_number: serial, status_label: status, notes: notes || null });
      setSerial("");
      setNotes("");
      onLogged();
    } catch (err) {
      setError(errMsg(err, "Couldn't log that exception."));
    } finally {
      setSubmitting(false);
    }
  };

  const inputClass = "bg-ink-soft border border-border-soft rounded-[3px] px-2.5 py-2 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none";

  return (
    <form onSubmit={submit} className="flex flex-col gap-2 border border-border-soft rounded-[3px] p-3">
      <input required value={serial} onChange={(e) => setSerial(e.target.value)} placeholder="Serial / asset tag number" className={inputClass} />
      <select value={status} onChange={(e) => setStatus(e.target.value)} className={inputClass}>
        <option>Under Repair</option>
        <option>Stolen</option>
        <option>Missing</option>
      </select>
      <textarea value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Notes (optional)" rows={2} className={inputClass} />
      {error && <p className="text-[11px] text-rust-soft">{error}</p>}
      <button type="submit" disabled={submitting} className="flex items-center justify-center gap-1.5 border border-brass/40 hover:bg-brass/10 disabled:opacity-60 text-brass-soft text-[12px] font-medium rounded-[3px] py-2 transition-colors">
        {submitting ? <Loader2 size={12} className="animate-spin" /> : <Wrench size={12} />}
        {submitting ? "Logging…" : "Log isolated unit"}
      </button>
    </form>
  );
}

function NameField({ assetId, name, onSaved }: { assetId: number; name: string; onSaved: () => void }) {
  const [value, setValue] = useState(name);
  useEffect(() => setValue(name), [name]);
  return (
    <InlineField
      label="Name"
      icon={<Tag size={11} />}
      display={name}
      renderInput={() => (
        <input autoFocus value={value} onChange={(e) => setValue(e.target.value)} className="min-w-0 flex-1 bg-ink-soft border border-border-soft rounded-[3px] px-2 py-1.5 text-[12.5px] text-text focus:border-brass/50 focus:outline-none" />
      )}
      onSave={async () => {
        if (!value.trim()) throw new Error("Asset name cannot be empty.");
        await assetsApi.updateName(assetId, value.trim());
        onSaved();
      }}
    />
  );
}

function CategoryField({ assetId, category, onSaved }: { assetId: number; category: string | null; onSaved: () => void }) {
  const [value, setValue] = useState(category ?? "");
  useEffect(() => setValue(category ?? ""), [category]);
  return (
    <InlineField
      label="Category"
      icon={<Tag size={11} />}
      display={category ?? "No category set"}
      renderInput={() => (
        <input autoFocus value={value} onChange={(e) => setValue(e.target.value)} placeholder="e.g. Engineering" className="min-w-0 flex-1 bg-ink-soft border border-border-soft rounded-[3px] px-2 py-1.5 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
      )}
      onSave={async () => {
        await assetsApi.updateCategory(assetId, value.trim() || null);
        onSaved();
      }}
    />
  );
}

function DepartmentField({ assetId, department, onSaved }: { assetId: number; department: string | null; onSaved: () => void }) {
  const [value, setValue] = useState(department ?? "");
  useEffect(() => setValue(department ?? ""), [department]);
  return (
    <InlineField
      label="Department"
      icon={<Tag size={11} />}
      display={department ?? "No department set"}
      renderInput={() => (
        <>
          <input list="asset-department-options" autoFocus value={value} onChange={(e) => setValue(e.target.value)} placeholder="e.g. Camera, Lighting, Grip" className="min-w-0 flex-1 bg-ink-soft border border-border-soft rounded-[3px] px-2 py-1.5 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
          <datalist id="asset-department-options">
            <option value="Camera" />
            <option value="Lighting" />
            <option value="Grip" />
            <option value="Audio" />
            <option value="Power" />
            <option value="Production" />
          </datalist>
        </>
      )}
      onSave={async () => {
        await assetsApi.updateDepartment(assetId, value.trim() || null);
        onSaved();
      }}
    />
  );
}

function PriceField({ assetId, price, onSaved }: { assetId: number; price: number | null; onSaved: () => void }) {
  const [value, setValue] = useState(price != null ? String(price) : "");
  useEffect(() => setValue(price != null ? String(price) : ""), [price]);
  return (
    <InlineField
      label="Unit price"
      icon={<DollarSign size={11} />}
      display={price != null ? formatPrice(price) : "No price set"}
      renderInput={() => (
        <input autoFocus type="number" min={0} step="0.01" value={value} onChange={(e) => setValue(e.target.value)} placeholder="0.00" className="min-w-0 flex-1 bg-ink-soft border border-border-soft rounded-[3px] px-2 py-1.5 text-[12.5px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none" />
      )}
      onSave={async () => {
        const raw = value.trim();
        if (raw && (isNaN(Number(raw)) || Number(raw) < 0)) throw new Error("Price must be a non-negative number.");
        await assetsApi.updatePrice(assetId, raw ? Number(raw) : null);
        onSaved();
      }}
    />
  );
}

// Full-width capacity editor -- kept separate from the 2-col grid of
// Name/Category/Price above since Available is derived server-side
// (Available = Total - Outbound - Isolated) and only Total is ever
// directly editable.
function CapacityField({ assetId, total, available, onSaved }: { assetId: number; total: number; available: number; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(String(total));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setValue(String(total)), [total]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const n = parseInt(value, 10);
      if (isNaN(n) || n < 0) throw new Error("Total capacity must be a non-negative whole number.");
      await assetsApi.updateQuantity(assetId, n);
      setEditing(false);
      onSaved();
    } catch (err) {
      setError(errMsg(err, "Couldn't update capacity."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="border border-border-soft rounded-[3px] p-3 mt-3">
      <div className="flex items-center justify-between">
        <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5"><Layers size={11} />Available / Total capacity</p>
        {!editing && (
          <button onClick={() => setEditing(true)} className="text-text-faint hover:text-brass-soft transition-colors"><Pencil size={11} /></button>
        )}
      </div>
      {!editing ? (
        <p className="font-mono text-xl text-text mt-1">{available}<span className="text-text-faint text-sm">/{total}</span></p>
      ) : (
        <div className="mt-1.5 flex items-center gap-1.5">
          <input autoFocus type="number" min={0} value={value} onChange={(e) => setValue(e.target.value)} className="min-w-0 flex-1 bg-ink-soft border border-border-soft rounded-[3px] px-2 py-1.5 text-[12.5px] text-text focus:border-brass/50 focus:outline-none" />
          <button onClick={save} disabled={saving} className="shrink-0 text-moss-soft hover:text-moss disabled:opacity-50 transition-colors">
            {saving ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
          </button>
          <button onClick={() => { setEditing(false); setError(null); }} disabled={saving} className="shrink-0 text-text-faint hover:text-text transition-colors"><X size={13} /></button>
        </div>
      )}
      {error && <p className="text-[11px] text-rust-soft mt-1.5">{error}</p>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// "Add to quote" -- lets whoever opened this pool (any signed-in role;
// POST /quotations/items only requires get_current_user, same as the
// Quotation Catalog's own "Add" button in Quotations.tsx) drop it straight
// into their own draft order without leaving Inventory to hunt it down
// again on the Quotations page. Deliberately shown regardless of
// canManage/canDispatch -- a Staff/Customer session (neither) otherwise
// has no action at all on this drawer, while a Manager/Admin/Super Admin
// still wants a personal order alongside their management/dispatch tools.
// Same qty + start/due-date shape as the Quotation Catalog row, so the two
// entry points behave identically.
// ---------------------------------------------------------------------------
function AddToQuoteField({ assetId, price }: { assetId: number; price: number | null }) {
  const [quantity, setQuantity] = useState("1");
  const [start, setStart] = useState(today());
  const [due, setDue] = useState(today());
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [added, setAdded] = useState(false);

  const clearFeedback = () => {
    if (added) setAdded(false);
    if (error) setError(null);
  };

  const submit = async () => {
    const qty = parseInt(quantity, 10);
    if (!qty || qty < 1) { setError("Enter a quantity of at least 1."); return; }
    if (!start || !due) { setError("Pick both a start and a due date."); return; }
    if (due < start) { setError("Due date cannot be before the start date."); return; }
    setError(null);
    setAdding(true);
    try {
      await quotationsApi.addToCart(assetId, qty, start, due);
      setAdded(true);
    } catch (err) {
      setError(errMsg(err, "Couldn't add that to your quote."));
    } finally {
      setAdding(false);
    }
  };

  return (
    <div className="border border-border-soft rounded-[3px] p-3 mt-3">
      <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5 mb-2.5">
        <ShoppingCart size={11} />Add to quote
      </p>
      <div className="grid grid-cols-3 gap-2">
        <div>
          <label className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-text-faint">Qty</label>
          <input
            type="number"
            min={1}
            value={quantity}
            onChange={(e) => { setQuantity(e.target.value); clearFeedback(); }}
            className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none"
          />
        </div>
        <div>
          <label className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-text-faint">Start</label>
          <input
            type="date"
            min={today()}
            value={start}
            onChange={(e) => { setStart(e.target.value); clearFeedback(); }}
            className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none"
          />
        </div>
        <div>
          <label className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-text-faint">Due</label>
          <input
            type="date"
            min={today()}
            value={due}
            onChange={(e) => { setDue(e.target.value); clearFeedback(); }}
            className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none"
          />
        </div>
      </div>
      <button
        onClick={submit}
        disabled={adding}
        className="w-full mt-2.5 flex items-center justify-center gap-1.5 bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink text-[12.5px] font-semibold rounded-[3px] py-2 transition-colors"
      >
        {adding ? <Loader2 size={12} className="animate-spin" /> : <ShoppingCart size={12} />}
        {adding ? "Adding…" : "Add to quote"}
      </button>
      {error && <p className="text-[11px] text-rust-soft mt-1.5">{error}</p>}
      {added && !error && (
        <p className="text-[11px] text-moss-soft mt-1.5">
          Added{price != null ? ` at ${formatPrice(price)}/day` : ""} — <Link to="/quotations" className="underline hover:text-moss">view your quote</Link>
        </p>
      )}
    </div>
  );
}

export function AssetDrawer({
  asset,
  onClose,
  canManage,
  canDispatch,
  showStock,
  onDispatch,
  onChanged,
}: {
  asset: { id: number; name: string } | null;
  onClose: () => void;
  /** Super Admin: inline edit, log exception, delete pool. */
  canManage: boolean;
  /** Super Admin/Admin/Manager: Issue / Dispatch. Also used to gate "Active
   * deployments" below -- that list names who currently has custody of
   * every unit (assignee_name/type across the whole org), which is
   * custody/checkout data, not asset-catalog data, so it belongs behind
   * the same privileged gate as the rest of the app's org-wide checkout
   * views (Checkouts.tsx, Dashboard.tsx's "Needs attention"), not shown to
   * a Staff/Customer just because they can see the pool itself. */
  canDispatch: boolean;
  /** From useAuth()'s canSeeStock (see lib/roles.ts). Manager/Admin/Super
   * Admin/demo always true; Staff/Customer only when
   * CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER is on. Hides the read-only
   * "Available/Total" card in the non-manage view below. */
  showStock: boolean;
  onDispatch?: (asset: { id: number; name: string; available_quantity: number }) => void;
  /** Called after any mutation here that should refresh the asset list behind this drawer. */
  onChanged: () => void;
}) {
  const [details, setDetails] = useState<AssetDetails | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const beginRequest = useRequestGuard();

  const refresh = () => {
    if (!asset) return;
    const isCurrent = beginRequest();
    setLoading(true);
    setError(null);
    assetsApi.details(asset.id)
      .then((d) => { if (isCurrent()) setDetails(d); })
      .catch((err) => { if (isCurrent()) setError(errMsg(err, "Couldn't load pool details.")); })
      .finally(() => { if (isCurrent()) setLoading(false); });
  };

  useEffect(() => {
    setDetails(null);
    if (asset) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asset?.id]);

  const afterMutation = () => {
    refresh();
    onChanged();
  };

  if (!asset) return null;

  const deletePool = async () => {
    if (!details) return;
    if (!confirm(`Delete asset pool "${details.name}"? It will be removed from active inventory, but can be restored later. Only pools with no outstanding checkouts or isolated units can be deleted.`)) return;
    setDeleting(true);
    setError(null);
    try {
      await assetsApi.remove(asset.id);
      onChanged();
      onClose();
    } catch (err) {
      setError(errMsg(err, "Couldn't delete this pool."));
    } finally {
      setDeleting(false);
    }
  };

  const isolatedItems = details ? [...details.under_repair_items, ...details.stolen_items] : [];

  return (
    <AnimatePresence>
      {asset && (
        <>
          <motion.div
            key="backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
            className="fixed inset-0 bg-ink/70 backdrop-blur-sm z-40"
          />
          <motion.div
            key="panel"
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ type: "spring", stiffness: 320, damping: 34 }}
            // MOBILE FIX: this was `h-screen` (100vh) -- on a mobile browser
            // whose address bar shows/hides as the page scrolls (notably
            // Chrome/Firefox for Android and iOS Safari), 100vh is measured
            // against the LARGEST possible viewport, so a `fixed`-positioned
            // panel sized off it renders taller than what's actually visible
            // once that chrome is on screen. The panel's own overflow-y-auto
            // never gets a chance to reveal the extra bottom content (here,
            // the "Isolated units" section and its "Log isolated unit" form)
            // because the panel's bottom edge is already off past the real
            // viewport -- reads as "can't scroll to the end" even though
            // scrolling itself works fine. `top-0 bottom-0` instead anchors
            // both edges to the fixed containing block, which every major
            // mobile browser sizes against the CURRENT visual viewport, not
            // the largest one -- same fix already applied to Layout.tsx's
            // sidebar, and the pattern CustodyDrawer.tsx/QuoteDetailDrawer.tsx
            // already used correctly (this was the one drawer that still had
            // the bug).
            className="fixed right-0 top-0 bottom-0 z-50 w-full max-w-md bg-surface border-l border-border-soft overflow-y-auto"
          >
            {/* Bottom safe-area padding so the last control (the "Log
                isolated unit" button) never sits flush against a mobile
                browser's own bottom toolbar -- same env(safe-area-inset-*)
                pattern Layout.tsx's sidebar already uses. */}
            <div className="p-6" style={{ paddingBottom: "max(1.5rem, env(safe-area-inset-bottom))" }}>
              <div className="flex items-start justify-between">
                <div>
                  <p className="font-mono text-[11px] tracking-widest text-brass-soft">POOL-{String(asset.id).padStart(4, "0")}</p>
                  <h2 className="font-display text-xl font-semibold text-text mt-1">{details?.name ?? asset.name}</h2>
                </div>
                <button onClick={onClose} className="p-1.5 rounded-full hover:bg-surface-raised text-text-muted hover:text-text transition-colors">
                  <X size={16} />
                </button>
              </div>

              {loading && <p className="text-[12px] text-text-faint text-center py-10">Loading…</p>}
              {error && <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5 mt-4">{error}</div>}

              {details && (
                <>
                  {canManage ? (
                    <>
                      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-6">
                        <PriceField assetId={asset.id} price={details.price} onSaved={afterMutation} />
                        <CategoryField assetId={asset.id} category={details.category} onSaved={afterMutation} />
                        <DepartmentField assetId={asset.id} department={details.department} onSaved={afterMutation} />
                      </div>
                      <div className="mt-3">
                        <NameField assetId={asset.id} name={details.name} onSaved={afterMutation} />
                      </div>
                      <CapacityField assetId={asset.id} total={details.total_quantity} available={details.available_quantity} onSaved={afterMutation} />
                    </>
                  ) : (
                    <div className="grid grid-cols-2 gap-3 mt-6">
                      {showStock && (
                        <div className="border border-border-soft rounded-[3px] p-3">
                          <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5"><Layers size={11} />Available</p>
                          <p className="font-mono text-xl text-text mt-1">{details.available_quantity}<span className="text-text-faint text-sm">/{details.total_quantity}</span></p>
                        </div>
                      )}
                      {details.price != null && (
                        <div className="border border-border-soft rounded-[3px] p-3">
                          <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5"><DollarSign size={11} />Unit price</p>
                          <p className="font-mono text-xl text-text mt-1">{formatPrice(details.price)}</p>
                        </div>
                      )}
                      {details.category && (
                        <div className="border border-border-soft rounded-[3px] p-3">
                          <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5"><Tag size={11} />Category</p>
                          <p className="text-sm text-text mt-1">{details.category}</p>
                        </div>
                      )}
                      {details.department && (
                        <div className="border border-border-soft rounded-[3px] p-3">
                          <p className="text-[10px] uppercase tracking-wider text-text-faint flex items-center gap-1.5"><Tag size={11} />Department</p>
                          <p className="text-sm text-text mt-1">{details.department}</p>
                        </div>
                      )}
                    </div>
                  )}

                  <AddToQuoteField assetId={asset.id} price={details.price} />

                  <div className="mt-6 flex gap-2">
                    {canDispatch && (
                      <button
                        onClick={() => onDispatch?.({ id: asset.id, name: details.name, available_quantity: details.available_quantity })}
                        className="flex-1 bg-brass hover:bg-brass-soft text-ink font-medium text-[13px] rounded-[3px] py-2.5 transition-colors"
                      >
                        Check out
                      </button>
                    )}
                    {canManage && (
                      <button
                        data-otel-action="asset.delete"
                        onClick={deletePool}
                        disabled={deleting}
                        className="flex-1 flex items-center justify-center gap-1.5 border border-rust/40 hover:bg-rust/10 disabled:opacity-60 text-rust-soft text-[13px] rounded-[3px] py-2.5 transition-colors"
                      >
                        {deleting ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                        {deleting ? "Deleting…" : "Delete pool"}
                      </button>
                    )}
                  </div>

                  {canDispatch && (
                    <div className="mt-8">
                      <p className="text-[11px] uppercase tracking-wider text-text-faint mb-3">Active deployments ({details.active_assignments.length})</p>
                      <div className="flex flex-col gap-2.5">
                        {details.active_assignments.length === 0 && <p className="text-[12px] text-text-faint">No active deployments for this pool.</p>}
                        {details.active_assignments.map((a) => (
                          <AssignmentRow key={a.checkout_id} item={a} onReturned={afterMutation} />
                        ))}
                      </div>
                    </div>
                  )}
                  {canManage && (
                    <div className="mt-8">
                      <p className="text-[11px] uppercase tracking-wider text-text-faint mb-3 flex items-center gap-1.5">
                        <ShieldAlert size={11} />Isolated units ({isolatedItems.length})
                      </p>
                      <div className="flex flex-col gap-2.5">
                        {isolatedItems.length === 0 && <p className="text-[12px] text-text-faint">No isolated units for this pool.</p>}
                        {isolatedItems.map((item) => (
                          <IsolatedRow key={item.exception_id} assetId={asset.id} item={item} onRecalled={afterMutation} />
                        ))}
                      </div>
                      <div className="mt-3">
                        <ExceptionForm assetId={asset.id} onLogged={afterMutation} />
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
