// =============================================================================
// js/components/quotation.js
// -----------------------------------------------------------------------------
// Two halves:
//
// 1) SELF-SERVICE (staff.html / customer.html): browse the read-only Asset
//    Catalog, add items with a quantity + rental date range to your own
//    saved order ("My Order"), edit quantities or remove lines, export the
//    current order as a PDF, and SUBMIT it -- which stamps it with a
//    permanent Quotation ID ("QT-000001") and moves it into "My Submitted
//    Quotes" (read-only from here on).
//
// 2) ADMIN/MANAGER "Quotes" tab (admin.html / manager.html): look up any
//    submitted Quotation by its ID or requester, adjust its line items,
//    leave notes, assign it to a user, and export/print it.
//
// Backed by backend/api/quotations.py + backend/services/quotation_service.py.
//
// Whether the catalog shows stock status/available quantity is controlled
// server-side (settings.CATALOG_SHOW_STOCK_TO_STAFF_CUSTOMER -- see
// config.py) -- this module just renders whatever GET /assets/catalog
// actually returns rather than deciding visibility itself.
// =============================================================================

import { apiRequest } from '../api.js';
import { getSession } from '../auth.js';
import {
  escapeHtml, formatPrice, setCurrencyCode, applySiteName, showToast, showFieldError, clearFieldError,
  openModal, closeModal, tableState, registerRenderer, filterAndPaginate, renderPaginationBar,
  rowDetailsTrigger, debounce, renderServerPaginationBar, formatTimestamp, resetTabScroll,
} from '../ui.js';

let showStock = false;

// -----------------------------------------------------------------------------
// Status badge -- the gray "Draft"-style pill turning into the sharp green
// "Approved / Ready for Pickup" pill once a Manager/Admin approves a
// submitted Quotation (see backend/services/quotation_service.py's
// approve_quotation()), and finally a neutral "Fulfilled" pill once it's
// been physically bulk-checked-out. Shared by the requester's own "My
// Quotes" panel and the Admin/Manager "Quotes" master queue so both sides
// of the Quote-to-Checkout workflow always read the same way.
// -----------------------------------------------------------------------------
export function quotationStatusBadge(status) {
  const variants = {
    submitted: { dot: 'bg-slate-400', bg: 'bg-slate-500/10', text: 'text-slate-300', ring: 'ring-slate-500/30', label: 'Pending Review' },
    approved: { dot: 'bg-emerald-500', bg: 'bg-emerald-500/10', text: 'text-emerald-400', ring: 'ring-emerald-500/30', label: 'Approved · Ready for Pickup' },
    fulfilled: { dot: 'bg-blue-500', bg: 'bg-blue-500/10', text: 'text-blue-400', ring: 'ring-blue-500/30', label: 'Fulfilled' },
    draft: { dot: 'bg-slate-500', bg: 'bg-slate-500/10', text: 'text-slate-400', ring: 'ring-slate-500/30', label: 'Draft' },
  };
  const v = variants[status] || variants.draft;
  return `<span class="inline-flex items-center gap-1 whitespace-nowrap rounded-full ${v.bg} px-2.5 py-1 text-[11px] font-semibold ${v.text} ring-1 ${v.ring}"><span class="h-1.5 w-1.5 rounded-full ${v.dot}"></span> ${v.label}</span>`;
}

// -----------------------------------------------------------------------------
// Public config (currency + stock visibility + site name) -- fetched once
// on every page load, including the unauthenticated login page (see
// main.js's DOMContentLoaded handler), so the navbar/login brand and
// <title> always reflect the live settings.SITE_NAME deployment value
// instead of the generic "Asset Registry" default baked into the HTML.
// -----------------------------------------------------------------------------
export async function loadPublicConfig() {
  try {
    const config = await apiRequest('/config/public');
    setCurrencyCode(config.currency_code);
    applySiteName(config.site_name);
  } catch (err) {
    // Non-fatal -- formatPrice() falls back to its own default (NGN), and
    // the page just keeps whatever site name is already in the markup.
  }
}

// =============================================================================
// SELF-SERVICE: ASSET CATALOG (browse + add to order)
// -----------------------------------------------------------------------------
// Small, bounded list (every active asset pool) -- uses the same client-
// side fetch-once/filter-in-memory tableState machinery as My Items (see
// js/ui.js), now with a real search box + "rows per page" selector, and a
// separate mobile card layout so the quantity/date/Add controls always fit
// on a phone instead of forcing a horizontal-scrolling table row.
// =============================================================================
export async function loadCatalog() {
  const tbody = document.getElementById('quotationCatalogBody');
  if (!tbody) return;
  try {
    const data = await apiRequest('/assets/catalog');
    showStock = data.show_stock;
    tableState.quotationCatalog.raw = data.items;
    renderCatalogTable();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="9" class="px-5 py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
  }
}

// One asset row's quantity/start/due inputs -- shared markup fragment
// (just the three inputs, no wrapper) reused by both the desktop table's
// separate Qty/Start/Due <td>s and the mobile card's stacked layout, so
// the id scheme addAssetToOrder() reads back never drifts between the two.
function catalogQtyInput(a, idSuffix, extraClass) {
  return `<input type="number" min="1" value="1" id="qcat-qty-${idSuffix}${a.id}" title="Quantity"
    class="rounded-md border border-border bg-card2 px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-blue-500/60 ${extraClass}" />`;
}
function catalogStartInput(a, today, idSuffix, extraClass) {
  return `<input type="date" id="qcat-start-${idSuffix}${a.id}" min="${today}" value="${today}" title="Start date"
    class="min-w-0 rounded-md border border-border bg-card2 px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-blue-500/60 ${extraClass}" />`;
}
function catalogDueInput(a, today, idSuffix, extraClass) {
  return `<input type="date" id="qcat-due-${idSuffix}${a.id}" min="${today}" value="${today}" title="Due date"
    class="min-w-0 rounded-md border border-border bg-card2 px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-blue-500/60 ${extraClass}" />`;
}
function catalogAddButton(a, idSuffix, extraClass) {
  return `<button type="button" data-action="add-to-order" data-asset-id="${a.id}" data-context="${idSuffix}" data-asset-name="${escapeHtml(a.name)}"
    class="rounded-md bg-blue-600 px-3 py-1.5 text-[12px] font-semibold text-white transition hover:bg-blue-500 active:bg-blue-700 ${extraClass}">
    Add
  </button>`;
}

// Desktop table: Qty/Start/Due/Add are their own separate, narrow <td>s
// (matching the header's own Qty | Start | Due | Add columns exactly)
// instead of one wide merged cell -- so the quantity box never has to
// stretch wider than a couple of digits need.
function catalogDesktopCells(a, today) {
  return `
    <td class="px-2 py-3">${catalogQtyInput(a, '', 'w-14')}</td>
    <td class="px-2 py-3">${catalogStartInput(a, today, '', 'w-[8.5rem]')}</td>
    <td class="px-2 py-3">${catalogDueInput(a, today, '', 'w-[8.5rem]')}</td>
    <td class="px-3 py-3 text-right">${catalogAddButton(a, '', 'w-full sm:w-auto')}</td>`;
}

// Mobile card: same three inputs + Add button, stacked with labels, each
// given the full card width to work with instead of a cramped table row.
function catalogMobileCardControls(a, today) {
  const idSuffix = 'm-';
  return `
    <div class="grid grid-cols-3 gap-2">
      <div>
        <label class="mb-1 block text-[10px] font-medium uppercase tracking-wide text-slate-500">Qty</label>
        ${catalogQtyInput(a, idSuffix, 'w-full')}
      </div>
      <div>
        <label class="mb-1 block text-[10px] font-medium uppercase tracking-wide text-slate-500">Start</label>
        ${catalogStartInput(a, today, idSuffix, 'w-full')}
      </div>
      <div>
        <label class="mb-1 block text-[10px] font-medium uppercase tracking-wide text-slate-500">Due</label>
        ${catalogDueInput(a, today, idSuffix, 'w-full')}
      </div>
    </div>
    ${catalogAddButton(a, idSuffix, 'w-full')}`;
}

function renderCatalogTable() {
  const tbody = document.getElementById('quotationCatalogBody');
  const cardsWrap = document.getElementById('quotationCatalogCards');
  const theadStockCells = document.querySelectorAll('.qcat-stock-col');
  theadStockCells.forEach((el) => el.classList.toggle('hidden', !showStock));
  if (!tbody) return;

  const { pageRows, total, startIndex } = filterAndPaginate('quotationCatalog', ['name', 'category']);
  const catalogCountEl = document.getElementById('quotationCatalogCount');
  if (catalogCountEl) catalogCountEl.textContent = total;

  const today = new Date().toISOString().slice(0, 10);

  if (pageRows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" class="px-5 py-6 text-center text-slate-500">No assets found.</td></tr>`;
    if (cardsWrap) cardsWrap.innerHTML = `<p class="px-4 py-6 text-center text-[13px] text-slate-500">No assets found.</p>`;
    renderPaginationBar('quotationCatalog', total, startIndex, pageRows.length);
    return;
  }

  // ---- Desktop table rows (sm and up) -- Qty/Start/Due/Add are their
  // own dedicated columns (catalogDesktopCells()) so they line up under
  // the "Qty | Start | Due | Add" header instead of one merged cell.
  tbody.innerHTML = pageRows.map((a) => {
    const priceLabel = a.price !== null && a.price !== undefined ? formatPrice(a.price) : '—';
    const stockCells = showStock
      ? `<td class="qcat-stock-col hidden px-3 py-3 sm:table-cell">${a.available_quantity}</td>
         <td class="qcat-stock-col hidden px-3 py-3 sm:table-cell">
           <span class="rounded-full px-2 py-0.5 text-[11px] font-medium ${a.status === 'In Stock' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'}">${escapeHtml(a.status)}</span>
         </td>`
      : '';
    return `
      <tr class="align-top" data-asset-row="${a.id}">
        <td class="px-3 py-3">
          <p class="font-medium text-slate-100">${escapeHtml(a.name)}</p>
          <p class="text-[11px] text-slate-500">${a.category ? escapeHtml(a.category) : '—'}</p>
        </td>
        <td class="hidden px-3 py-3 sm:table-cell">${a.category ? escapeHtml(a.category) : '—'}</td>
        <td class="px-3 py-3 tag-mono">${priceLabel}${priceLabel !== '—' ? '<span class="text-slate-500">/day</span>' : ''}</td>
        ${stockCells}
        ${catalogDesktopCells(a, today)}
      </tr>`;
  }).join('');

  // ---- Mobile cards (below sm) -- each asset is a self-contained card so
  // the quantity/date/Add controls always have the full screen width to
  // work with instead of being squeezed into a horizontally-scrolling
  // table row (the "Add button not showing" issue). Uses a "-m" id suffix
  // so these inputs never collide with the desktop table's copies above.
  if (cardsWrap) {
    cardsWrap.innerHTML = pageRows.map((a) => {
      const priceLabel = a.price !== null && a.price !== undefined ? formatPrice(a.price) : '—';
      const stockLine = showStock
        ? `<span class="rounded-full px-2 py-0.5 text-[11px] font-medium ${a.status === 'In Stock' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-rose-500/10 text-rose-400'}">${escapeHtml(a.status)} · ${a.available_quantity} available</span>`
        : '';
      return `
        <div class="space-y-3 border-b border-border p-4 last:border-0">
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <p class="truncate font-medium text-slate-100">${escapeHtml(a.name)}</p>
              <p class="text-[11px] text-slate-500">${a.category ? escapeHtml(a.category) : '—'}</p>
            </div>
            <p class="shrink-0 tag-mono text-[13px] text-slate-200">${priceLabel}${priceLabel !== '—' ? '<span class="text-slate-500">/day</span>' : ''}</p>
          </div>
          ${stockLine ? `<div>${stockLine}</div>` : ''}
          ${catalogMobileCardControls(a, today)}
        </div>`;
    }).join('');
  }

  renderPaginationBar('quotationCatalog', total, startIndex, pageRows.length);
}
registerRenderer('quotationCatalog', renderCatalogTable);

export async function addAssetToOrder(assetId, context = '') {
  const qtyInput = document.getElementById(`qcat-qty-${context}${assetId}`);
  const startInput = document.getElementById(`qcat-start-${context}${assetId}`);
  const dueInput = document.getElementById(`qcat-due-${context}${assetId}`);
  if (!qtyInput || !startInput || !dueInput) return;

  const quantity = parseInt(qtyInput.value, 10);
  const startDate = startInput.value;
  const dueDate = dueInput.value;

  if (!quantity || quantity < 1) {
    showFieldError(`qcat-qty-${context}${assetId}`, 'Enter a quantity of at least 1.');
    return;
  }
  if (!startDate || !dueDate) {
    showFieldError(`qcat-due-${context}${assetId}`, 'Pick both a start and a due date.');
    return;
  }
  if (dueDate < startDate) {
    showFieldError(`qcat-due-${context}${assetId}`, 'Due date cannot be before the start date.');
    return;
  }
  clearFieldError(`qcat-qty-${context}${assetId}`);
  clearFieldError(`qcat-due-${context}${assetId}`);

  try {
    await apiRequest('/quotations/items', {
      method: 'POST',
      body: JSON.stringify({ asset_id: assetId, quantity, start_date: startDate, due_date: dueDate }),
    });
    showToast('Added to your order.');
    loadMyQuotation();
  } catch (err) {
    alert(err.message);
  }
}

// =============================================================================
// SELF-SERVICE: MY ORDER (saved draft -- view / edit qty / remove / export / submit)
// =============================================================================
export async function loadMyQuotation() {
  const tbody = document.getElementById('myOrderTableBody');
  if (!tbody) return;
  try {
    const data = await apiRequest('/quotations/me');
    renderMyQuotation(data);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="px-5 py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
  }
}

function renderMyQuotation(data) {
  const tbody = document.getElementById('myOrderTableBody');
  const cardsWrap = document.getElementById('myOrderCards');
  const exportBtn = document.getElementById('exportQuotationBtn');
  const submitBtn = document.getElementById('submitQuotationBtn');
  const itemCountEl = document.getElementById('myOrderItemCount');
  const tabCountEl = document.getElementById('myOrderTabCount');
  if (itemCountEl) itemCountEl.textContent = data.items.length;
  if (tabCountEl) tabCountEl.textContent = data.items.length;
  if (exportBtn) exportBtn.disabled = data.items.length === 0;
  if (submitBtn) submitBtn.disabled = data.items.length === 0;

  if (data.items.length === 0) {
    const emptyMsg = 'Your saved order is empty -- add assets from the catalog above.';
    tbody.innerHTML = `<tr><td colspan="8" class="px-5 py-6 text-center text-slate-500">${emptyMsg}</td></tr>`;
    if (cardsWrap) cardsWrap.innerHTML = `<p class="px-4 py-6 text-center text-[13px] text-slate-500">${emptyMsg}</p>`;
  } else {
    tbody.innerHTML = data.items.map((li) => `
      <tr data-order-item-row="${li.item_id}">
        <td class="px-3 py-3">
          <p class="font-medium text-slate-100">${escapeHtml(li.asset_name)}</p>
          <p class="text-[11px] text-slate-500 sm:hidden">${li.category ? escapeHtml(li.category) : '—'} · ${li.start_date} → ${li.due_date}</p>
        </td>
        <td class="hidden px-3 py-3 sm:table-cell">${li.category ? escapeHtml(li.category) : '—'}</td>
        <td class="px-3 py-3">
          <input type="number" min="1" value="${li.quantity}" data-action="update-order-qty" data-item-id="${li.item_id}"
            class="w-16 rounded-md border border-border bg-card2 px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-blue-500/60" />
        </td>
        <td class="hidden px-3 py-3 tag-mono sm:table-cell">${li.start_date}</td>
        <td class="hidden px-3 py-3 tag-mono sm:table-cell">${li.due_date}</td>
        <td class="hidden px-3 py-3 sm:table-cell">${li.days}</td>
        <td class="px-3 py-3 tag-mono">${formatPrice(li.line_total)}</td>
        <td class="px-3 py-3 text-right">
          <button type="button" data-action="remove-order-item" data-item-id="${li.item_id}" title="Remove from order"
            class="rounded-md border border-border p-1.5 text-slate-400 transition hover:border-rose-500/40 hover:text-rose-400">
            <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6"/></svg>
          </button>
        </td>
      </tr>`).join('');

    if (cardsWrap) {
      cardsWrap.innerHTML = data.items.map((li) => `
        <div class="space-y-2.5 border-b border-border p-4 last:border-0" data-order-item-card="${li.item_id}">
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <p class="truncate font-medium text-slate-100">${escapeHtml(li.asset_name)}</p>
              <p class="text-[11px] text-slate-500">${li.category ? escapeHtml(li.category) : '—'} · ${li.start_date} → ${li.due_date} (${li.days}d)</p>
            </div>
            <button type="button" data-action="remove-order-item" data-item-id="${li.item_id}" title="Remove from order"
              class="shrink-0 rounded-md border border-border p-1.5 text-slate-400 transition hover:border-rose-500/40 hover:text-rose-400">
              <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6"/></svg>
            </button>
          </div>
          <div class="flex items-center justify-between gap-3">
            <label class="flex items-center gap-2 text-[12px] text-slate-500">
              Qty
              <input type="number" min="1" value="${li.quantity}" data-action="update-order-qty" data-item-id="${li.item_id}"
                class="w-16 rounded-md border border-border bg-card2 px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-blue-500/60" />
            </label>
            <p class="tag-mono text-[13px] text-slate-100">${formatPrice(li.line_total)}</p>
          </div>
        </div>`).join('');
    }
  }

  const subtotalEl = document.getElementById('quotationSubtotal');
  const vatLabelEl = document.getElementById('quotationVatLabel');
  const vatAmountEl = document.getElementById('quotationVatAmount');
  const totalEl = document.getElementById('quotationTotal');
  const discountRowEl = document.getElementById('quotationDiscountRow');
  const discountLabelEl = document.getElementById('quotationDiscountLabel');
  const discountAmountEl = document.getElementById('quotationDiscountAmount');
  if (subtotalEl) subtotalEl.textContent = formatPrice(data.subtotal);
  if (vatLabelEl) vatLabelEl.textContent = `VAT (${data.vat_percent}%)`;
  if (vatAmountEl) vatAmountEl.textContent = formatPrice(data.vat_amount);
  if (totalEl) totalEl.textContent = formatPrice(data.total);
  // Discount (Admin/Manager-set, see services/quotation_service.py's
  // _serialize_quotation()) -- hidden entirely when 0/unset, same "hide
  // rather than show a zero row" pattern used elsewhere in this app.
  if (discountRowEl) {
    if (data.discount_percent) {
      if (discountLabelEl) discountLabelEl.textContent = `Discount (${data.discount_percent}%)`;
      if (discountAmountEl) discountAmountEl.textContent = `-${formatPrice(data.discount_amount)}`;
      discountRowEl.classList.remove('hidden');
      discountRowEl.classList.add('flex');
    } else {
      discountRowEl.classList.add('hidden');
      discountRowEl.classList.remove('flex');
    }
  }
}

export async function updateOrderItemQuantity(itemId, quantity) {
  const qty = parseInt(quantity, 10);
  if (!qty || qty < 1) return;
  try {
    const data = await apiRequest(`/quotations/items/${itemId}`, {
      method: 'PUT',
      body: JSON.stringify({ quantity: qty }),
    });
    renderMyQuotation(data);
  } catch (err) {
    alert(err.message);
    loadMyQuotation(); // re-sync the input back to the real saved value
  }
}

export async function removeOrderItem(itemId) {
  try {
    const data = await apiRequest(`/quotations/items/${itemId}`, { method: 'DELETE' });
    showToast('Removed from your order.');
    renderMyQuotation(data);
  } catch (err) {
    alert(err.message);
  }
}

// -----------------------------------------------------------------------------
// SUBMIT -- turns "My Order" into a permanent Quotation ID an Admin/Manager
// can pull up, adjust, and assign. A brand new empty draft starts right
// after, exactly like starting a fresh cart.
// -----------------------------------------------------------------------------
export async function submitMyQuotation(button) {
  const original = button ? button.textContent : null;
  if (button) { button.disabled = true; button.textContent = 'Submitting…'; }
  try {
    const data = await apiRequest('/quotations/submit', { method: 'POST' });
    showSubmittedQuotationModal(data);
    loadMyQuotation();
    loadMyQuotationHistory();
  } catch (err) {
    alert(err.message);
  } finally {
    if (button) { button.disabled = false; button.textContent = original; }
  }
}

function showSubmittedQuotationModal(data) {
  const refEl = document.getElementById('submittedQuotationRef');
  const bodyEl = document.getElementById('submittedQuotationBody');
  if (!refEl || !bodyEl) {
    // Fallback for any page without the modal markup.
    showToast(`Quotation submitted -- ID ${data.reference_number}`);
    return;
  }
  refEl.textContent = data.reference_number;
  bodyEl.textContent = `${data.items.length} item(s) · ${formatPrice(data.total)} total. Share this ID with your manager -- they can pull it up, adjust it, and assign it to you or another user.`;
  openModal('submittedQuotationModal');
}

// -----------------------------------------------------------------------------
// MY SUBMITTED QUOTES (read-only history -- staff.html/customer.html)
// -----------------------------------------------------------------------------
export async function loadMyQuotationHistory() {
  const tbody = document.getElementById('myQuotationHistoryBody');
  if (!tbody) return;
  try {
    const data = await apiRequest('/quotations/me/history');
    renderMyQuotationHistory(data.items);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" class="px-5 py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
  }
}

// True if the currently-logged-in account is the one that actually built
// and submitted this quote (models.Quotation.user_id), as opposed to a
// quote an Admin/Manager built and assigned TO this account (see
// backend/services/quotation_service.py's list_my_submitted_quotations()
// docstring for why both now show up in the same "My Quotes" list).
// `requester` is only present when the backend serialized with
// include_admin_fields=True, which "My Quotes" always does.
function isOwnRequester(q) {
  if (!q.requester) return true;
  const session = getSession();
  return session ? String(q.requester.id) === String(session.sub) : true;
}

function renderMyQuotationHistory(items) {
  const tbody = document.getElementById('myQuotationHistoryBody');
  const tabCountEl = document.getElementById('myQuotesTabCount');
  if (!tbody) return;
  if (tabCountEl) tabCountEl.textContent = items.length;

  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="px-5 py-6 text-center text-slate-500">You haven't submitted any quotes yet -- build an order on the "My Order" tab, then Submit Quotation. Quotes an Admin/Manager builds and assigns to you will also show up here.</td></tr>`;
    return;
  }

  tbody.innerHTML = items.map((q) => `
    <tr data-action="open-my-quote-detail" data-quotation-id="${q.id}" class="cursor-pointer transition hover:bg-card2/40 active:bg-card2/60">
      <td class="px-3 py-3 tag-mono font-medium text-slate-100">
        ${escapeHtml(q.reference_number)}
        ${!isOwnRequester(q) ? `<span class="mt-0.5 block max-w-[10rem] truncate tag-mono text-[11px] font-normal normal-case text-slate-500" title="Requested on your behalf by ${escapeHtml(q.requester.name)}">Requested by ${escapeHtml(q.requester.name)}</span>` : ''}
      </td>
      <td class="px-3 py-3">${quotationStatusBadge(q.status)}</td>
      <td class="hidden px-3 py-3 tag-mono text-slate-300 sm:table-cell">${escapeHtml(formatTimestamp(q.submitted_at))}</td>
      <td class="hidden px-3 py-3 sm:table-cell">${q.items.length}</td>
      <td class="px-3 py-3 tag-mono">${formatPrice(q.total)}</td>
      <td class="hidden px-3 py-3 sm:table-cell">${q.assigned_to ? escapeHtml(q.assigned_to.name) : (q.assigned_outsider ? `${escapeHtml(q.assigned_outsider.name)} (Ad-Hoc)` : '<span class="text-slate-500">Unassigned</span>')}</td>
    </tr>`).join('');
}

// -----------------------------------------------------------------------------
// MY QUOTE DETAIL (self-service) -- opened from a "My Quotes" history row.
// Still adjustable (quantity/remove line) while `status === "submitted"`
// (unapproved) -- by the original requester AND, if an Admin/Manager
// assigned this quote to someone else, by that assignee too; read-only
// once Approved/Fulfilled. Mirrors the shape of the Admin/Manager Quote
// Detail modal, minus the admin-only affordances (approve, assign-to-user,
// notes, add-another-asset) that don't belong on the requester/assignee's
// own side. See backend/services/quotation_service.py's
// _get_own_editable_quotation() / _get_own_or_assigned_quotation_or_404().
// -----------------------------------------------------------------------------
let myQuoteDetailId = null;

export async function openMyQuoteDetail(quotationId) {
  myQuoteDetailId = quotationId;
  clearMyQuoteDetailAsset(); // reset any leftover selection from a previously-opened quote
  await ensureQuoteCatalogLoaded();
  try {
    const data = await apiRequest(`/quotations/me/${quotationId}`);
    renderMyQuoteDetail(data);
    openModal('myQuoteDetailModal');
  } catch (err) {
    alert(err.message);
  }
}

// Lets components/exports.js's My Quote Detail export button find out
// which of the caller's OWN submitted Quotations is currently open --
// same pattern as getCurrentQuoteId() above (the Admin/Manager Quote
// Detail modal's equivalent getter).
export function getCurrentMyQuoteDetailId() {
  return myQuoteDetailId;
}

function renderMyQuoteDetail(data) {
  const refEl = document.getElementById('myQuoteDetailRef');
  const statusEl = document.getElementById('myQuoteDetailStatusBadge');
  const metaEl = document.getElementById('myQuoteDetailMeta');
  const noticeEl = document.getElementById('myQuoteDetailEditableNotice');
  const addItemForm = document.getElementById('myQuoteDetailAddItemForm');
  const tbody = document.getElementById('myQuoteDetailItemsBody');
  const cardsWrap = document.getElementById('myQuoteDetailCards');
  const subtotalEl = document.getElementById('myQuoteDetailSubtotal');
  const vatEl = document.getElementById('myQuoteDetailVat');
  const totalEl = document.getElementById('myQuoteDetailTotal');
  const assignedEl = document.getElementById('myQuoteDetailAssignedTo');
  if (!refEl || !tbody) return;

  // A "My Quotes" row is only ever visible here because the viewer is
  // EITHER the original requester OR the person the quote is assigned to
  // (see backend/services/quotation_service.py's
  // list_my_submitted_quotations()/_get_own_or_assigned_quotation_or_404())
  // -- and, while still "submitted" (unapproved), both can now adjust
  // quantities/remove lines (see that module's _get_own_editable_quotation()).
  // So editability here only ever depends on status, not on which of the
  // two the viewer is.
  const editable = data.status === 'submitted';
  refEl.textContent = data.reference_number;
  if (statusEl) statusEl.innerHTML = quotationStatusBadge(data.status);
  if (metaEl) {
    const requesterNote = !isOwnRequester(data) ? ` · Requested by ${escapeHtml(data.requester.name)}` : '';
    metaEl.textContent = `Submitted ${formatTimestamp(data.submitted_at)} · ${data.items.length} item(s)${requesterNote}`;
  }
  if (noticeEl) {
    // Blue "you can still edit this" notice whenever the quote is still
    // editable (Pending Review) -- worded slightly differently depending
    // on whether the viewer built it themselves or it was assigned to
    // them by an Admin/Manager. Hidden entirely once it's no longer
    // editable (Approved/Fulfilled), for both cases alike.
    const notOwn = !isOwnRequester(data);
    noticeEl.classList.toggle('hidden', !editable);
    noticeEl.classList.toggle('border-blue-500/30', editable);
    noticeEl.classList.toggle('bg-blue-500/10', editable);
    noticeEl.classList.toggle('text-blue-300', editable);
    noticeEl.textContent = notOwn
      ? `Requested on your behalf by ${data.requester.name} -- assigned to you, you can adjust quantities or remove lines until it's approved.`
      : "Still Pending Review -- you can adjust quantities or remove lines until it's approved.";
  }
  if (assignedEl) assignedEl.textContent = data.assigned_to ? data.assigned_to.name : 'Unassigned';
  // "Add item" -- same editable gate as qty inputs/remove buttons below
  // (only while status === "submitted"); hidden entirely once approved/fulfilled.
  if (addItemForm) addItemForm.classList.toggle('hidden', !editable);

  const removeButton = (li) => (editable && !li.is_outsourced)
    ? `<button type="button" data-action="remove-my-quote-item" data-item-id="${li.item_id}" title="Remove from quote"
        class="shrink-0 rounded-md border border-border p-1.5 text-slate-400 transition hover:border-rose-500/40 hover:text-rose-400">
        <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6"/></svg>
      </button>`
    : '';
  const qtyInput = (li, extraClass) => (editable && !li.is_outsourced)
    ? `<input type="number" min="1" value="${li.quantity}" data-action="update-my-quote-item-qty" data-item-id="${li.item_id}"
        class="${extraClass} rounded-md border border-border bg-card2 px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-blue-500/60" />`
    : `<span class="tag-mono text-slate-300">${li.quantity}</span>`;
  // ---- Desktop table rows (sm and up) ----
  // Note: unlike the Admin/Manager Quote Detail modal, this self-service
  // view intentionally does NOT show a "Not in Inventory"/"Outsourced"
  // badge on these lines -- the requester/assignee doesn't need the
  // sourcing explanation, just the fixed qty/no-remove behavior below.
  tbody.innerHTML = data.items.map((li) => `
    <tr data-my-quote-item-row="${li.item_id || `out-${li.outsourced_item_id}`}">
      <td class="px-3 py-3">
        <p class="font-medium text-slate-100">${escapeHtml(li.asset_name)}</p>
      </td>
      <td class="px-3 py-3">${qtyInput(li, 'w-16')}</td>
      <td class="px-3 py-3 tag-mono">${li.start_date}</td>
      <td class="px-3 py-3 tag-mono">${li.due_date}</td>
      <td class="px-3 py-3 tag-mono">${formatPrice(li.line_total)}</td>
      <td class="px-3 py-3 text-right">${removeButton(li)}</td>
    </tr>`).join('');

  // ---- Mobile cards (below sm) -- same reasoning as #myOrderCards: a
  // table with Asset/Qty/Line Total/Remove crammed into a phone-width
  // modal either clips the Remove button off-screen or forces horizontal
  // scroll. Each line item gets the full modal width instead. ----
  if (cardsWrap) {
    cardsWrap.innerHTML = data.items.map((li) => `
      <div class="space-y-2.5 p-4" data-my-quote-item-card="${li.item_id || `out-${li.outsourced_item_id}`}">
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <p class="truncate font-medium text-slate-100">${escapeHtml(li.asset_name)}</p>
            <p class="text-[11px] text-slate-500">${li.start_date} → ${li.due_date}</p>
          </div>
          ${removeButton(li)}
        </div>
        <div class="flex items-center justify-between gap-3">
          <label class="flex items-center gap-2 text-[12px] text-slate-500">
            Qty ${qtyInput(li, 'w-16')}
          </label>
          <p class="tag-mono text-[13px] text-slate-100">${formatPrice(li.line_total)}</p>
        </div>
      </div>`).join('');
  }

  if (subtotalEl) subtotalEl.textContent = formatPrice(data.subtotal);
  if (vatEl) vatEl.textContent = `VAT (${data.vat_percent}%): ${formatPrice(data.vat_amount)}`;
  if (totalEl) totalEl.textContent = formatPrice(data.total);
  const discountEl = document.getElementById('myQuoteDetailDiscount');
  if (discountEl) {
    if (data.discount_percent) {
      discountEl.textContent = `Discount (${data.discount_percent}%): -${formatPrice(data.discount_amount)}`;
      discountEl.classList.remove('hidden');
    } else {
      discountEl.classList.add('hidden');
    }
  }
}

export async function updateMyQuoteItemQuantity(itemId, quantity) {
  const qty = parseInt(quantity, 10);
  if (!qty || qty < 1 || !myQuoteDetailId) return;
  try {
    const data = await apiRequest(`/quotations/me/${myQuoteDetailId}/items/${itemId}`, {
      method: 'PUT',
      body: JSON.stringify({ quantity: qty }),
    });
    renderMyQuoteDetail(data);
    loadMyQuotationHistory();
  } catch (err) {
    alert(err.message);
    openMyQuoteDetail(myQuoteDetailId); // re-sync the input back to the real saved value
  }
}

export async function removeMyQuoteItem(itemId) {
  if (!myQuoteDetailId) return;
  try {
    const data = await apiRequest(`/quotations/me/${myQuoteDetailId}/items/${itemId}`, { method: 'DELETE' });
    showToast('Removed from your quote.');
    renderMyQuoteDetail(data);
    loadMyQuotationHistory();
  } catch (err) {
    alert(err.message);
  }
}

// ---- Add item (self-service) -- lets the requester/assignee add another
// catalog asset to their own already-submitted quote while it's still
// "submitted" (unapproved). Same live search-box pattern as the
// Admin/Manager Quote Detail modal's "Add another asset" (see
// searchQuoteDetailAssets() and friends above), reusing the same shared
// quoteCatalogCache, just scoped to this modal's own myQuoteDetail* ids
// and posting to POST /quotations/me/{id}/items instead of
// /quotations/{id}/items. ----
let myQuoteDetailSelectedAssetId = null;

export const searchMyQuoteDetailAssets = debounce((query) => {
  const resultsEl = document.getElementById('myQuoteDetailAssetResults');
  if (!resultsEl) return;
  const term = query.trim().toLowerCase();
  if (!term) { resultsEl.innerHTML = ''; resultsEl.classList.add('hidden'); return; }
  const matches = quoteCatalogCache.filter((a) =>
    a.name.toLowerCase().includes(term) || (a.category || '').toLowerCase().includes(term)
  ).slice(0, 8);
  resultsEl.innerHTML = matches.length
    ? matches.map((a) => `
        <button type="button" data-action="select-my-quote-detail-asset" data-asset-id="${a.id}" data-asset-name="${escapeHtml(a.name)}"
          class="block w-full rounded-md px-3 py-2 text-left text-[12px] transition hover:bg-card2">
          <span class="font-medium text-slate-100">${escapeHtml(a.name)}</span>
          <span class="text-slate-500"> · ${a.category ? escapeHtml(a.category) : '—'}${a.price !== null && a.price !== undefined ? ` · ${escapeHtml(formatPrice(a.price))}/day` : ''}</span>
        </button>`).join('')
    : `<p class="px-3 py-2 text-[12px] text-slate-500">No matching assets.</p>`;
  resultsEl.classList.remove('hidden');
});

export function selectMyQuoteDetailAsset(assetId, assetName) {
  myQuoteDetailSelectedAssetId = assetId;
  const labelEl = document.getElementById('myQuoteDetailSelectedAssetLabel');
  const wrapEl = document.getElementById('myQuoteDetailSelectedAsset');
  const searchInput = document.getElementById('myQuoteDetailAssetSearchInput');
  const resultsEl = document.getElementById('myQuoteDetailAssetResults');
  if (labelEl) labelEl.textContent = assetName;
  if (wrapEl) wrapEl.classList.remove('hidden');
  if (searchInput) searchInput.value = '';
  if (resultsEl) { resultsEl.innerHTML = ''; resultsEl.classList.add('hidden'); }
  clearFieldError('myQuoteDetailAssetSearchInput');
}

export function clearMyQuoteDetailAsset() {
  myQuoteDetailSelectedAssetId = null;
  const wrapEl = document.getElementById('myQuoteDetailSelectedAsset');
  const searchInput = document.getElementById('myQuoteDetailAssetSearchInput');
  const resultsEl = document.getElementById('myQuoteDetailAssetResults');
  if (wrapEl) wrapEl.classList.add('hidden');
  if (searchInput) searchInput.value = '';
  if (resultsEl) { resultsEl.innerHTML = ''; resultsEl.classList.add('hidden'); }
}

export async function addMyQuoteDetailItem() {
  if (!myQuoteDetailId) return;
  const qtyInput = document.getElementById('myQuoteDetailAddQty');
  const startInput = document.getElementById('myQuoteDetailAddStart');
  const dueInput = document.getElementById('myQuoteDetailAddDue');
  if (!qtyInput || !startInput || !dueInput) return;

  const assetId = myQuoteDetailSelectedAssetId;
  const quantity = parseInt(qtyInput.value, 10);
  const startDate = startInput.value;
  const dueDate = dueInput.value;

  if (!assetId) { showFieldError('myQuoteDetailAssetSearchInput', 'Search for and select an asset to add.'); return; }
  if (!quantity || quantity < 1) { showFieldError('myQuoteDetailAddQty', 'Enter a quantity of at least 1.'); return; }
  if (!startDate || !dueDate || dueDate < startDate) { showFieldError('myQuoteDetailAddDue', 'Pick a valid start/due date range.'); return; }
  clearFieldError('myQuoteDetailAssetSearchInput');
  clearFieldError('myQuoteDetailAddQty');
  clearFieldError('myQuoteDetailAddDue');

  try {
    const data = await apiRequest(`/quotations/me/${myQuoteDetailId}/items`, {
      method: 'POST',
      body: JSON.stringify({ asset_id: assetId, quantity, start_date: startDate, due_date: dueDate }),
    });
    renderMyQuoteDetail(data);
    clearMyQuoteDetailAsset();
    loadMyQuotationHistory();
    showToast('Line added.');
  } catch (err) {
    alert(err.message);
  }
}

// =============================================================================
// SELF-SERVICE: "My Order" / "My Quotes" TABS (staff.html/customer.html)
// -----------------------------------------------------------------------------
// A small, self-contained tab pair scoped to the Equipment Quotation card
// (separate from ui.js's switchTab()/initSwipeNav(), which are hardcoded
// to admin.html/manager.html's top-level dashboard sections). Mirrors the
// same visual language (active/inactive tab classes, swipe-dot strip,
// touch-swipe between panels) so it feels consistent with the rest of the
// app, just scoped to #myOrderTabSection / #myQuotationHistoryTabSection.
// =============================================================================
const QUOTATION_TAB_ORDER = ['order', 'history'];

export function switchQuotationTab(tab) {
  const orderSection = document.getElementById('myOrderTabSection');
  const historySection = document.getElementById('myQuotationHistoryTabSection');
  const tabOrderBtn = document.getElementById('quotationTabOrder');
  const tabHistoryBtn = document.getElementById('quotationTabHistory');
  if (!orderSection || !historySection) return;

  const activeCls = ['border-blue-500', 'text-slate-50', 'font-semibold'];
  const inactiveCls = ['border-transparent', 'text-slate-500', 'font-medium'];
  const isOrder = tab !== 'history';

  orderSection.classList.toggle('hidden', !isOrder);
  historySection.classList.toggle('hidden', isOrder);
  if (tabOrderBtn) { tabOrderBtn.classList.remove(...activeCls, ...inactiveCls); tabOrderBtn.classList.add(...(isOrder ? activeCls : inactiveCls)); }
  if (tabHistoryBtn) { tabHistoryBtn.classList.remove(...activeCls, ...inactiveCls); tabHistoryBtn.classList.add(...(isOrder ? inactiveCls : activeCls)); }

  updateQuotationSwipeDots(isOrder ? 'order' : 'history');
  resetTabScroll(document.getElementById('quotationSwipeArea'));

  const activeSection = isOrder ? orderSection : historySection;
  activeSection.classList.remove('swipe-content-enter');
  void activeSection.offsetWidth; // force reflow so the CSS animation re-plays
  activeSection.classList.add('swipe-content-enter');
}

function updateQuotationSwipeDots(activeTab) {
  const strip = document.getElementById('quotationSwipeDots');
  if (!strip) return;
  strip.innerHTML = QUOTATION_TAB_ORDER.map(t =>
    `<span class="swipe-dot${t === activeTab ? ' is-active' : ''}"></span>`
  ).join('');
}

// Same conservative "was this really a horizontal swipe" heuristic as
// ui.js's initSwipeNav() (a real horizontal-distance threshold that
// clearly dominates over any vertical movement, ignored while a modal is
// open) -- just scoped to this card's own #quotationSwipeArea instead of
// the whole-page tab strip.
function initQuotationSwipeNav() {
  const swipeArea = document.getElementById('quotationSwipeArea');
  if (!swipeArea) return;
  updateQuotationSwipeDots('order');

  const H_THRESHOLD = 60;
  let startX = 0, startY = 0, tracking = false;

  swipeArea.addEventListener('touchstart', (event) => {
    if (event.touches.length !== 1) { tracking = false; return; }
    if (document.querySelector('.fixed.flex[id$="Modal"]')) { tracking = false; return; }
    startX = event.touches[0].clientX;
    startY = event.touches[0].clientY;
    tracking = true;
  }, { passive: true });

  swipeArea.addEventListener('touchend', (event) => {
    if (!tracking) return;
    tracking = false;
    const dx = event.changedTouches[0].clientX - startX;
    const dy = event.changedTouches[0].clientY - startY;
    if (Math.abs(dx) < H_THRESHOLD || Math.abs(dx) < Math.abs(dy) * 1.3) return;

    const historySection = document.getElementById('myQuotationHistoryTabSection');
    const currentlyOnOrder = historySection ? historySection.classList.contains('hidden') === false : true;
    if (dx < 0 && currentlyOnOrder) switchQuotationTab('history'); // swipe left -> next tab
    else if (dx > 0 && !currentlyOnOrder) switchQuotationTab('order'); // swipe right -> previous tab
  }, { passive: true });
}

// =============================================================================
// ADMIN: GLOBAL VAT SETTING (admin.html's Quotation Settings card)
// =============================================================================
export async function loadVatSetting() {
  const input = document.getElementById('vatPercentInput');
  if (!input) return;
  try {
    const data = await apiRequest('/settings/vat');
    input.value = data.vat_percent;
  } catch (err) {
    // Leave the field blank -- the form's own submit will surface any real error.
  }
}

export async function submitVatSettingsForm(event) {
  event.preventDefault();
  const input = document.getElementById('vatPercentInput');
  const messageEl = document.getElementById('vatSettingsMessage');
  const vatPercent = parseFloat(input.value);
  if (isNaN(vatPercent) || vatPercent < 0 || vatPercent > 100) {
    showFieldError('vatPercentInput', 'Enter a VAT percentage between 0 and 100.');
    return;
  }
  clearFieldError('vatPercentInput');
  try {
    await apiRequest('/settings/vat', { method: 'PUT', body: JSON.stringify({ vat_percent: vatPercent }) });
    if (messageEl) {
      messageEl.textContent = 'VAT updated -- applies to every saved order immediately.';
      messageEl.className = 'text-[12px] font-medium text-emerald-400';
      messageEl.classList.remove('hidden');
    }
    showToast('VAT updated.');
  } catch (err) {
    if (messageEl) {
      messageEl.textContent = err.message;
      messageEl.className = 'text-[12px] font-medium text-rose-400';
      messageEl.classList.remove('hidden');
    }
  }
}

// =============================================================================
// ADMIN/MANAGER: THE "QUOTES" TAB
// -----------------------------------------------------------------------------
// True server-side search + pagination (same pattern as components/
// assets.js's assetsState) -- every keystroke (debounced), page turn, or
// "rows per page" change re-fetches from GET /quotations?search=&limit=&
// offset=.
// =============================================================================
const quotesState = { page: 1, perPage: 5, search: '', total: 0 };
let currentQuoteId = null;          // which Quotation the detail modal is currently showing
let quoteCatalogCache = [];         // asset catalog, fetched once, reused for the "Add item" mini-form in the detail modal
let quoteDetailSelectedAssetId = null; // asset chosen via the search box below, for the "Add item" mini-form

// Lets components/exports.js's Quote Detail export button find out which
// Quotation is currently open, without needing its own copy of this
// module-level state -- same pattern as components/assets.js's
// getCurrentPropsAssetId() / components/custody.js's getCurrentCustodyEntity().
export function getCurrentQuoteId() {
  return currentQuoteId;
}

export async function loadQuotes() {
  const tbody = document.getElementById('quotesTableBody');
  if (!tbody) return; // this page doesn't have the Quotes tab
  try {
    const offset = (quotesState.page - 1) * quotesState.perPage;
    const params = new URLSearchParams({ limit: quotesState.perPage, offset });
    if (quotesState.search.trim()) params.set('search', quotesState.search.trim());
    const result = await apiRequest(`/quotations?${params.toString()}`);
    quotesState.total = result.total;
    renderQuotesTable(result.items);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="7" class="px-5 py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
  }
}

export const setQuotesSearch = debounce((value) => {
  quotesState.search = value;
  quotesState.page = 1;
  loadQuotes();
});

export function setQuotesPerPage(value) {
  quotesState.perPage = parseInt(value, 10) || 10;
  quotesState.page = 1;
  loadQuotes();
}

export function changeQuotesPage(delta) {
  const nextPage = quotesState.page + delta;
  if (nextPage < 1) return;
  quotesState.page = nextPage;
  loadQuotes();
}

function renderQuotesTable(items) {
  const tbody = document.getElementById('quotesTableBody');
  if (!tbody) return;
  document.querySelectorAll('.quote-count').forEach(el => el.textContent = quotesState.total);
  // Same admin-vs-manager gate used by components/assets.js and
  // components/users.js -- admin.html sets data-view="admin",
  // manager.html sets data-view="manager".
  const isAdminView = document.body.dataset.view === 'admin';

  if (items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="px-5 py-6 text-center text-slate-500">No submitted quotes yet.</td></tr>`;
  } else {
    tbody.innerHTML = items.map((q) => {
      const assignedLabel = q.assigned_to
        ? `<span class="text-slate-200">${escapeHtml(q.assigned_to.name)}</span>`
        : (q.assigned_outsider
          ? `<span class="text-slate-200">${escapeHtml(q.assigned_outsider.name)}</span> <span class="text-slate-500">(Ad-Hoc)</span>`
          : `<span class="rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-400">Unassigned</span>`);
      // "Approve" only appears while the quote is still "submitted" (pending
      // review) -- once approved, it's still adjustable by an Admin/Manager
      // (see "View / Adjust" below) right up until it's checked out from
      // the Fulfillment Drawer.
      const approveButton = q.status === 'submitted'
        ? `<button type="button" data-action="approve-quote" data-quote-id="${q.id}"
            class="rounded-md bg-emerald-600/90 px-2.5 py-1.5 text-[12px] font-semibold text-white transition hover:bg-emerald-500">
            Approve
          </button>`
        : '';
      const actionButton = `<button type="button" data-action="open-quote-detail" data-quote-id="${q.id}"
          class="rounded-md border border-border px-2.5 py-1.5 text-[12px] font-medium text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">
          ${q.locked ? 'View' : 'View / Adjust'}
        </button>`;
      // Admin/Super Admin only (element only ever rendered when
      // data-view="admin" -- see admin.html vs manager.html -- a Manager
      // can adjust a quote but never delete one, enforced again server-side
      // by deps.require_super_admin on DELETE /quotations/{id}), and never
      // for an already-fulfilled quote, same lock as every other edit.
      const deleteButton = (isAdminView && !q.locked)
        ? `<button type="button" data-action="delete-quote-row" data-quote-id="${q.id}" data-quote-ref="${escapeHtml(q.reference_number)}" title="Delete quotation"
            class="rounded-md border border-border p-1.5 text-slate-400 transition hover:border-rose-500/40 hover:text-rose-400">
            <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6"/></svg>
          </button>`
        : '';
      return `
      <tr ${rowDetailsTrigger(escapeHtml(q.reference_number), [
        ['Status', quotationStatusBadge(q.status)],
        ['Requester', escapeHtml(q.requester ? `${q.requester.name} (${q.requester.email})` : '—')],
        ['Submitted', escapeHtml(formatTimestamp(q.submitted_at))],
        ['Items', String(q.item_count)],
        ['Total', escapeHtml(formatPrice(q.total))],
        ['Assigned To', q.assigned_to ? escapeHtml(q.assigned_to.name) : (q.assigned_outsider ? `${escapeHtml(q.assigned_outsider.name)} (Ad-Hoc)` : 'Unassigned')],
        ['', [approveButton, actionButton, deleteButton].filter(Boolean).join(' ')],
      ])} class="cursor-pointer transition hover:bg-card2/40 active:bg-card2/60 sm:cursor-default">
        <td class="px-3 py-3 tag-mono font-medium text-slate-100">${escapeHtml(q.reference_number)}</td>
        <td class="px-3 py-3">${quotationStatusBadge(q.status)}</td>
        <td class="hidden px-3 py-3 sm:table-cell">
          <p class="text-slate-200">${escapeHtml(q.requester ? q.requester.name : '—')}</p>
          <p class="text-[11px] text-slate-500">${escapeHtml(q.requester ? q.requester.email : '')}</p>
        </td>
        <td class="hidden px-3 py-3 tag-mono text-slate-300 sm:table-cell">${escapeHtml(formatTimestamp(q.submitted_at))}</td>
        <td class="px-3 py-3">${q.item_count}</td>
        <td class="hidden px-3 py-3 sm:table-cell">${assignedLabel}</td>
        <td class="px-3 py-3 text-right"><span class="hidden items-center justify-end gap-2 sm:inline-flex">${approveButton}${actionButton}${deleteButton}</span></td>
      </tr>`;
    }).join('');
  }

  renderServerPaginationBar('quotes', quotesState);
}

// -----------------------------------------------------------------------------
// Quote Detail modal -- view/edit items, notes, and assignment.
// -----------------------------------------------------------------------------
async function ensureQuoteCatalogLoaded() {
  if (quoteCatalogCache.length) return;
  try {
    const data = await apiRequest('/assets/catalog');
    quoteCatalogCache = data.items;
  } catch (err) {
    quoteCatalogCache = [];
  }
}

export async function openQuoteDetail(quotationId) {
  currentQuoteId = quotationId;
  clearQuoteDetailAsset(); // reset any leftover selection from a previously-opened quote
  await ensureQuoteCatalogLoaded();
  await refreshQuoteDetail();
  openModal('quoteDetailModal');
}

async function refreshQuoteDetail() {
  if (!currentQuoteId) return;
  const titleEl = document.getElementById('quoteDetailRef');
  try {
    const data = await apiRequest(`/quotations/${currentQuoteId}`);
    renderQuoteDetail(data);
  } catch (err) {
    if (titleEl) titleEl.textContent = 'Quote not found';
    alert(err.message);
    closeModal('quoteDetailModal');
  }
}

function renderQuoteDetail(data) {
  const titleEl = document.getElementById('quoteDetailRef');
  const metaEl = document.getElementById('quoteDetailMeta');
  const statusEl = document.getElementById('quoteDetailStatusBadge');
  const approveBtn = document.getElementById('quoteDetailApproveBtn');
  const lockedNotice = document.getElementById('quoteDetailLockedNotice');
  const addItemForm = document.getElementById('quoteDetailAddItemForm');
  const addOutsourcedItemForm = document.getElementById('quoteDetailAddOutsourcedItemForm');
  const assignForm = document.getElementById('quoteDetailAssignForm');
  const notesSaveBtn = document.getElementById('quoteDetailSaveNotesBtn');
  const itemsBody = document.getElementById('quoteDetailItemsBody');
  const notesInput = document.getElementById('quoteDetailNotes');
  const assignCurrentEl = document.getElementById('quoteDetailAssignedTo');
  const discountInput = document.getElementById('quoteDetailDiscountInput');
  const discountSaveBtn = document.getElementById('quoteDetailSaveDiscountBtn');

  // `data.locked` is only ever true once a quote is "fulfilled" (see
  // backend/services/quotation_service.py's _serialize_quotation()) --
  // an approved quote stays fully editable here; only the REQUESTER/
  // assignee's own self-service side is cut off at "approved" (see that
  // module's _get_own_editable_quotation()).
  const locked = !!data.locked;
  // A customer/staff account's own self-submitted request -- the backend
  // (assign_quotation() in backend/services/quotation_service.py) refuses
  // to reassign these no matter what the UI sends, but hiding the
  // controls here means an Admin/Manager never sees an action that would
  // just come back as an error.
  const personal = !!data.is_personal_request;
  const assignLocked = locked || personal;

  if (titleEl) titleEl.textContent = data.reference_number || `Quotation #${data.id}`;
  if (metaEl) {
    metaEl.textContent = `Requested by ${data.requester ? `${data.requester.name} (${data.requester.email})` : '—'} · Submitted ${formatTimestamp(data.submitted_at)}`;
  }
  if (statusEl) statusEl.innerHTML = quotationStatusBadge(data.status);
  // "Approve" only while the quote is still "submitted" -- see
  // quotationStatusBadge()/backend approve_quotation() for the lifecycle.
  if (approveBtn) approveBtn.classList.toggle('hidden', data.status !== 'submitted');
  // "Delete" -- Admin-only (deleteQuoteBtn only exists on admin.html, see
  // the `if (!deleteBtn) return` guard nowhere needed here since the
  // element itself is simply absent on manager.html) and refused once
  // fulfilled, same `locked` rule as every other edit above -- see
  // backend's delete_quotation()/_ensure_admin_editable().
  const deleteBtn = document.getElementById('quoteDetailDeleteBtn');
  if (deleteBtn) deleteBtn.classList.toggle('hidden', locked);
  if (lockedNotice) {
    lockedNotice.classList.toggle('hidden', !locked);
    if (locked) {
      lockedNotice.textContent = `Fulfilled ${formatTimestamp(data.fulfilled_at)} -- this quote is now history and can no longer be edited.`;
    }
  }
  // Item edits, "Add another asset", and notes stay open through
  // "submitted" AND "approved" -- an Admin/Manager can keep adjusting an
  // approved quote right up until the Fulfillment Drawer checks it out;
  // only "fulfilled" (locked) shuts them off (see _ensure_admin_editable()
  // in backend/services/quotation_service.py). Assignment has its OWN,
  // stricter gate (assignLocked): also disabled whenever this is the
  // requester's own personal request, regardless of status.
  if (addItemForm) addItemForm.classList.toggle('hidden', locked);
  if (addOutsourcedItemForm) addOutsourcedItemForm.classList.toggle('hidden', locked);
  if (assignForm) assignForm.classList.toggle('hidden', assignLocked);
  if (notesInput) notesInput.disabled = locked;
  if (notesSaveBtn) notesSaveBtn.classList.toggle('hidden', locked);
  // Discount -- Admin/Manager-only, editable under the EXACT same lock as
  // notes/items above: right up until the quote is "fulfilled" (see
  // backend's _ensure_admin_editable()), not its own separate rule.
  if (discountInput) {
    discountInput.disabled = locked;
    if (document.activeElement !== discountInput) discountInput.value = data.discount_percent || 0;
  }
  if (discountSaveBtn) discountSaveBtn.classList.toggle('hidden', locked);

  if (notesInput && document.activeElement !== notesInput) notesInput.value = data.notes || '';
  if (assignCurrentEl) {
    const unassignButton = assignLocked ? '' : `<button type="button" data-action="unassign-quote" class="ml-2 text-[11px] font-medium text-rose-400 underline-offset-2 hover:underline">Unassign</button>`;
    const personalNote = personal ? ` <span class="text-slate-500">(personal request -- can't be reassigned)</span>` : '';
    if (data.assigned_to) {
      assignCurrentEl.innerHTML = `<span class="text-slate-200">${escapeHtml(data.assigned_to.name)}</span> <span class="text-slate-500">(${escapeHtml(data.assigned_to.email)})</span>${unassignButton}${personalNote}`;
    } else if (data.assigned_outsider) {
      const companyLabel = data.assigned_outsider.company ? ` · ${escapeHtml(data.assigned_outsider.company)}` : '';
      assignCurrentEl.innerHTML = `<span class="text-slate-200">${escapeHtml(data.assigned_outsider.name)}</span> <span class="text-slate-500">(Ad-Hoc${companyLabel})</span>${unassignButton}${personalNote}`;
    } else {
      assignCurrentEl.innerHTML = `<span class="rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-400">Unassigned</span>${personalNote}`;
    }
  }
  const assignAdhocToggle = document.querySelector('[data-action="toggle-quote-assign-adhoc"]');
  const assignAdhocForm = document.getElementById('quoteAssignAdhocForm');
  if (assignAdhocToggle) assignAdhocToggle.classList.toggle('hidden', assignLocked);
  if (assignAdhocForm && assignLocked) assignAdhocForm.classList.add('hidden');

  const cardsWrap = document.getElementById('quoteDetailItemsCards');
  if (itemsBody) {
    const qtyCell = (li, extraClass) => (locked || li.is_outsourced)
      ? `<span class="tag-mono text-slate-300">${li.quantity}</span>`
      : `<input type="number" min="1" value="${li.quantity}" data-action="update-admin-quote-qty" data-item-id="${li.item_id}"
          class="${extraClass} rounded-md border border-border bg-card2 px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-blue-500/60" />`;
    // Outsourced lines use a SEPARATE remove endpoint/table
    // (DELETE /quotations/{id}/outsourced-items/{item_id}) from a
    // regular catalog line (DELETE .../items/{item_id}) -- see
    // models.py's QuotationOutsourcedItem docstring.
    const removeCell = (li) => locked ? '' : (li.is_outsourced
      ? `<button type="button" data-action="remove-quote-outsourced-item" data-outsourced-item-id="${li.outsourced_item_id}" title="Remove outsourced line"
            class="shrink-0 rounded-md border border-border p-1.5 text-slate-400 transition hover:border-rose-500/40 hover:text-rose-400">
            <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6"/></svg>
          </button>`
      : `<button type="button" data-action="remove-admin-quote-item" data-item-id="${li.item_id}" title="Remove line"
            class="shrink-0 rounded-md border border-border p-1.5 text-slate-400 transition hover:border-rose-500/40 hover:text-rose-400">
            <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6"/></svg>
          </button>`);
    // "Outsourced" -- unlike the neutral "Not in Inventory" wording shown
    // to the requester in My Quote Detail (see renderMyQuoteDetail), the
    // Manager/Admin view can name it plainly, and can also show WHERE
    // it's being sourced from (li.sourced_from -- only present on this
    // payload because get_quotation_detail()/admin_* routes ask
    // _serialize_quotation() for it via `reveal_sourcing=True`; the
    // requester's own payload never has this key at all).
    const outsourcedBadge = (li) => li.is_outsourced
      ? `<span class="ml-1.5 inline-flex max-w-full items-center break-words rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-medium text-amber-400 ring-1 ring-amber-500/30">Outsourced${li.sourced_from ? ` · ${escapeHtml(li.sourced_from)}` : ''}</span>`
      : '';

    // ---- Desktop table rows (sm and up) ----
    itemsBody.innerHTML = data.items.length === 0
      ? `<tr><td colspan="7" class="px-3 py-5 text-center text-slate-500">No items on this quote.</td></tr>`
      : data.items.map((li) => `
        <tr data-quote-item-row="${li.item_id || `out-${li.outsourced_item_id}`}">
          <td class="px-3 py-2.5">
            <p class="font-medium text-slate-100">${escapeHtml(li.asset_name)}${outsourcedBadge(li)}</p>
            <p class="text-[11px] text-slate-500">${li.category ? escapeHtml(li.category) : '—'}</p>
          </td>
          <td class="px-3 py-2.5">${qtyCell(li, 'w-16')}</td>
          <td class="px-3 py-2.5 tag-mono">${li.start_date}</td>
          <td class="px-3 py-2.5 tag-mono">${li.due_date}</td>
          <td class="px-3 py-2.5">${li.days}</td>
          <td class="px-3 py-2.5 tag-mono">${formatPrice(li.line_total)}</td>
          <td class="px-3 py-2.5 text-right">${removeCell(li)}</td>
        </tr>`).join('');

    // ---- Mobile cards (below sm) -- same reasoning as My Quote Detail's
    // #myQuoteDetailCards on staff.html/customer.html: Asset/Qty/Line
    // Total/Remove don't all fit in one row inside a phone-width modal
    // without clipping the Remove button or forcing horizontal scroll. ----
    if (cardsWrap) {
      cardsWrap.innerHTML = data.items.length === 0
        ? `<p class="px-4 py-6 text-center text-[13px] text-slate-500">No items on this quote.</p>`
        : data.items.map((li) => `
          <div class="space-y-2.5 p-4" data-quote-item-card="${li.item_id || `out-${li.outsourced_item_id}`}">
            <div class="flex items-start justify-between gap-2">
              <div class="min-w-0">
                <p class="truncate font-medium text-slate-100">${escapeHtml(li.asset_name)}</p>
                ${li.is_outsourced ? `<p class="mt-0.5">${outsourcedBadge(li)}</p>` : ''}
                <p class="text-[11px] text-slate-500">${li.category ? escapeHtml(li.category) : '—'} · ${li.start_date} → ${li.due_date} (${li.days}d)</p>
              </div>
              ${removeCell(li)}
            </div>
            <div class="flex items-center justify-between gap-3">
              <label class="flex items-center gap-2 text-[12px] text-slate-500">
                Qty ${qtyCell(li, 'w-16')}
              </label>
              <p class="tag-mono text-[13px] text-slate-100">${formatPrice(li.line_total)}</p>
            </div>
          </div>`).join('');
    }
  }

  const subtotalEl = document.getElementById('quoteDetailSubtotal');
  const vatEl = document.getElementById('quoteDetailVat');
  const totalEl = document.getElementById('quoteDetailTotal');
  const discountAmountEl = document.getElementById('quoteDetailDiscountAmount');
  if (subtotalEl) subtotalEl.textContent = formatPrice(data.subtotal);
  if (vatEl) vatEl.textContent = `VAT (${data.vat_percent}%): ${formatPrice(data.vat_amount)}`;
  if (totalEl) totalEl.textContent = formatPrice(data.total);
  if (discountAmountEl) {
    if (data.discount_percent) {
      discountAmountEl.textContent = `Discount (${data.discount_percent}%): -${formatPrice(data.discount_amount)}`;
      discountAmountEl.classList.remove('hidden');
    } else {
      discountAmountEl.classList.add('hidden');
    }
  }
}

// ---- Approve: submitted -> approved. Locks the quote for editing and
// turns its badge green (see quotationStatusBadge()). ----
export async function approveQuote(quotationId) {
  try {
    const data = await apiRequest(`/quotations/${quotationId}/approve`, { method: 'POST' });
    if (currentQuoteId === quotationId) renderQuoteDetail(data);
    loadQuotes();
    showToast(`Quotation ${data.reference_number} approved -- Ready for Pickup.`);
  } catch (err) {
    alert(err.message);
  }
}

// Admin-only: permanently deletes a Quotation directly from the Quotes
// table row (without opening the detail modal first). Same endpoint/rules
// as deleteQuoteDetail() below.
export async function deleteQuoteRow(quotationId, referenceLabel) {
  if (!confirm(`Permanently delete ${referenceLabel || 'this quotation'}? This cannot be undone.`)) return;
  try {
    await apiRequest(`/quotations/${quotationId}`, { method: 'DELETE' });
    loadQuotes();
    showToast(`${referenceLabel || 'Quotation'} deleted.`);
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
}

// Admin-only: permanently deletes the Quotation currently open in the
// detail modal. Gated server-side by deps.require_super_admin (Admin/Super
// Admin, NOT Manager -- see api/quotations.py's DELETE /quotations/{id})
// and refused once fulfilled -- the deleteQuoteDetail button itself is
// already hidden past that point by renderQuoteDetail() above, but the
// backend is the real enforcement either way.
export async function deleteQuoteDetail() {
  if (!currentQuoteId) return;
  const refEl = document.getElementById('quoteDetailRef');
  const label = refEl ? refEl.textContent : 'this quotation';
  if (!confirm(`Permanently delete ${label}? This cannot be undone.`)) return;
  try {
    await apiRequest(`/quotations/${currentQuoteId}`, { method: 'DELETE' });
    closeModal('quoteDetailModal');
    loadQuotes();
    showToast(`${label} deleted.`);
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
}

// ---- Add another asset: live search box (replaces the old long
// dropdown -- much faster to use once the catalog has more than a
// handful of assets, and doesn't force a huge <select> onto a phone). ----
export const searchQuoteDetailAssets = debounce((query) => {
  const resultsEl = document.getElementById('quoteDetailAssetResults');
  if (!resultsEl) return;
  const term = query.trim().toLowerCase();
  if (!term) { resultsEl.innerHTML = ''; resultsEl.classList.add('hidden'); return; }
  const matches = quoteCatalogCache.filter((a) =>
    a.name.toLowerCase().includes(term) || (a.category || '').toLowerCase().includes(term)
  ).slice(0, 8);
  resultsEl.innerHTML = matches.length
    ? matches.map((a) => `
        <button type="button" data-action="select-quote-detail-asset" data-asset-id="${a.id}" data-asset-name="${escapeHtml(a.name)}"
          class="block w-full rounded-md px-3 py-2 text-left text-[12px] transition hover:bg-card2">
          <span class="font-medium text-slate-100">${escapeHtml(a.name)}</span>
          <span class="text-slate-500"> · ${a.category ? escapeHtml(a.category) : '—'}${a.price !== null && a.price !== undefined ? ` · ${escapeHtml(formatPrice(a.price))}/day` : ''}</span>
        </button>`).join('')
    : `<p class="px-3 py-2 text-[12px] text-slate-500">No matching assets.</p>`;
  resultsEl.classList.remove('hidden');
});

export function selectQuoteDetailAsset(assetId, assetName) {
  quoteDetailSelectedAssetId = assetId;
  const labelEl = document.getElementById('quoteDetailSelectedAssetLabel');
  const wrapEl = document.getElementById('quoteDetailSelectedAsset');
  const searchInput = document.getElementById('quoteDetailAssetSearchInput');
  const resultsEl = document.getElementById('quoteDetailAssetResults');
  if (labelEl) labelEl.textContent = assetName;
  if (wrapEl) wrapEl.classList.remove('hidden');
  if (searchInput) searchInput.value = '';
  if (resultsEl) { resultsEl.innerHTML = ''; resultsEl.classList.add('hidden'); }
  clearFieldError('quoteDetailAssetSearchInput');
}

export function clearQuoteDetailAsset() {
  quoteDetailSelectedAssetId = null;
  const wrapEl = document.getElementById('quoteDetailSelectedAsset');
  const searchInput = document.getElementById('quoteDetailAssetSearchInput');
  const resultsEl = document.getElementById('quoteDetailAssetResults');
  if (wrapEl) wrapEl.classList.add('hidden');
  if (searchInput) searchInput.value = '';
  if (resultsEl) { resultsEl.innerHTML = ''; resultsEl.classList.add('hidden'); }
}

export async function addQuoteDetailItem() {
  if (!currentQuoteId) return;
  const qtyInput = document.getElementById('quoteDetailAddQty');
  const startInput = document.getElementById('quoteDetailAddStart');
  const dueInput = document.getElementById('quoteDetailAddDue');
  if (!qtyInput || !startInput || !dueInput) return;

  const assetId = quoteDetailSelectedAssetId;
  const quantity = parseInt(qtyInput.value, 10);
  const startDate = startInput.value;
  const dueDate = dueInput.value;

  if (!assetId) { showFieldError('quoteDetailAssetSearchInput', 'Search for and select an asset to add.'); return; }
  if (!quantity || quantity < 1) { showFieldError('quoteDetailAddQty', 'Enter a quantity of at least 1.'); return; }
  if (!startDate || !dueDate || dueDate < startDate) { showFieldError('quoteDetailAddDue', 'Pick a valid start/due date range.'); return; }
  clearFieldError('quoteDetailAssetSearchInput');
  clearFieldError('quoteDetailAddQty');
  clearFieldError('quoteDetailAddDue');

  try {
    const data = await apiRequest(`/quotations/${currentQuoteId}/items`, {
      method: 'POST',
      body: JSON.stringify({ asset_id: assetId, quantity, start_date: startDate, due_date: dueDate }),
    });
    renderQuoteDetail(data);
    clearQuoteDetailAsset();
    loadQuotes();
    showToast('Line added.');
  } catch (err) {
    alert(err.message);
  }
}

// ---- Add outsourced (not currently in inventory) item: a Manager/Admin-only
// mini-form, separate from "Add another asset" above -- see
// models.py's QuotationOutsourcedItem docstring and
// POST /quotations/{id}/outsourced-items. ----
export async function addQuoteOutsourcedItem() {
  if (!currentQuoteId) return;
  const nameInput = document.getElementById('quoteDetailOutsourcedName');
  const sourcedFromInput = document.getElementById('quoteDetailOutsourcedSourcedFrom');
  const descInput = document.getElementById('quoteDetailOutsourcedDescription');
  const priceInput = document.getElementById('quoteDetailOutsourcedPrice');
  const qtyInput = document.getElementById('quoteDetailOutsourcedQty');
  const startInput = document.getElementById('quoteDetailOutsourcedStart');
  const dueInput = document.getElementById('quoteDetailOutsourcedDue');
  if (!nameInput || !priceInput || !qtyInput || !startInput || !dueInput) return;

  const name = nameInput.value.trim();
  const sourcedFrom = sourcedFromInput ? sourcedFromInput.value.trim() : '';
  const description = descInput ? descInput.value.trim() : '';
  const unitPrice = parseFloat(priceInput.value);
  const quantity = parseInt(qtyInput.value, 10);
  const startDate = startInput.value;
  const dueDate = dueInput.value;

  if (!name) { showFieldError('quoteDetailOutsourcedName', 'Enter a name for the item.'); return; }
  if (isNaN(unitPrice) || unitPrice < 0) { showFieldError('quoteDetailOutsourcedPrice', 'Enter a valid price per day.'); return; }
  if (!quantity || quantity < 1) { showFieldError('quoteDetailOutsourcedQty', 'Enter a quantity of at least 1.'); return; }
  if (!startDate || !dueDate || dueDate < startDate) { showFieldError('quoteDetailOutsourcedDue', 'Pick a valid start/due date range.'); return; }
  clearFieldError('quoteDetailOutsourcedName');
  clearFieldError('quoteDetailOutsourcedPrice');
  clearFieldError('quoteDetailOutsourcedQty');
  clearFieldError('quoteDetailOutsourcedDue');

  try {
    const data = await apiRequest(`/quotations/${currentQuoteId}/outsourced-items`, {
      method: 'POST',
      body: JSON.stringify({
        name, description: description || null, unit_price: unitPrice,
        quantity, sourced_from: sourcedFrom || null, start_date: startDate, due_date: dueDate,
      }),
    });
    renderQuoteDetail(data);
    nameInput.value = '';
    if (sourcedFromInput) sourcedFromInput.value = '';
    if (descInput) descInput.value = '';
    priceInput.value = '';
    qtyInput.value = '1';
    loadQuotes();
    showToast('Outsourced item added.');
  } catch (err) {
    alert(err.message);
  }
}

export async function removeQuoteOutsourcedItem(itemId) {
  if (!currentQuoteId) return;
  try {
    const data = await apiRequest(`/quotations/${currentQuoteId}/outsourced-items/${itemId}`, { method: 'DELETE' });
    renderQuoteDetail(data);
    loadQuotes();
    showToast('Outsourced item removed.');
  } catch (err) {
    alert(err.message);
  }
}

export async function updateAdminQuoteItemQuantity(itemId, quantity) {
  if (!currentQuoteId) return;
  const qty = parseInt(quantity, 10);
  if (!qty || qty < 1) return;
  try {
    const data = await apiRequest(`/quotations/${currentQuoteId}/items/${itemId}`, {
      method: 'PUT',
      body: JSON.stringify({ quantity: qty }),
    });
    renderQuoteDetail(data);
    loadQuotes();
  } catch (err) {
    alert(err.message);
    refreshQuoteDetail();
  }
}

export async function removeAdminQuoteItem(itemId) {
  if (!currentQuoteId) return;
  try {
    const data = await apiRequest(`/quotations/${currentQuoteId}/items/${itemId}`, { method: 'DELETE' });
    renderQuoteDetail(data);
    loadQuotes();
    showToast('Line removed.');
  } catch (err) {
    alert(err.message);
  }
}

export async function saveQuoteNotes() {
  if (!currentQuoteId) return;
  const notesInput = document.getElementById('quoteDetailNotes');
  try {
    await apiRequest(`/quotations/${currentQuoteId}`, {
      method: 'PUT',
      body: JSON.stringify({ notes: notesInput ? notesInput.value : null }),
    });
    showToast('Notes saved.');
  } catch (err) {
    alert(err.message);
  }
}

// Admin/Manager-only: sets the discount percentage (0-100) on this
// specific quote. Own endpoint (not folded into saveQuoteNotes()'s
// PUT /quotations/{id}) so saving notes can never accidentally reset an
// already-set discount back to 0 -- see backend/api/quotations.py's
// PUT /quotations/{id}/discount. Same _ensure_admin_editable() lock as
// notes/items -- editable right up until the quote is fulfilled.
export async function saveQuoteDiscount() {
  if (!currentQuoteId) return;
  const discountInput = document.getElementById('quoteDetailDiscountInput');
  const raw = discountInput ? discountInput.value.trim() : '';
  const discountPercent = raw === '' ? 0 : Number(raw);
  if (Number.isNaN(discountPercent) || discountPercent < 0 || discountPercent > 100) {
    alert('Discount must be a number between 0 and 100.');
    return;
  }
  try {
    const data = await apiRequest(`/quotations/${currentQuoteId}/discount`, {
      method: 'PUT',
      body: JSON.stringify({ discount_percent: discountPercent }),
    });
    renderQuoteDetail(data);
    loadQuotes();
    showToast('Discount saved.');
  } catch (err) {
    alert(err.message);
  }
}

// ---- Assign to user: small debounced search + click-to-assign list ----
export const searchAssignUsers = debounce(async (query) => {
  const resultsEl = document.getElementById('quoteAssignResults');
  if (!resultsEl) return;
  const term = query.trim();
  if (!term) { resultsEl.innerHTML = ''; resultsEl.classList.add('hidden'); return; }
  try {
    const params = new URLSearchParams({ search: term, limit: 6 });
    const result = await apiRequest(`/users?${params.toString()}`);
    const users = result.items || [];
    resultsEl.innerHTML = users.length
      ? users.map(u => `
          <button type="button" data-action="assign-quote-to-user" data-user-id="${u.id}"
            class="block w-full rounded-md px-3 py-2 text-left text-[12px] transition hover:bg-card2">
            <span class="font-medium text-slate-100">${escapeHtml(u.name)}</span>
            <span class="text-slate-500"> · ${escapeHtml(u.email)}</span>
          </button>`).join('')
      : `<p class="px-3 py-2 text-[12px] text-slate-500">No matching users.</p>`;
    resultsEl.classList.remove('hidden');
  } catch (err) {
    resultsEl.innerHTML = `<p class="px-3 py-2 text-[12px] text-rose-400">${escapeHtml(err.message)}</p>`;
    resultsEl.classList.remove('hidden');
  }
});

export async function assignQuoteToUser(userId) {
  if (!currentQuoteId) return;
  try {
    const data = await apiRequest(`/quotations/${currentQuoteId}/assign`, {
      method: 'POST',
      body: JSON.stringify(userId ? { assignee_type: 'user', user_id: userId } : {}),
    });
    renderQuoteDetail(data);
    loadQuotes();
    const resultsEl = document.getElementById('quoteAssignResults');
    const searchInput = document.getElementById('quoteAssignSearchInput');
    if (resultsEl) { resultsEl.innerHTML = ''; resultsEl.classList.add('hidden'); }
    if (searchInput) searchInput.value = '';
    showToast('Quotation assigned.');
  } catch (err) {
    alert(err.message);
  }
}

export async function unassignQuote() {
  await assignQuoteToUser(null);
}

export function toggleQuoteAssignAdhocForm() {
  const form = document.getElementById('quoteAssignAdhocForm');
  if (form) form.classList.toggle('hidden');
}

// Same "new profile fields only show when '+ Create New Unlinked Profile'
// is selected" pattern as ui.js's toggleAdhocExisting(), just scoped to the
// Ad-Hoc mini-form under "Assign to an Ad-Hoc Individual instead" on the
// Quote Detail screen (#quoteAssignAdhocExistingSelect / #quoteAssignAdhocNewFields).
export function toggleQuoteAssignAdhocExisting() {
  const select = document.getElementById('quoteAssignAdhocExistingSelect');
  const newFields = document.getElementById('quoteAssignAdhocNewFields');
  if (!select || !newFields) return;
  newFields.classList.toggle('hidden', select.value !== 'new');
}

export async function submitQuoteAssignAdhoc() {
  if (!currentQuoteId) return;
  const existingSelect = document.getElementById('quoteAssignAdhocExistingSelect');
  const existingId = existingSelect ? existingSelect.value : 'new';
  const nameInput = document.getElementById('quoteAssignAdhocName');
  const companyInput = document.getElementById('quoteAssignAdhocCompany');
  const emailInput = document.getElementById('quoteAssignAdhocEmail');
  const phoneInput = document.getElementById('quoteAssignAdhocPhone');

  // "+ Create New Unlinked Profile" (value="new", the select's only
  // option before components/outsiders.js's loadOutsiders() populates it
  // with real profiles) still creates a brand new Outsider row from the
  // name/company/email/phone fields below. Any other option is an
  // existing outsider's real id -- selecting one reuses that profile
  // (backend/schemas/quotations.py's QuotationAssignRequest.outsider_id)
  // instead of fabricating a duplicate every time the same person is
  // assigned a quote.
  const payload = { assignee_type: 'outsider' };
  if (existingId && existingId !== 'new') {
    payload.outsider_id = parseInt(existingId, 10);
  } else {
    const name = nameInput ? nameInput.value.trim() : '';
    const email = emailInput ? emailInput.value.trim() : '';
    const phone = phoneInput ? phoneInput.value.trim() : '';
    if (!name || (!email && !phone)) {
      alert('Name and at least one of email/phone are required for an Ad-Hoc individual.');
      return;
    }
    payload.outsider_name = name;
    payload.outsider_email = email || null;
    payload.outsider_phone = phone || null;
    payload.outsider_company = companyInput ? companyInput.value.trim() : '';
  }

  try {
    const data = await apiRequest(`/quotations/${currentQuoteId}/assign`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    renderQuoteDetail(data);
    loadQuotes();
    if (nameInput) nameInput.value = '';
    if (companyInput) companyInput.value = '';
    if (contactInput) contactInput.value = '';
    if (existingSelect) { existingSelect.value = 'new'; toggleQuoteAssignAdhocExisting(); }
    const form = document.getElementById('quoteAssignAdhocForm');
    if (form) form.classList.add('hidden');
    showToast('Assigned to Ad-Hoc individual.');
  } catch (err) {
    alert(err.message);
  }
}

// -----------------------------------------------------------------------------
// ADMIN/MANAGER: CREATE A QUOTE DIRECTLY (e.g. building one on a user's
// behalf over the phone) -- separate from a staff/customer's own
// self-service cart. Starts empty and already-submitted; the Quote
// Detail modal opens right after so items/notes can be filled in
// immediately. Assignment uses the same Staff Member / Linked Customer
// Account / Ad-Hoc Individual route-select as the Issue/Dispatch drawer
// (see ui.js's toggleQuoteRoute()) instead of a generic user search, plus
// an "Unassigned" option (Dispatch never has one -- a checkout always
// needs a recipient -- but a Quotation can start unassigned and be
// assigned later from the Quote Detail screen).
// -----------------------------------------------------------------------------
export function openCreateQuoteModal() {
  const form = document.getElementById('createQuoteForm');
  const routeSelect = document.getElementById('quoteRouteSelect');
  if (form) form.reset();
  if (routeSelect) { routeSelect.value = 'unassigned'; toggleQuoteRoute(); }
  toggleQuoteAdhocExisting();
  openModal('createQuoteModal');
}

// Mirrors ui.js's toggleRoute() (the Issue/Dispatch drawer's own
// staffField/customerField/adhocField show/hide), just scoped to this
// modal's own #quoteStaffField/#quoteCustomerField/#quoteAdhocField ids
// (and the extra "unassigned" option, which shows/hides all three) so it
// never collides with the dispatch drawer's identically-shaped fields on
// the same page.
export function toggleQuoteRoute() {
  const val = document.getElementById('quoteRouteSelect').value;
  document.getElementById('quoteStaffField').classList.toggle('hidden', val !== 'staff');
  document.getElementById('quoteCustomerField').classList.toggle('hidden', val !== 'customer');
  document.getElementById('quoteAdhocField').classList.toggle('hidden', val !== 'adhoc');
  const hint = document.getElementById('createQuoteUnassignedHint');
  if (hint) hint.classList.toggle('hidden', val !== 'unassigned');
}

// Same "new profile fields only show when '+ Create New Unlinked Profile'
// is selected" pattern as ui.js's toggleAdhocExisting(), just scoped to the
// Create Quote modal's own Ad-Hoc route fields
// (#quoteAdhocExistingSelect / #quoteAdhocNewFields) so it never collides
// with the identically-shaped dispatch-drawer or quote-assign fields on
// the same page.
export function toggleQuoteAdhocExisting() {
  const select = document.getElementById('quoteAdhocExistingSelect');
  const newFields = document.getElementById('quoteAdhocNewFields');
  if (!select || !newFields) return;
  newFields.classList.toggle('hidden', select.value !== 'new');
}

export async function submitCreateQuote(button) {
  const routeVal = document.getElementById('quoteRouteSelect').value;
  const payload = {};

  if (routeVal === 'staff') {
    const staffId = document.getElementById('quoteStaffSelect').value;
    if (!staffId) { alert('Select a staff member.'); return; }
    payload.assignee_type = 'user';
    payload.assigned_user_id = parseInt(staffId, 10);
  } else if (routeVal === 'customer') {
    const customerId = document.getElementById('quoteCustomerSelect').value;
    if (!customerId) { alert('Select a linked customer account.'); return; }
    payload.assignee_type = 'user';
    payload.assigned_user_id = parseInt(customerId, 10);
  } else if (routeVal === 'adhoc') {
    // Same "existing profile vs. brand new" choice as
    // submitDispatchForm()/submitQuoteAssignAdhoc() above -- "+ Create New
    // Unlinked Profile" (value="new") creates a new Outsider row from the
    // name/company/email/phone fields; any other option is an existing
    // outsider's real id and reuses that profile
    // (backend/schemas/quotations.py's QuotationCreateRequest.outsider_id).
    const existingSelect = document.getElementById('quoteAdhocExistingSelect');
    const existingId = existingSelect ? existingSelect.value : 'new';
    payload.assignee_type = 'outsider';
    if (existingId && existingId !== 'new') {
      payload.outsider_id = parseInt(existingId, 10);
    } else {
      const name = document.getElementById('quoteAdhocName').value.trim();
      const email = document.getElementById('quoteAdhocEmail').value.trim();
      const phoneEl = document.getElementById('quoteAdhocPhone');
      const phone = phoneEl ? phoneEl.value.trim() : '';
      if (!name || (!email && !phone)) { alert('Name and at least one of email/phone are required for an Ad-Hoc individual.'); return; }
      payload.outsider_name = name;
      payload.outsider_email = email || null;
      payload.outsider_phone = phone || null;
      payload.outsider_company = document.getElementById('quoteAdhocCompany').value.trim();
    }
  }
  // routeVal === 'unassigned' -- payload stays empty, quote starts unassigned.

  const original = button ? button.textContent : null;
  if (button) { button.disabled = true; button.textContent = 'Creating…'; }
  try {
    const data = await apiRequest('/quotations', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    closeModal('createQuoteModal');
    loadQuotes();
    await openQuoteDetail(data.id);
    showToast(`Quotation ${data.reference_number} created -- add items below.`);
  } catch (err) {
    alert(err.message);
  } finally {
    if (button) { button.disabled = false; button.textContent = original; }
  }
}

// =============================================================================
// ADMIN/MANAGER: FULFILLMENT DRAWER
// -----------------------------------------------------------------------------
// Every "approved" Quotation (oldest first), selectable via checkboxes
// exactly like components/custody.js's bulk-return selection, so a Manager/
// Admin can process several ready-for-pickup quotes in one sitting. The
// actual physical checkout -- and the ONLY moment stock is evaluated/
// deducted anywhere in this whole workflow -- happens per-quote against
// POST /quotations/{id}/checkout; selecting several quotes just calls it
// once per quote in sequence, same pattern as bulkProcessReturns().
// =============================================================================
let fulfillmentQueueCache = [];

export async function openFulfillmentDrawer() {
  openModal('fulfillmentDrawerModal');
  await loadFulfillmentQueue();
}

export async function loadFulfillmentQueue() {
  const body = document.getElementById('fulfillmentQueueBody');
  if (!body) return;
  body.innerHTML = `<p class="px-4 py-6 text-center text-[13px] text-slate-500">Loading approved quotes…</p>`;
  try {
    const data = await apiRequest('/quotations/fulfillment-queue');
    fulfillmentQueueCache = data.items;
    renderFulfillmentQueue();
  } catch (err) {
    body.innerHTML = `<p class="px-4 py-6 text-center text-[13px] text-rose-400">${escapeHtml(err.message)}</p>`;
  }
}

// One allocation row inside a shortfall line's "Add another source" list --
// Qty/Sourced From/Price-per-day, plus a Remove button (hidden when it's
// the only row -- see updateShortfallRowRemoveButtons() below). Used both
// for the single row every shortfall line starts with (pre-filled with the
// FULL shortfall quantity, so the common "just outsource it all to one
// company" case needs zero extra clicks) and for rows appended later by
// addShortfallAllocationRow().
function shortfallAllocationRowHtml(quantity, defaultPrice) {
  return `
    <div class="shortfall-alloc-row grid grid-cols-[3.5rem_1fr_1fr_auto] items-center gap-1.5">
      <input type="number" min="1" value="${quantity === '' ? '' : quantity}" placeholder="Qty" data-shortfall-qty
        class="w-full rounded border border-border bg-card2 px-2 py-1 text-[12px] text-slate-200" />
      <input type="text" placeholder="Sourced from (optional)" data-shortfall-source
        class="w-full rounded border border-border bg-card2 px-2 py-1 text-[12px] text-slate-200 placeholder:text-slate-600" />
      <input type="number" min="0" step="0.01" placeholder="Price/day (default ${formatPrice(defaultPrice)})" data-shortfall-price
        class="w-full rounded border border-border bg-card2 px-2 py-1 text-[12px] text-slate-200 placeholder:text-slate-600" />
      <button type="button" data-action="remove-shortfall-row" class="rounded p-1 text-slate-500 transition hover:text-rose-400" title="Remove this source">
        <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
      </button>
    </div>`;
}

// Only shows the per-row Remove button once there's more than one
// allocation row -- a lone row can't be removed (there'd be nowhere left
// to put that quantity), same reasoning as most other "at least one line"
// UIs in this app.
function updateShortfallRowRemoveButtons(container) {
  const rows = container.querySelectorAll('.shortfall-alloc-row');
  rows.forEach((row) => {
    const btn = row.querySelector('[data-action="remove-shortfall-row"]');
    if (btn) btn.classList.toggle('invisible', rows.length <= 1);
  });
}

export function addShortfallAllocationRow(button) {
  const key = `${button.dataset.quoteId}-${button.dataset.itemId}`;
  const container = document.getElementById(`shortfallRowsContainer-${key}`);
  if (!container) return;
  const shortfallQty = parseInt(button.dataset.shortfallQty, 10) || 0;
  const defaultPrice = parseFloat(button.dataset.defaultPrice) || 0;
  // New row defaults to whatever's still unallocated across the existing
  // rows, so splitting a shortfall in half (say) is just "add a row" then
  // adjust one number, not two.
  const allocated = Array.from(container.querySelectorAll('[data-shortfall-qty]'))
    .reduce((sum, input) => sum + (parseInt(input.value, 10) || 0), 0);
  const remaining = Math.max(shortfallQty - allocated, 0);
  container.insertAdjacentHTML('beforeend', shortfallAllocationRowHtml(remaining || '', defaultPrice));
  updateShortfallRowRemoveButtons(container);
}

export function removeShortfallAllocationRow(button) {
  const container = button.closest('[data-shortfall-rows]');
  const row = button.closest('.shortfall-alloc-row');
  if (row) row.remove();
  if (container) updateShortfallRowRemoveButtons(container);
}

function renderFulfillmentQueue() {
  const body = document.getElementById('fulfillmentQueueBody');
  const countEl = document.getElementById('fulfillmentQueueCount');
  if (!body) return;
  if (countEl) countEl.textContent = fulfillmentQueueCache.length;

  if (fulfillmentQueueCache.length === 0) {
    body.innerHTML = `<p class="px-4 py-6 text-center text-[13px] text-slate-500">Nothing is Approved / Ready for Pickup right now.</p>`;
    updateFulfillmentSelection();
    return;
  }

  body.innerHTML = fulfillmentQueueCache.map((q) => {
    const lines = q.items.map((li) => `${li.quantity}× ${escapeHtml(li.asset_name)}`).join(', ');

    // PARTIAL-SHORTFALL OUTSOURCING: one control block per line that's both
    // inventory-backed (`!li.is_outsourced` -- an already-outsourced line
    // never had stock to begin with, see get_fulfillment_queue() in
    // backend/services/quotation_service.py) AND currently short
    // (`li.stock_shortfall`). Checking the box tells POST
    // /quotations/{id}/checkout to source ONLY the shortfall portion
    // externally (whatever stock IS on hand still checks out of inventory
    // normally) instead of blocking the whole quote's checkout -- and that
    // shortfall can be split across more than one allocation row/
    // outsourcing company. processFulfillmentSelected() below reads these
    // rows back out by container id at submit time (same "read the DOM
    // directly" pattern addQuoteOutsourcedItem() above uses), and
    // bulk_checkout_quotation()'s `outsource_shortfall_items` handling is
    // what actually applies the decision server-side. Left unchecked, a
    // genuinely short line still blocks that quote's checkout exactly like
    // before this feature existed -- nothing changes unless a Manager/Admin
    // explicitly opts a line in.
    const shortfallControls = q.items
      .filter((li) => li.stock_shortfall && !li.is_outsourced)
      .map((li) => {
        const key = `${q.id}-${li.item_id}`;
        const shortfallQty = li.shortfall_quantity != null ? li.shortfall_quantity : (li.quantity - li.available_quantity);
        return `
          <div class="mt-2 rounded-md border border-rose-500/30 bg-rose-500/5 p-2.5">
            <p class="text-[11px] font-medium text-rose-400">⚠ '${escapeHtml(li.asset_name)}' needs ${li.quantity}, only ${li.available_quantity} available -- ${shortfallQty} short.</p>
            <label class="mt-1.5 flex cursor-pointer items-center gap-1.5 text-[12px] text-slate-300">
              <input type="checkbox" id="shortfallOutsourceToggle-${key}" data-shortfall-checkbox
                data-quote-id="${q.id}" data-item-id="${li.item_id}" data-shortfall-qty="${shortfallQty}"
                class="h-3.5 w-3.5 rounded border-border bg-card2" />
              Source the ${shortfallQty} short externally so ${li.available_quantity} still checks out of stock
            </label>
            <div id="shortfallRowsContainer-${key}" data-shortfall-rows class="mt-1.5 space-y-1.5">
              ${shortfallAllocationRowHtml(shortfallQty, li.unit_price)}
            </div>
            <button type="button" data-action="add-shortfall-row" data-quote-id="${q.id}" data-item-id="${li.item_id}"
              data-default-price="${li.unit_price != null ? li.unit_price : 0}" data-shortfall-qty="${shortfallQty}"
              class="mt-1.5 text-[11px] font-medium text-blue-400 hover:underline">
              + Split across another outsourcing company
            </button>
          </div>`;
      }).join('');

    return `
      <div class="flex items-start gap-3 border-b border-border p-4 last:border-0 hover:bg-card2/40">
        <input type="checkbox" data-action="toggle-fulfillment-selection" class="fulfillment-item-checkbox mt-1 h-4 w-4 cursor-pointer rounded border-border bg-card2" data-quote-id="${q.id}" />
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <p class="tag-mono font-medium text-slate-100">${escapeHtml(q.reference_number)}</p>
            <p class="tag-mono text-[13px] text-slate-200">${formatPrice(q.total)}</p>
          </div>
          <p class="mt-0.5 text-[12px] text-slate-400">For ${q.checkout_to ? escapeHtml(`${q.checkout_to.name} (${q.checkout_to.email})`) : 'Unknown'} · ${q.item_count} item(s) · approved ${escapeHtml(formatTimestamp(q.approved_at))}</p>
          <p class="mt-1 truncate text-[12px] text-slate-500">${lines}</p>
          ${q.has_shortfall ? `<p class="mt-1 text-[11px] font-medium text-rose-400">⚠ Not enough stock on hand for one or more lines below.</p>` : ''}
          ${shortfallControls}
        </div>
      </div>`;
  }).join('');

  updateFulfillmentSelection();
}

export function updateFulfillmentSelection() {
  const checkboxes = document.querySelectorAll('.fulfillment-item-checkbox');
  const checked = document.querySelectorAll('.fulfillment-item-checkbox:checked');
  const countEl = document.getElementById('fulfillmentSelectedCount');
  const bulkBtn = document.getElementById('processFulfillmentBtn');
  const selectAllEl = document.getElementById('fulfillmentSelectAll');
  if (countEl) countEl.textContent = checked.length;
  if (bulkBtn) bulkBtn.disabled = checked.length === 0;
  if (selectAllEl) selectAllEl.checked = checkboxes.length > 0 && checked.length === checkboxes.length;
}

export function toggleSelectAllFulfillment(masterCheckbox) {
  document.querySelectorAll('.fulfillment-item-checkbox').forEach((cb) => { cb.checked = masterCheckbox.checked; });
  updateFulfillmentSelection();
}

// Bulk-checks-out every SELECTED approved quote, one at a time (each call
// is its own atomic, stock-locked transaction server-side -- see
// bulk_checkout_quotation() in backend/services/quotation_service.py). A
// per-quote failure (e.g. a genuine stock shortfall discovered at the
// authoritative, row-locked moment) is reported but doesn't stop the rest
// of the batch from processing.
export async function processFulfillmentSelected(button) {
  const checked = Array.from(document.querySelectorAll('.fulfillment-item-checkbox:checked'));
  if (!checked.length) return;
  const original = button ? button.textContent : null;
  if (button) { button.disabled = true; button.textContent = 'Checking out…'; }

  let succeeded = 0;
  const failures = [];
  for (const cb of checked) {
    const quoteId = parseInt(cb.dataset.quoteId, 10);
    // PARTIAL-SHORTFALL OUTSOURCING: pick up whichever of THIS quote's
    // shortfall-line checkboxes (rendered by renderFulfillmentQueue()
    // above) are checked, along with each of their allocation rows
    // (Qty/Sourced-from/Price -- one or more per line, split across
    // outsourcing companies), and send them along as this quote's
    // outsource_shortfall_items -- see bulk_checkout_quotation() in
    // backend/services/quotation_service.py for how each is applied
    // (only ever used for a line the authoritative stock check right
    // before it finds genuinely short; otherwise ignored). A line whose
    // rows don't add up to any valid quantity is left out entirely, same
    // as leaving the checkbox unchecked -- the server then reports the
    // ordinary "not enough available" error for that line.
    const outsourceShortfallItems = Array.from(
      document.querySelectorAll(`[data-shortfall-checkbox][data-quote-id="${quoteId}"]:checked`)
    ).map((box) => {
      const itemId = parseInt(box.dataset.itemId, 10);
      const key = `${quoteId}-${itemId}`;
      const container = document.getElementById(`shortfallRowsContainer-${key}`);
      const rows = container ? Array.from(container.querySelectorAll('.shortfall-alloc-row')) : [];
      const allocations = rows.map((row) => {
        const qty = parseInt(row.querySelector('[data-shortfall-qty]')?.value, 10);
        const sourceInput = row.querySelector('[data-shortfall-source]');
        const priceInput = row.querySelector('[data-shortfall-price]');
        const typedPrice = priceInput && priceInput.value !== '' ? parseFloat(priceInput.value) : NaN;
        return {
          quantity: (!isNaN(qty) && qty > 0) ? qty : 0,
          sourced_from: (sourceInput && sourceInput.value.trim()) ? sourceInput.value.trim() : null,
          unit_price: (!isNaN(typedPrice) && typedPrice >= 0) ? typedPrice : null,
        };
      }).filter((a) => a.quantity > 0);
      return { quotation_item_id: itemId, allocations };
    }).filter((decision) => decision.allocations.length > 0);

    try {
      await apiRequest(`/quotations/${quoteId}/checkout`, {
        method: 'POST',
        body: JSON.stringify({ outsource_shortfall_items: outsourceShortfallItems }),
      });
      succeeded += 1;
    } catch (err) {
      const ref = fulfillmentQueueCache.find((q) => q.id === quoteId)?.reference_number || `#${quoteId}`;
      failures.push(`${ref}: ${err.message}`);
    }
  }

  if (succeeded) showToast(`Checked out ${succeeded} quotation(s).`);
  if (failures.length) alert(`Some quotations could not be checked out:\n\n${failures.join('\n')}`);

  await loadFulfillmentQueue();
  loadQuotes();
  if (button) { button.disabled = checked.length === 0; button.textContent = original; }
}


export async function initQuotationPage() {
  await loadPublicConfig();
  await loadCatalog();
  await loadMyQuotation();
  await loadMyQuotationHistory();
  initQuotationSwipeNav();
}

// Called once from main.js on admin.html/manager.html, only if the Quotes
// tab's markup is present on that page/role.
export async function initQuotesTab() {
  await loadPublicConfig();
  await loadQuotes();
}
