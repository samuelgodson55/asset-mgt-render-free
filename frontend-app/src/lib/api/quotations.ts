/**
 * lib/api/quotations.ts
 * --------------------------
 * Quotation feature -- ported from the legacy frontend's
 * js/components/quotation.js. Backed by backend/api/quotations_api.py +
 * backend/services/quotation_service.py; see that module's own docstring
 * for the two-halves shape (self-service cart/history vs Admin/Manager
 * Quotes tab) this mirrors.
 */

import { mockCatalog, mockQuotationCart } from "../mock";
import type {
  CatalogAsset, FulfillmentQueueRow, PublicConfig, QuotationCartOrDetail,
  QuotationListRow, QuotationNotification, QuotationOutsourcedItemCreate,
  QuotationOutsourceShortfallItem,
} from "../types";
import { API_BASE, qs, rawFetch, tryLoad } from "./core";
import type { DirectoryPage } from "./core";


export const quotationsApi = {
  publicConfig: () => rawFetch<PublicConfig>("/config/public"),
  // Omitting limit/offset/search returns the WHOLE active catalog in one
  // response (backend default -- see services/quotation_service.py's
  // list_catalog()), which is what every full-catalog consumer still
  // wants (the Admin/Manager Quote Detail drawer's "Add another asset"
  // typeahead searches this list entirely client-side).
  catalog: () => tryLoad(async () => (await rawFetch<{ items: CatalogAsset[]; show_stock: boolean }>("/assets/catalog")).items, mockCatalog),
  // True server-side paged + searched catalog page -- same
  // limit/offset/search -> {items,total,limit,offset} contract as
  // assetsApi.list/usersApi.list/etc (DirectoryPage<T>). Used by the
  // Quotations page's own browsable "Asset Catalog" table.
  catalogPage: (limit: number, offset: number, search = ""): Promise<DirectoryPage<CatalogAsset>> =>
    tryLoad(async () => {
      const data = await rawFetch<{ items: CatalogAsset[]; total: number; limit: number; offset: number; show_stock: boolean }>(`/assets/catalog${qs({ limit, offset, search })}`);
      return { items: data.items ?? [], total: data.total, limit: data.limit, offset: data.offset };
    }, (() => {
      const filtered = search.trim()
        ? mockCatalog.filter((a) => a.name.toLowerCase().includes(search.trim().toLowerCase()) || (a.category ?? "").toLowerCase().includes(search.trim().toLowerCase()))
        : mockCatalog;
      return { items: filtered.slice(offset, offset + limit), total: filtered.length, limit, offset };
    })()),

  // ---- self-service: "My Order" (draft cart) ----
  myCart: () => tryLoad(() => rawFetch<QuotationCartOrDetail>("/quotations/me"), mockQuotationCart),
  addToCart: (assetId: number, quantity: number, startDate: string, dueDate: string) =>
    rawFetch<QuotationCartOrDetail>("/quotations/items", {
      method: "POST",
      body: JSON.stringify({ asset_id: assetId, quantity, start_date: startDate, due_date: dueDate }),
    }),
  updateCartItem: (itemId: number, quantity: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/items/${itemId}`, { method: "PUT", body: JSON.stringify({ quantity }) }),
  removeCartItem: (itemId: number) => rawFetch<QuotationCartOrDetail>(`/quotations/items/${itemId}`, { method: "DELETE" }),
  // `idempotencyKey`: see assets.ts's checkoutAdvanced() for the full
  // reasoning -- this is the actual "checkout" moment for a self-service
  // order, so it gets the same double-click protection.
  submitCart: (idempotencyKey?: string) =>
    rawFetch<QuotationCartOrDetail>("/quotations/submit", { method: "POST", headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined }),

  // ---- self-service: "My Quotes" (submitted history + own detail) ----
  myHistory: () => tryLoad(async () => (await rawFetch<{ items: QuotationListRow[] }>("/quotations/me/history")).items, []),
  myQuoteDetail: (quotationId: number) => rawFetch<QuotationCartOrDetail>(`/quotations/me/${quotationId}`),
  updateMyQuoteItem: (quotationId: number, itemId: number, quantity: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/me/${quotationId}/items/${itemId}`, { method: "PUT", body: JSON.stringify({ quantity }) }),
  removeMyQuoteItem: (quotationId: number, itemId: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/me/${quotationId}/items/${itemId}`, { method: "DELETE" }),

  // ---- self-service: in-app "Quotation updates" notifications ----
  // (assigned/updated alerts -- see lib/types.ts's QuotationNotification
  // and backend/services/quotation_service.py's
  // _notify_quotation_recipient()). Fails soft to an empty list, same as
  // every other alert feed the Notification Bell polls, so a transient
  // error here never breaks the bell's overall unread count.
  myNotifications: () =>
    tryLoad(async () => (await rawFetch<{ items: QuotationNotification[] }>("/quotations/me/notifications")).items, []),
  markNotificationsRead: (notificationIds: number[]) =>
    rawFetch<{ updated: number }>("/quotations/me/notifications/read", {
      method: "POST",
      body: JSON.stringify({ notification_ids: notificationIds }),
    }),
  addMyQuoteItem: (quotationId: number, assetId: number, quantity: number, startDate: string, dueDate: string) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/me/${quotationId}/items`, {
      method: "POST",
      body: JSON.stringify({ asset_id: assetId, quantity, start_date: startDate, due_date: dueDate }),
    }),

  // ---- Admin/Manager: the "Quotes" tab (require_privileged_role) ----
  list: (limit: number, offset: number, search: string) =>
    rawFetch<DirectoryPage<QuotationListRow>>(`/quotations${qs({ limit, offset, search })}`),
  detail: (quotationId: number) => rawFetch<QuotationCartOrDetail & { id: number }>(`/quotations/${quotationId}`),
  create: (payload: Record<string, unknown>) => rawFetch<QuotationCartOrDetail & { id: number }>("/quotations", { method: "POST", body: JSON.stringify(payload) }),
  remove: (quotationId: number) => rawFetch<void>(`/quotations/${quotationId}`, { method: "DELETE" }),
  approve: (quotationId: number) => rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/approve`, { method: "POST" }),
  markPaid: (quotationId: number, paymentMethod: string, paymentReference?: string | null) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/paid`, { method: "POST", body: JSON.stringify({ payment_method: paymentMethod, payment_reference: paymentReference || null }) }),
  saveNotes: (quotationId: number, notes: string) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}`, { method: "PUT", body: JSON.stringify({ notes: notes || null }) }),
  saveDiscount: (quotationId: number, discountPercent: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/discount`, { method: "PUT", body: JSON.stringify({ discount_percent: discountPercent }) }),
  addItem: (quotationId: number, assetId: number, quantity: number, startDate: string, dueDate: string) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/items`, {
      method: "POST",
      body: JSON.stringify({ asset_id: assetId, quantity, start_date: startDate, due_date: dueDate }),
    }),
  updateItem: (quotationId: number, itemId: number, quantity: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/items/${itemId}`, { method: "PUT", body: JSON.stringify({ quantity }) }),
  removeItem: (quotationId: number, itemId: number) => rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/items/${itemId}`, { method: "DELETE" }),
  assign: (quotationId: number, payload: Record<string, unknown>) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/assign`, { method: "POST", body: JSON.stringify(payload) }),

  // ---- Admin/Manager-only: "not currently in inventory" lines (Manager/
  // Admin-added, requester can see but not edit/remove) ----
  addOutsourcedItem: (quotationId: number, payload: QuotationOutsourcedItemCreate) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/outsourced-items`, { method: "POST", body: JSON.stringify(payload) }),
  removeOutsourcedItem: (quotationId: number, itemId: number) =>
    rawFetch<QuotationCartOrDetail>(`/quotations/${quotationId}/outsourced-items/${itemId}`, { method: "DELETE" }),

  // ---- Admin/Manager: Fulfillment Drawer (approved -> checked out) ----
  fulfillmentQueue: () => tryLoad(async () => (await rawFetch<{ items: FulfillmentQueueRow[] }>("/quotations/fulfillment-queue")).items, []),
  // `outsourceShortfallItems` pre-authorizes specific inventory-backed lines
  // (optionally split across more than one external source) to be sourced
  // externally rather than blocking the whole checkout if the row-locked
  // stock check at this exact moment finds them genuinely short -- see
  // QuotationCheckoutRequest/bulk_checkout_quotation() on the backend.
  checkout: (quotationId: number, outsourceShortfallItems: QuotationOutsourceShortfallItem[] = []) =>
    rawFetch<{ message?: string }>(`/quotations/${quotationId}/checkout`, { method: "POST", body: JSON.stringify({ outsource_shortfall_items: outsourceShortfallItems }) }),

  // ---- Admin-only: global VAT setting ----
  getVat: () => rawFetch<{ vat_percent: number }>("/settings/vat"),
  setVat: (vatPercent: number) => rawFetch<{ vat_percent: number }>("/settings/vat", { method: "PUT", body: JSON.stringify({ vat_percent: vatPercent }) }),

  // ---- PDF export links (not JSON round trips -- each streams a file
  // directly, same pattern as assetsApi.exportUrl). Ported from the legacy
  // frontend's components/exports.js: exportQuotation() for the caller's
  // current draft, exportMyQuoteDetail()/exportQuoteDetail() for one
  // submitted Quotation by ID (self-service vs Admin/Manager). PDF only --
  // unlike the directory/custody exports above, the backend never
  // accepted a `format` query param here (a Quotation is a formatted
  // document, not a tabular row set). ----
  exportCartUrl: () => `${API_BASE}/quotations/export`,
  exportMyQuoteUrl: (quotationId: number) => `${API_BASE}/quotations/me/${quotationId}/export`,
  exportQuoteUrl: (quotationId: number) => `${API_BASE}/quotations/${quotationId}/export`,
};
