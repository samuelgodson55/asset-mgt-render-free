// =============================================================================
// Quotes -- Admin/Manager view of the self-service Quotation feature,
// ported from the legacy frontend's js/components/quotation.js
// (initQuotesTab()/loadQuotes()/renderQuotesTable()). Same server-side
// search + pagination pattern as UsersPanel/OutsidersPanel. require_privileged_role
// on the backend (Super Admin/Admin/Manager) -- same `canDirectory` gate
// AdminOrManagerPage uses for User/Ad-Hoc Directory.
// =============================================================================
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Loader2, CheckCircle, PackageCheck, X } from "lucide-react";
import { quotationsApi, formatPrice, formatDate } from "../../lib/api";
import type { QuotationListRow, CatalogAsset, FulfillmentQueueRow, QuotationLineItem, QuotationOutsourceShortfallItem } from "../../lib/types";
import { QuoteDetailDrawer } from "../../components/QuoteDetailDrawer";
import { PaginationBar, RowsPerPageSelect } from "../../components/PaginationBar";
import { DEFAULT_PAGE_SIZE } from "../../lib/pagination";
import { useRequestGuard } from "../../lib/useRequestGuard";
import { ErrorBanner } from "../../components/ui/ErrorBanner";
import { SearchInput } from "../../components/ui/SearchInput";
import { TableShell, TableHead, TablePlaceholderRow } from "../../components/ui/TableShell";
import { errMsg } from "./sharedHelpers";

function statusBadgeClasses(status: string): string {
  if (status === "approved") return "bg-moss/15 text-moss-soft";
  if (status === "fulfilled") return "bg-sky/15 text-sky";
  if (status === "paid") return "bg-moss/15 text-moss-soft";
  if (status === "submitted") return "bg-brass/15 text-brass-soft";
  return "bg-surface-raised text-text-faint";
}

function statusLabel(status: string): string {
  if (status === "submitted") return "Pending Review";
  if (status === "approved") return "Approved";
  if (status === "fulfilled") return "Fulfilled";
  if (status === "paid") return "Paid";
  return status;
}

// One allocation row inside a shortfall line's "split across another
// outsourcing company" list -- mirrors the legacy frontend's
// shortfallAllocationRowHtml()/#shortfallRowsContainer-* (js/components/
// quotation.js). Quantity is kept as the source of truth for how much of
// the line's shortfall this row covers; sourced_from/unit_price are both
// optional (blank unit_price falls back to the depleted AssetType's own
// catalog price server-side).
interface ShortfallAllocRow { quantity: string; sourcedFrom: string; unitPrice: string }
interface ShortfallItemState { enabled: boolean; rows: ShortfallAllocRow[] }

function shortfallQtyFor(li: QuotationLineItem): number {
  return li.shortfall_quantity != null ? li.shortfall_quantity : li.quantity - (li.available_quantity ?? 0);
}

function FulfillmentPanel({ onCheckedOut }: { onCheckedOut: () => void }) {
  const [rows, setRows] = useState<FulfillmentQueueRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [checkingOutId, setCheckingOutId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Keyed by `${quoteId}:${itemId}` so state naturally clears itself once
  // refresh() pulls a fresh queue (a checked-out/no-longer-approved quote
  // just stops appearing, no separate cleanup needed).
  const [shortfall, setShortfall] = useState<Record<string, ShortfallItemState>>({});
  const beginRequest = useRequestGuard();

  const refresh = () => {
    const isCurrent = beginRequest();
    setLoading(true);
    quotationsApi.fulfillmentQueue().then((items) => {
      if (!isCurrent()) return;
      setError(null);
      setRows(items);
      setShortfall({});
      setLoading(false);
    }).catch((err) => {
      if (!isCurrent()) return;
      setError(errMsg(err, "Couldn't load the fulfillment queue."));
      setLoading(false);
    });
  };

  useEffect(refresh, []);

  const keyFor = (quoteId: number, itemId: number) => `${quoteId}:${itemId}`;

  const toggleShortfall = (quoteId: number, li: QuotationLineItem) => {
    if (li.item_id == null) return;
    const key = keyFor(quoteId, li.item_id);
    setShortfall((prev) => {
      const existing = prev[key];
      if (existing) return { ...prev, [key]: { ...existing, enabled: !existing.enabled } };
      // First time this line's checked -- pre-fill the single starting row
      // with the FULL shortfall quantity, so "just outsource it all to one
      // company" needs zero extra clicks.
      const qty = shortfallQtyFor(li);
      return { ...prev, [key]: { enabled: true, rows: [{ quantity: String(qty), sourcedFrom: "", unitPrice: "" }] } };
    });
  };

  const addAllocRow = (quoteId: number, li: QuotationLineItem) => {
    if (li.item_id == null) return;
    const key = keyFor(quoteId, li.item_id);
    const shortfallQty = shortfallQtyFor(li);
    setShortfall((prev) => {
      const existing = prev[key];
      if (!existing) return prev;
      // New row defaults to whatever's still unallocated, so splitting a
      // shortfall is "add a row" then adjust one number, not two.
      const allocated = existing.rows.reduce((sum, r) => sum + (parseInt(r.quantity, 10) || 0), 0);
      const remaining = Math.max(shortfallQty - allocated, 0);
      return { ...prev, [key]: { ...existing, rows: [...existing.rows, { quantity: remaining ? String(remaining) : "", sourcedFrom: "", unitPrice: "" }] } };
    });
  };

  const removeAllocRow = (quoteId: number, itemId: number, index: number) => {
    const key = keyFor(quoteId, itemId);
    setShortfall((prev) => {
      const existing = prev[key];
      if (!existing || existing.rows.length <= 1) return prev; // a lone row can't be removed
      return { ...prev, [key]: { ...existing, rows: existing.rows.filter((_, i) => i !== index) } };
    });
  };

  const updateAllocRow = (quoteId: number, itemId: number, index: number, patch: Partial<ShortfallAllocRow>) => {
    const key = keyFor(quoteId, itemId);
    setShortfall((prev) => {
      const existing = prev[key];
      if (!existing) return prev;
      const rows = existing.rows.map((r, i) => (i === index ? { ...r, ...patch } : r));
      return { ...prev, [key]: { ...existing, rows } };
    });
  };

  const checkout = async (row: FulfillmentQueueRow) => {
    setCheckingOutId(row.id);
    setError(null);
    try {
      const outsourceShortfallItems: QuotationOutsourceShortfallItem[] = row.items
        .filter((li) => li.stock_shortfall && !li.is_outsourced && li.item_id != null)
        .map((li) => ({ itemId: li.item_id as number, state: shortfall[keyFor(row.id, li.item_id as number)] }))
        .filter((x) => x.state?.enabled)
        .map(({ itemId, state }) => ({
          quotation_item_id: itemId,
          allocations: state!.rows
            .filter((r) => (parseInt(r.quantity, 10) || 0) > 0)
            .map((r) => ({
              quantity: parseInt(r.quantity, 10),
              sourced_from: r.sourcedFrom.trim() || null,
              unit_price: r.unitPrice.trim() === "" ? null : Number(r.unitPrice),
            })),
        }))
        .filter((item) => item.allocations.length > 0);

      await quotationsApi.checkout(row.id, outsourceShortfallItems);
      refresh();
      onCheckedOut();
    } catch (err) {
      setError(errMsg(err, `Couldn't check out ${row.reference_number}.`));
    } finally {
      setCheckingOutId(null);
    }
  };

  return (
    <div className="border border-border-soft bg-surface rounded-[3px] p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-display text-[14px] font-medium text-text flex items-center gap-1.5"><PackageCheck size={14} /> Fulfillment queue</h3>
        <span className="text-[11px] text-text-faint">{rows.length} approved</span>
      </div>
      {error && <div className="mb-3"><ErrorBanner>{error}</ErrorBanner></div>}
      {loading && <p className="text-[12px] text-text-faint text-center py-6">Loading…</p>}
      {!loading && rows.length === 0 && <p className="text-[12px] text-text-faint text-center py-6">Nothing is Approved / Ready for Pickup right now.</p>}
      <div className="flex flex-col gap-2">
        {rows.map((q) => {
          // One control block per line that's both inventory-backed (an
          // already-outsourced line never had stock to begin with) AND
          // currently short. Checking its box tells POST /checkout to
          // source ONLY the shortfall externally -- whatever stock IS on
          // hand still checks out of inventory normally -- instead of
          // blocking the whole quote's checkout. Left unchecked, a
          // genuinely short line still blocks checkout exactly like before
          // this feature existed.
          const shortfallLines = q.items.filter((li) => li.stock_shortfall && !li.is_outsourced && li.item_id != null);
          return (
            <div key={q.id} className="border border-border-soft rounded-[3px] p-3">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-mono text-[12.5px] text-text font-medium">{q.reference_number}</p>
                  <p className="text-[11px] text-text-faint truncate">
                    For {q.checkout_to ? `${q.checkout_to.name} (${q.checkout_to.email})` : "Unknown"} · {q.item_count} item(s) · approved {formatDate(q.approved_at)}
                  </p>
                  {q.has_shortfall && <p className="text-[11px] text-rust-soft mt-0.5">⚠ Not enough stock on hand for one or more lines below.</p>}
                </div>
                <div className="shrink-0 flex items-center gap-2">
                  <span className="font-mono text-[12.5px] text-text-muted">{formatPrice(q.total)}</span>
                  <button
                    onClick={() => checkout(q)}
                    disabled={checkingOutId === q.id}
                    className="flex items-center gap-1.5 bg-moss/15 hover:bg-moss/25 disabled:opacity-60 text-moss-soft text-[11.5px] font-medium rounded-[3px] px-2.5 py-1.5 transition-colors"
                  >
                    {checkingOutId === q.id ? <Loader2 size={11} className="animate-spin" /> : <CheckCircle size={11} />}
                    {checkingOutId === q.id ? "Checking out…" : "Check out"}
                  </button>
                </div>
              </div>

              {shortfallLines.map((li) => {
                const itemId = li.item_id as number;
                const key = keyFor(q.id, itemId);
                const shortfallQty = shortfallQtyFor(li);
                const state = shortfall[key];
                return (
                  <div key={key} className="mt-2 rounded-[3px] border border-rust/30 bg-rust/5 p-2.5">
                    <p className="text-[11px] font-medium text-rust-soft">
                      ⚠ '{li.asset_name}' needs {li.quantity}, only {li.available_quantity} available — {shortfallQty} short.
                    </p>
                    <label className="mt-1.5 flex cursor-pointer items-center gap-1.5 text-[12px] text-text-muted">
                      <input type="checkbox" checked={!!state?.enabled} onChange={() => toggleShortfall(q.id, li)} className="h-3.5 w-3.5 rounded border-border-soft" />
                      Source the {shortfallQty} short externally so {li.available_quantity} still checks out of stock
                    </label>
                    {state?.enabled && (
                      <div className="mt-1.5">
                        <div className="flex flex-col gap-1.5">
                          {state.rows.map((row, idx) => (
                            <div key={idx} className="grid grid-cols-[3.5rem_1fr_1fr_auto] items-center gap-1.5">
                              <input
                                type="number"
                                min={1}
                                value={row.quantity}
                                onChange={(e) => updateAllocRow(q.id, itemId, idx, { quantity: e.target.value })}
                                placeholder="Qty"
                                className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1 text-[12px] text-text focus:border-brass/50 focus:outline-none"
                              />
                              <input
                                type="text"
                                value={row.sourcedFrom}
                                onChange={(e) => updateAllocRow(q.id, itemId, idx, { sourcedFrom: e.target.value })}
                                placeholder="Sourced from (optional)"
                                className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none"
                              />
                              <input
                                type="number"
                                min={0}
                                step={0.01}
                                value={row.unitPrice}
                                onChange={(e) => updateAllocRow(q.id, itemId, idx, { unitPrice: e.target.value })}
                                placeholder={`Price/day (default ${formatPrice(li.unit_price ?? 0)})`}
                                className="w-full rounded-[3px] border border-border-soft bg-ink-soft px-2 py-1 text-[12px] text-text placeholder:text-text-faint focus:border-brass/50 focus:outline-none"
                              />
                              <button
                                type="button"
                                onClick={() => removeAllocRow(q.id, itemId, idx)}
                                title="Remove this source"
                                className={`rounded p-1 text-text-faint transition-colors hover:text-rust-soft ${state.rows.length <= 1 ? "invisible" : ""}`}
                              >
                                <X size={13} />
                              </button>
                            </div>
                          ))}
                        </div>
                        <button
                          type="button"
                          onClick={() => addAllocRow(q.id, li)}
                          className="mt-1.5 text-[11px] font-medium text-brass-soft hover:underline"
                        >
                          + Split across another outsourcing company
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function QuotesPanel() {
  const [rows, setRows] = useState<QuotationListRow[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [perPage, setPerPage] = useState(DEFAULT_PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [catalog, setCatalog] = useState<CatalogAsset[]>([]);
  const [openQuoteId, setOpenQuoteId] = useState<number | null>(null);
  const beginListRequest = useRequestGuard();
  const beginCatalogRequest = useRequestGuard();
  const [approvingId, setApprovingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fulfillmentTick, setFulfillmentTick] = useState(0);

  // Deep-link support (?openQuote=<id>) -- lets the global header search
  // (see Layout.tsx's submitHeaderSearch()) open this exact quote's
  // Admin/Manager detail drawer straight away once AdminOrManagerPage's
  // own ?tab=quotes deep link has already switched to this panel. Same
  // "read once, then strip" shape as Quotations.tsx's self-service
  // ?quotation= deep link.
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    const raw = searchParams.get("openQuote");
    if (!raw) return;
    const id = Number(raw);
    if (Number.isFinite(id)) setOpenQuoteId(id);
    setSearchParams((prev) => { const next = new URLSearchParams(prev); next.delete("openQuote"); return next; }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refresh = () => {
    const isCurrent = beginListRequest();
    setLoading(true);
    quotationsApi.list(perPage, offset, search).then((res) => {
      if (!isCurrent()) return;
      setError(null);
      setRows(res.items);
      setTotal(res.total);
      setLoading(false);
    }).catch((err) => {
      if (!isCurrent()) return;
      setError(errMsg(err, "Couldn't load quotations."));
      setLoading(false);
    });
  };

  useEffect(refresh, [offset, perPage, search]);
  useEffect(() => {
    const isCurrent = beginCatalogRequest();
    quotationsApi.catalog().then((data) => { if (isCurrent()) setCatalog(data); }).catch((err) => { if (isCurrent()) console.error("Failed to load catalog:", err); });
  }, [beginCatalogRequest]);
  useEffect(() => {
    if (offset > 0 && offset >= total) setOffset(Math.max(0, Math.floor(Math.max(total - 1, 0) / perPage) * perPage));
  }, [offset, total, perPage]);

  const handlePerPageChange = (n: number) => {
    setPerPage(n);
    setOffset(0);
  };

  const approve = async (q: QuotationListRow) => {
    setApprovingId(q.id);
    setError(null);
    try {
      await quotationsApi.approve(q.id);
      refresh();
      setFulfillmentTick((t) => t + 1);
    } catch (err) {
      setError(errMsg(err, `Couldn't approve ${q.reference_number}.`));
    } finally {
      setApprovingId(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      {error && <ErrorBanner>{error}</ErrorBanner>}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <div className="lg:col-span-2 flex flex-col gap-4">
          <div className="flex items-center gap-2 flex-wrap">
            <SearchInput value={search} onChange={(v) => { setOffset(0); setSearch(v); }} placeholder="Search quotes…" />
            <RowsPerPageSelect value={perPage} onChange={handlePerPageChange} />
          </div>

          <TableShell>
            <table className="w-full text-left text-[12.5px]">
              <TableHead>
                <th className="px-5 py-3 font-medium">Reference</th>
                <th className="hidden sm:table-cell px-5 py-3 font-medium">Requester</th>
                <th className="hidden sm:table-cell px-5 py-3 font-medium">Status</th>
                <th className="px-5 py-3 font-medium text-right">Total</th>
                <th className="px-5 py-3 font-medium text-right">Actions</th>
              </TableHead>
              <tbody className="divide-y divide-border-soft">
                {loading && <TablePlaceholderRow columns={5}>Loading…</TablePlaceholderRow>}
                {!loading && rows.length === 0 && <TablePlaceholderRow columns={5}>No submitted quotes yet.</TablePlaceholderRow>}
                {rows.map((q) => (
                  <tr key={q.id}>
                    <td className="px-5 py-3">
                      <p className="font-mono text-text font-medium">{q.reference_number}</p>
                      <p className="text-[11px] text-text-faint sm:hidden">{formatDate(q.submitted_at)} · {q.item_count} item(s)</p>
                    </td>
                    <td className="hidden sm:table-cell px-5 py-3">
                      <p className="text-text-muted">{q.requester ? q.requester.name : "—"}</p>
                      <p className="text-[11px] text-text-faint">{q.requester?.email ?? ""}</p>
                    </td>
                    <td className="hidden sm:table-cell px-5 py-3">
                      <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ${statusBadgeClasses(q.status)}`}>{statusLabel(q.status)}</span>
                    </td>
                    <td className="px-5 py-3 text-right font-mono text-text-muted">{formatPrice(q.total)}</td>
                    <td className="px-5 py-3 text-right">
                      <div className="flex items-center justify-end gap-1.5 flex-wrap">
                        {q.status === "submitted" && (
                          <button
                            onClick={() => approve(q)}
                            disabled={approvingId === q.id}
                            className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-moss/50 hover:text-moss-soft transition-colors disabled:opacity-50"
                          >
                            Approve
                          </button>
                        )}
                        <button onClick={() => setOpenQuoteId(q.id)} className="rounded-md border border-border-soft px-2 py-1 text-[11px] font-medium text-text-muted hover:border-brass/50 hover:text-brass-soft transition-colors">
                          {q.locked ? "View" : "View / Adjust"}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableShell>

          <PaginationBar total={total} perPage={perPage} offset={offset} onOffsetChange={setOffset} />
        </div>

        <FulfillmentPanel key={fulfillmentTick} onCheckedOut={refresh} />
      </div>

      <QuoteDetailDrawer
        mode="admin"
        quotationId={openQuoteId}
        catalog={catalog}
        onClose={() => setOpenQuoteId(null)}
        onChanged={refresh}
      />
    </div>
  );
}
