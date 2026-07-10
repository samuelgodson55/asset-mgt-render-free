// =============================================================================
// js/components/myitems.js
// -----------------------------------------------------------------------------
// Self-service "My Checked-Out Items" table used on staff.html and
// customer.html -- each user only sees their own custody ledger here (no
// admin actions), backed by GET /users/me/items.
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, tableState, registerRenderer, filterAndPaginate, renderPaginationBar } from '../ui.js';

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

  tbody.innerHTML = pageRows.map(item => `
    <tr class="transition hover:bg-card2/40">
      <td class="px-5 py-3.5 font-medium text-slate-100">${escapeHtml(item.asset_name)}</td>
      <td class="px-5 py-3.5 tag-mono text-slate-300">${item.quantity}</td>
      <td class="px-5 py-3.5 tag-mono text-slate-300">${escapeHtml(item.checkout_date)}</td>
      <td class="px-5 py-3.5 tag-mono text-slate-300">${escapeHtml(item.due_date)}</td>
      <td class="px-5 py-3.5">
        <span class="inline-flex items-center gap-1 rounded-full bg-blue-500/10 px-2.5 py-1 text-[11px] font-semibold text-blue-400 ring-1 ring-blue-500/30">
          <span class="h-1.5 w-1.5 rounded-full bg-blue-500"></span> On Loan
        </span>
      </td>
      <td class="px-5 py-3.5">
        <button data-action="open-extension-request" data-checkout-id="${item.checkout_id}" data-asset-name="${escapeHtml(item.asset_name)}" data-due-date="${escapeHtml(item.due_date)}"
          class="rounded-md border border-border px-2.5 py-1.5 text-[11px] font-semibold text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">
          Request Extension
        </button>
      </td>
    </tr>`).join('') || `<tr><td colspan="6" class="px-5 py-6 text-center text-slate-500">You have no items currently checked out.</td></tr>`;

  renderPaginationBar('myItems', total, startIndex, pageRows.length);
}
registerRenderer('myItems', renderMyItemsTable);
