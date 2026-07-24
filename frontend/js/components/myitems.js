// =============================================================================
// js/components/myitems.js
// -----------------------------------------------------------------------------
// Self-service "My Checked-Out Items" table used on staff.html and
// customer.html -- each user only sees their own custody ledger here (no
// admin actions), backed by GET /users/me/items. Rows due within
// settings.DUE_SOON_REMINDER_DAYS show an amber "Due Soon" badge instead
// of the usual "On Loan" one (see the `due_soon` flag computed server-side
// by services/user_service.py's `_is_due_soon()`) -- a personal reminder
// before the item actually goes overdue.
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, formatTimestamp, tableState, registerRenderer, filterAndPaginate, renderPaginationBar, rowDetailsTrigger } from '../ui.js';

export async function loadMyItems() {
  const tbody = document.getElementById('myItemsTableBody');
  if (!tbody) return; // this page doesn't have a self-service table
  try {
    const data = await apiRequest('/users/me/items');

    const nameEl = document.getElementById('myProfileName');
    if (nameEl) nameEl.textContent = data.name;
    const roleEl = document.getElementById('myProfileRole');
    if (roleEl) roleEl.textContent = data.department_role || (data.role === 'customer' ? 'External Client Contact' : data.role);

    tableState.myItems.raw = data.assigned_items;
    renderMyItemsTable();
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" class="px-5 py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
  }
}

export function renderMyItemsTable() {
  const tbody = document.getElementById('myItemsTableBody');
  if (!tbody) return;

  const { pageRows, total, startIndex } = filterAndPaginate('myItems', ['asset_name']);
  document.querySelectorAll('.my-item-count').forEach(el => el.textContent = total);

  tbody.innerHTML = pageRows.map(item => {
    const actionButtons = `<button data-action="open-extension-request" data-checkout-id="${item.checkout_id}" data-asset-name="${escapeHtml(item.asset_name)}" data-due-date="${escapeHtml(item.due_date)}"
      class="rounded-md border border-border px-2.5 py-1.5 text-[11px] font-semibold text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">
      Request Extension
    </button>`;

    // Whole row is tappable on mobile -- see components/assets.js's
    // renderAssetsTable() for the full explanation of this pattern.
    return `
    <tr ${rowDetailsTrigger(escapeHtml(item.asset_name), [
      ['Quantity', String(item.quantity)],
      ['Checked Out', escapeHtml(formatTimestamp(item.checkout_date))],
      ['Due Back', escapeHtml(item.due_date)],
      ['', `<div class="flex flex-wrap gap-2">${actionButtons}</div>`],
    ])} class="cursor-pointer transition hover:bg-card2/40 active:bg-card2/60 sm:cursor-default">
      <td class="px-5 py-3.5">
        <div class="flex items-center gap-2">
          <span class="font-medium text-slate-100">${escapeHtml(item.asset_name)}</span>
          <!-- Mobile-only affordance showing the row itself is tappable
               (replaces the old separate "Details" button). -->
          <svg class="ml-auto h-4 w-4 shrink-0 text-slate-600 sm:hidden" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
        </div>
      </td>
      <td class="hidden px-5 py-3.5 tag-mono text-slate-300 sm:table-cell">${item.quantity}</td>
      <td class="hidden px-5 py-3.5 tag-mono text-slate-300 sm:table-cell" title="${escapeHtml(item.checkout_date)}">${formatTimestamp(item.checkout_date)}</td>
      <td class="hidden px-5 py-3.5 tag-mono text-slate-300 sm:table-cell">${escapeHtml(item.due_date)}</td>
      <td class="px-5 py-3.5">
        ${item.due_soon
          ? `<span class="inline-flex items-center gap-1 rounded-full bg-amber-500/10 px-2.5 py-1 text-[11px] font-semibold text-amber-400 ring-1 ring-amber-500/30" title="A reminder before this goes overdue -- return it or request an extension soon.">
              <span class="h-1.5 w-1.5 rounded-full bg-amber-500"></span> Due Soon
            </span>`
          : `<span class="inline-flex items-center gap-1 rounded-full bg-blue-500/10 px-2.5 py-1 text-[11px] font-semibold text-blue-400 ring-1 ring-blue-500/30">
              <span class="h-1.5 w-1.5 rounded-full bg-blue-500"></span> On Loan
            </span>`}
      </td>
      <td class="hidden px-5 py-3.5 sm:table-cell">${actionButtons}</td>
    </tr>`;
  }).join('') || `<tr><td colspan="6" class="px-5 py-6 text-center text-slate-500">You have no items currently checked out.</td></tr>`;

  renderPaginationBar('myItems', total, startIndex, pageRows.length);
}
registerRenderer('myItems', renderMyItemsTable);
