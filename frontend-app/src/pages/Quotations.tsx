import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { ShoppingCart, Trash2, Loader2, Search, Send, History, UserPlus } from "lucide-react";
import { quotationsApi, usersApi, formatPrice, formatDate, setCurrencyCode, ApiError } from "../lib/api";
import type { CatalogAsset, QuotationCartOrDetail, QuotationListRow, UserRow } from "../lib/types";
import { QuoteDetailDrawer } from "../components/QuoteDetailDrawer";
import { StatusPill } from "../components/StatusPill";
import { ExportButtons } from "../components/ExportButtons";
import { PaginationBar, RowsPerPageSelect } from "../components/PaginationBar";
import { DEFAULT_PAGE_SIZE, MOBILE_DEFAULT_PAGE_SIZE, MOBILE_PAGE_SIZE_OPTIONS, PAGE_SIZE_OPTIONS } from "../lib/pagination";
import { useAuth } from "../lib/useAuth";
import { isPrivileged } from "../lib/roles";
import { useIsMobile } from "../lib/useIsMobile";

function errMsg(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

const today = () => new Date().toISOString().slice(0, 10);

function statusPillFor(status: string) {
  if (status === "approved") return <StatusPill status="active" />;
  if (status === "fulfilled") return <StatusPill status="returned" />;
  return <StatusPill status="pending" />;
}

/** One catalog row's Qty/Start/Due draft, keyed by asset id -- mirrors the
 * legacy frontend's per-row `qcat-qty-*`/`qcat-start-*`/`qcat-due-*`
 * inputs (js/components/quotation.js), just kept as component state
 * instead of raw DOM lookups. */
interface RowDraft { quantity: string; start: string; due: string }

export function Quotations() {
  const { user, demo } = useAuth();
  const canAssign = demo || isPrivileged(user?.role);

  // Below the `lg` breakpoint this page's catalog rows render as tall
  // stacked cards instead of table rows (see the layout below), so a
  // smaller default page size keeps the initial scroll reasonable.
  const isMobile = useIsMobile();

  // ---- Asset Catalog table: true server-side pagination + search, same
  // {items,total,limit,offset} contract every other directory table in
  // the app uses (Assets.tsx/Admin.tsx's User/Ad-Hoc/Audit tables) --
  // see lib/pagination.ts's docstring, which already listed "Quotations"
  // among the pages meant to work this way. ----
  const [catalog, setCatalog] = useState<CatalogAsset[]>([]);
  const [catalogTotal, setCatalogTotal] = useState(0);
  const [catalogOffset, setCatalogOffset] = useState(0);
  const [catalogPerPage, setCatalogPerPage] = useState(isMobile ? MOBILE_DEFAULT_PAGE_SIZE : DEFAULT_PAGE_SIZE);
  const [catalogLoading, setCatalogLoading] = useState(true);

  const [cart, setCart] = useState<QuotationCartOrDetail | null>(null);
  const [history, setHistory] = useState<QuotationListRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [drafts, setDrafts] = useState<Record<number, RowDraft>>({});
  const [addingId, setAddingId] = useState<number | null>(null);
  const [cartBusyId, setCartBusyId] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [tab, setTab] = useState<"order" | "history">("order");
  const [openQuoteId, setOpenQuoteId] = useState<number | null>(null);

  // Full, unpaginated catalog -- kept separate from the paginated `catalog`
  // state above purely to feed QuoteDetailDrawer's "Add another asset"
  // typeahead, which searches an in-memory list rather than hitting the
  // server per keystroke (see quotationsApi.catalog()'s own docstring).
  const [fullCatalog, setFullCatalog] = useState<CatalogAsset[]>([]);

  // ---- Manager/Admin-only: assign the collated "My Order" cart to a
  // user, without waiting for it to be Submitted first -- lets a
  // Manager/Admin build an order on someone's behalf straight from the
  // Inventory page's "Add to quote" action, then hand it off. Mirrors
  // QuoteDetailDrawer's "assign to a linked user" search-select (backend
  // assign_quotation() already permits this on any non-fulfilled
  // Quotation, draft included -- see services/quotation_service.py's
  // _ensure_admin_editable()). ----
  const [assignOpen, setAssignOpen] = useState(false);
  const [assignQuery, setAssignQuery] = useState("");
  const [assignMatches, setAssignMatches] = useState<UserRow[]>([]);
  const [assigning, setAssigning] = useState(false);
  const [assignMessage, setAssignMessage] = useState<string | null>(null);

  const refreshCart = () => quotationsApi.myCart().then(setCart).catch((err) => setError(errMsg(err, "Couldn't refresh your cart.")));
  const refreshHistory = () => quotationsApi.myHistory().then(setHistory).catch((err) => setError(errMsg(err, "Couldn't refresh your order history.")));

  useEffect(() => {
    let cancelled = false;
    Promise.all([quotationsApi.publicConfig(), quotationsApi.catalog(), quotationsApi.myCart(), quotationsApi.myHistory()])
      .then(([config, fullCat, myCart, myHistory]) => {
        if (cancelled) return;
        setCurrencyCode(config.currency_code);
        setFullCatalog(fullCat);
        setCart(myCart);
        setHistory(myHistory);
      })
      .catch((err) => !cancelled && setError(errMsg(err, "Couldn't load the Quotation Catalog.")))
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, []);

  const refreshCatalog = () => {
    setCatalogLoading(true);
    quotationsApi.catalogPage(catalogPerPage, catalogOffset, search).then((res) => {
      setCatalog(res.items);
      setCatalogTotal(res.total);
      setCatalogLoading(false);
    }).catch((err) => {
      setError(errMsg(err, "Couldn't load the Asset Catalog."));
      setCatalogLoading(false);
    });
  };

  useEffect(refreshCatalog, [catalogOffset, catalogPerPage, search]);

  // Always jumps back to the first page on a page-size or search change
  // (mirrors Assets.tsx's handlePerPageChange()/search onChange).
  const handleCatalogPerPageChange = (n: number) => {
    setCatalogPerPage(n);
    setCatalogOffset(0);
  };
  const handleSearchChange = (value: string) => {
    setCatalogOffset(0);
    setSearch(value);
  };

  // Search-as-you-type for the assign panel's user picker -- same
  // debounce-free pattern as QuoteDetailDrawer's own "assign to a linked
  // user" search.
  useEffect(() => {
    if (!assignOpen || !assignQuery.trim()) { setAssignMatches([]); return; }
    let cancelled = false;
    usersApi.list(8, 0, assignQuery.trim()).then((page) => !cancelled && setAssignMatches(page.items)).catch(() => {});
    return () => { cancelled = true; };
  }, [assignOpen, assignQuery]);

  const assignCartToUser = async (target: UserRow) => {
    if (!cart?.id) return;
    setAssigning(true);
    setAssignMessage(null);
    try {
      await quotationsApi.assign(cart.id, { assignee_type: "user", user_id: target.id });
      await refreshCart();
      setAssignOpen(false);
      setAssignQuery("");
      setAssignMatches([]);
      setAssignMessage(`Assigned to ${target.name}.`);
    } catch (err) {
      setError(errMsg(err, "Couldn't assign this quote."));
    } finally {
      setAssigning(false);
    }
  };

  const draftFor = (id: number): RowDraft => drafts[id] ?? { quantity: "1", start: today(), due: today() };
  const setDraft = (id: number, patch: Partial<RowDraft>) =>
    setDrafts((prev) => ({ ...prev, [id]: { ...draftFor(id), ...patch } }));

  const addToOrder = async (asset: CatalogAsset) => {
    const draft = draftFor(asset.id);
    const quantity = parseInt(draft.quantity, 10);
    if (!quantity || quantity < 1) { setError("Enter a quantity of at least 1."); return; }
    if (!draft.start || !draft.due) { setError("Pick both a start and a due date."); return; }
    if (draft.due < draft.start) { setError("Due date cannot be before the start date."); return; }
    setError(null);
    setAddingId(asset.id);
    try {
      const updated = await quotationsApi.addToCart(asset.id, quantity, draft.start, draft.due);
      setCart(updated);
    } catch (err) {
      setError(errMsg(err, "Couldn't add that to your order."));
    } finally {
      setAddingId(null);
    }
  };

  const updateQty = async (itemId: number, quantity: number) => {
    if (quantity < 1) return;
    setCartBusyId(itemId);
    try {
      setCart(await quotationsApi.updateCartItem(itemId, quantity));
    } catch (err) {
      setError(errMsg(err, "Couldn't update that line."));
      refreshCart();
    } finally {
      setCartBusyId(null);
    }
  };

  const removeItem = async (itemId: number) => {
    setCartBusyId(itemId);
    try {
      setCart(await quotationsApi.removeCartItem(itemId));
    } catch (err) {
      setError(errMsg(err, "Couldn't remove that line."));
    } finally {
      setCartBusyId(null);
    }
  };

  const submitOrder = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await quotationsApi.submitCart();
      await Promise.all([refreshCart(), refreshHistory()]);
      setTab("history");
    } catch (err) {
      setError(errMsg(err, "Couldn't submit your order."));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div>
      <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4 }} className="mb-6">
        <h1 className="font-display text-2xl font-semibold text-text">Quotations</h1>
        <p className="text-text-muted text-sm mt-1">Browse the catalog, build an order, and submit it for approval.</p>
      </motion.div>

      {error && (
        <div className="bg-rust/10 border border-rust/30 text-rust-soft text-[12px] rounded-[3px] px-3 py-2.5 mb-4 flex items-center justify-between">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="text-rust-soft/70 hover:text-rust-soft">×</button>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* On mobile (single column) this comes second in the DOM but is
            pushed above the catalog via `order-2`, so "My Order"/"My
            Quotes" is visible without scrolling past the whole catalog
            first. At `lg` and up it's back in its normal second-column
            spot (`lg:order-2`), unchanged from before. */}
        <div className="order-2 lg:order-1 lg:col-span-2">
          <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
            <h2 className="font-display text-[15px] font-medium text-text">Asset Catalog</h2>
            <div className="flex items-center gap-2 flex-wrap">
              <div className="relative w-56 max-w-full">
                <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
                <input
                  value={search}
                  onChange={(e) => handleSearchChange(e.target.value)}
                  placeholder="Search catalog…"
                  className="w-full bg-surface border border-border-soft rounded-[3px] pl-8 pr-3 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none transition-colors"
                />
              </div>
              <RowsPerPageSelect
                value={catalogPerPage}
                onChange={handleCatalogPerPageChange}
                options={isMobile ? MOBILE_PAGE_SIZE_OPTIONS : PAGE_SIZE_OPTIONS}
              />
            </div>
          </div>

          <div className="border border-border-soft rounded-[3px] bg-surface overflow-hidden mb-3">
            {catalogLoading ? (
              <p className="text-center text-text-faint text-[12px] py-10">Loading catalog…</p>
            ) : catalog.length === 0 ? (
              <p className="text-center text-text-faint text-[12px] py-10">No assets found.</p>
            ) : (
              <div className="divide-y divide-border-soft">
                {catalog.map((a) => {
                  const draft = draftFor(a.id);
                  return (
                    <div key={a.id} className="p-4 hover:bg-surface-raised transition-colors" data-testid="catalog-row">
                      <div className="flex items-start justify-between gap-3 mb-2.5">
                        <div className="min-w-0">
                          <p className="text-[13px] text-text font-medium truncate">{a.name}</p>
                          <p className="text-[11px] text-text-faint">{a.category ?? "—"}</p>
                        </div>
                        <p className="shrink-0 font-mono text-[13px] text-text">
                          {formatPrice(a.price)}
                          {a.price != null && <span className="text-text-faint">/day</span>}
                        </p>
                      </div>
                      <div className="grid grid-cols-3 gap-2 mb-2.5">
                        <div>
                          <label className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-text-faint">Qty</label>
                          <input
                            type="number"
                            min={1}
                            value={draft.quantity}
                            onChange={(e) => setDraft(a.id, { quantity: e.target.value })}
                            className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none"
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-text-faint">Start</label>
                          <input
                            type="date"
                            min={today()}
                            value={draft.start}
                            onChange={(e) => setDraft(a.id, { start: e.target.value })}
                            className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none"
                          />
                        </div>
                        <div>
                          <label className="mb-1 block text-[10px] font-medium uppercase tracking-wide text-text-faint">Due</label>
                          <input
                            type="date"
                            min={today()}
                            value={draft.due}
                            onChange={(e) => setDraft(a.id, { due: e.target.value })}
                            className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1.5 text-[12px] text-text focus:border-brass/50 focus:outline-none"
                          />
                        </div>
                      </div>
                      <button
                        onClick={() => addToOrder(a)}
                        disabled={addingId === a.id}
                        className="w-full flex items-center justify-center gap-1.5 bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink text-[12px] font-semibold rounded-[3px] py-1.5 transition-colors"
                      >
                        {addingId === a.id ? <Loader2 size={12} className="animate-spin" /> : <ShoppingCart size={12} />}
                        {addingId === a.id ? "Adding…" : "Add"}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div className="mb-6">
            <PaginationBar total={catalogTotal} perPage={catalogPerPage} offset={catalogOffset} onOffsetChange={setCatalogOffset} />
          </div>
        </div>

        <div className="order-1 lg:order-2">
          <div className="flex items-center gap-1 mb-3 border-b border-border-soft">
            <button
              onClick={() => setTab("order")}
              className={`relative px-3 py-2 text-[12.5px] font-medium transition-colors ${tab === "order" ? "text-text" : "text-text-muted hover:text-text"}`}
            >
              My Order {cart && cart.items.length > 0 && <span className="ml-1 font-mono text-[10px] text-text-faint">({cart.items.length})</span>}
              {tab === "order" && <span className="absolute left-0 right-0 -bottom-px h-[2px] bg-brass" />}
            </button>
            <button
              onClick={() => setTab("history")}
              className={`relative flex items-center gap-1 px-3 py-2 text-[12.5px] font-medium transition-colors ${tab === "history" ? "text-text" : "text-text-muted hover:text-text"}`}
            >
              <History size={12} /> My Quotes
              {tab === "history" && <span className="absolute left-0 right-0 -bottom-px h-[2px] bg-brass" />}
            </button>
          </div>

          {tab === "order" && (
            <div className="border border-border-soft rounded-[3px] bg-surface p-4">
              {loading ? (
                <p className="text-center text-text-faint text-[12px] py-8">Loading…</p>
              ) : !cart || cart.items.length === 0 ? (
                <p className="text-center text-text-faint text-[12px] py-8">Your saved order is empty — add assets from the catalog.</p>
              ) : (
                <>
                  <div className="flex flex-col gap-2.5 mb-4">
                    {cart.items.map((li) => (
                      <div key={li.item_id} className="border border-border-soft rounded-[3px] p-3" data-testid="cart-row">
                        <div className="flex items-start justify-between gap-2 mb-2">
                          <div className="min-w-0">
                            <p className="text-[12.5px] text-text font-medium truncate">{li.asset_name}</p>
                            <p className="text-[11px] text-text-faint">{li.start_date} → {li.due_date} ({li.days}d)</p>
                          </div>
                          <button
                            onClick={() => li.item_id != null && removeItem(li.item_id)}
                            disabled={cartBusyId === li.item_id}
                            title="Remove from order"
                            className="shrink-0 rounded-[3px] border border-border-soft p-1.5 text-text-faint hover:border-rust/40 hover:text-rust-soft transition-colors disabled:opacity-50"
                          >
                            {cartBusyId === li.item_id ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
                          </button>
                        </div>
                        <div className="flex items-center justify-between gap-3">
                          <input
                            type="number"
                            min={1}
                            value={li.quantity}
                            onChange={(e) => li.item_id != null && updateQty(li.item_id, parseInt(e.target.value, 10) || 1)}
                            disabled={cartBusyId === li.item_id}
                            className="w-16 rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1 text-[12px] text-text focus:border-brass/50 focus:outline-none"
                          />
                          <p className="font-mono text-[12.5px] text-text">{formatPrice(li.line_total)}</p>
                        </div>
                      </div>
                    ))}
                  </div>

                  <div className="border-t border-border-soft pt-3 mb-4 flex flex-col gap-1.5 text-[12.5px]">
                    <div className="flex justify-between text-text-muted"><span>Subtotal</span><span className="font-mono">{formatPrice(cart.subtotal)}</span></div>
                    <div className="flex justify-between text-text-muted"><span>VAT ({cart.vat_percent}%)</span><span className="font-mono">{formatPrice(cart.vat_amount)}</span></div>
                    <div className="flex justify-between text-text font-medium text-[14px] pt-1"><span>Total</span><span className="font-mono">{formatPrice(cart.total)}</span></div>
                  </div>

                  {canAssign && (
                    <div className="border border-border-soft rounded-[3px] p-3 mb-4">
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-[11.5px] text-text-muted">
                          {assignMessage ?? "Assign this quote to a user before or after submitting."}
                        </p>
                        <button
                          onClick={() => { setAssignOpen((v) => !v); setAssignMessage(null); }}
                          className="flex items-center gap-1 shrink-0 text-[11.5px] font-medium text-brass-soft hover:underline"
                        >
                          <UserPlus size={11} /> {assignOpen ? "Cancel" : "Assign Quote"}
                        </button>
                      </div>
                      {assignOpen && (
                        <div className="relative mt-2.5">
                          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-faint" />
                          <input
                            value={assignQuery}
                            onChange={(e) => setAssignQuery(e.target.value)}
                            placeholder="Search staff or customers by name/email…"
                            className="w-full bg-ink-soft border border-border-soft rounded-[3px] pl-8 pr-3 py-1.5 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none"
                          />
                          {assignMatches.length > 0 && (
                            <div className="border border-border-soft rounded-[3px] mt-2 overflow-hidden">
                              {assignMatches.map((u) => (
                                <button
                                  key={u.id}
                                  type="button"
                                  onClick={() => assignCartToUser(u)}
                                  disabled={assigning}
                                  className="block w-full px-3 py-2 text-left text-[12px] hover:bg-ink-soft transition-colors disabled:opacity-50"
                                >
                                  <span className="text-text font-medium">{u.name}</span>
                                  <span className="text-text-faint"> · {u.email} · {u.role}</span>
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}

                  <div className="flex items-center gap-2">
                    <button
                      onClick={submitOrder}
                      disabled={submitting || cart.items.length === 0}
                      className="flex-1 flex items-center justify-center gap-1.5 bg-brass hover:bg-brass-soft disabled:opacity-60 text-ink text-[12.5px] font-semibold rounded-[3px] py-2.5 transition-colors"
                    >
                      {submitting ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
                      {submitting ? "Submitting…" : "Submit Quotation"}
                    </button>
                    <ExportButtons
                      compact
                      formats={["pdf"]}
                      urlFor={() => quotationsApi.exportCartUrl()}
                      filenameFor={() => "equipment_quotation.pdf"}
                    />
                  </div>
                </>
              )}
            </div>
          )}

          {tab === "history" && (
            <div className="border border-border-soft rounded-[3px] bg-surface overflow-hidden">
              {history.length === 0 ? (
                <p className="text-center text-text-faint text-[12px] py-8 px-4">
                  You haven't submitted any quotes yet — build an order on the "My Order" tab, then Submit Quotation.
                </p>
              ) : (
                <div className="divide-y divide-border-soft">
                  {history.map((q) => (
                    <button
                      key={q.id}
                      onClick={() => setOpenQuoteId(q.id)}
                      className="w-full text-left p-3.5 hover:bg-surface-raised transition-colors"
                      data-testid="history-row"
                    >
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <span className="font-mono text-[12.5px] text-text font-medium">{q.reference_number}</span>
                        {statusPillFor(q.status)}
                      </div>
                      <div className="flex items-center justify-between text-[11.5px] text-text-faint">
                        <span>{formatDate(q.submitted_at)} · {q.item_count} item(s)</span>
                        <span className="font-mono text-text-muted">{formatPrice(q.total)}</span>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <QuoteDetailDrawer
        mode="self"
        quotationId={openQuoteId}
        catalog={fullCatalog}
        onClose={() => setOpenQuoteId(null)}
        onChanged={refreshHistory}
      />
    </div>
  );
}
