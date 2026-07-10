// =============================================================================
// js/components/custody.js
// -----------------------------------------------------------------------------
// The Custody Ledger modal is shared identically by the User Directory and
// the Ad-Hoc (Unlinked) Directory (requirement #6 from an earlier pass) --
// the underlying data shape (a list of checkouts with a
// checkout_id/quantity/due_date) is identical whether `entityType` is
// 'user' or 'outsider'. Return-processing (single or bulk) also lives here
// since it's triggered from both this modal AND the Properties Hub's
// deployment ledger in components/assets.js. Each row also has an "Extend"
// button (see components/extensions.js's openDirectExtendModal()) letting
// a Manager/Admin/Super Admin grant more time on this checkout directly,
// right next to Process Return.
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, openModal } from '../ui.js';
import { refreshDashboard } from '../dashboard.js';
import { getCurrentPropsAssetId, openPropsModal } from './assets.js';

let currentCustodyUserId = null;   // remembers which user/outsider the open custody ledger is for
let currentCustodyEntityType = 'user';

// Lets other modules (components/exports.js's "Export" button in the
// Custody Ledger drawer) find out which user/outsider is currently open,
// without needing their own copy of this module-level state -- same
// pattern as components/assets.js's getCurrentPropsAssetId().
export function getCurrentCustodyEntity() {
  return { id: currentCustodyUserId, type: currentCustodyEntityType };
}

export async function openCustodyModal(entityId, entityType = 'user') {
  currentCustodyUserId = entityId;
  currentCustodyEntityType = entityType;
  try {
    const path = entityType === 'outsider' ? `/outsiders/${entityId}/items` : `/users/${entityId}/items`;
    const data = await apiRequest(path);
    const subtitle = entityType === 'outsider'
      ? `${data.name} · ${data.contact_details}${data.company ? ' · ' + data.company : ''}`
      : `${data.name} · ${data.email}`;
    document.getElementById('custodyUserName').textContent = subtitle;
    document.getElementById('custodyCount').textContent = data.assigned_items.length;

    const list = document.getElementById('custodyItemList');
    list.innerHTML = data.assigned_items.map(item => `
    <div class="flex items-center gap-3 rounded-lg border border-border bg-card2/50 px-3 py-2.5">
      <input type="checkbox" data-checkout-id="${item.checkout_id}" data-outstanding="${item.outstanding}" data-action="update-custody-selection"
        class="custody-item-checkbox h-4 w-4 rounded border-border bg-card2 text-blue-600 focus:ring-0 focus:ring-offset-0" />
      <div class="flex flex-1 items-center justify-between">
        <div>
          <p class="text-[13px] font-medium text-slate-200">${escapeHtml(item.asset_name)}</p>
          <p class="tag-mono text-[11px] text-slate-500">Outstanding ${item.outstanding} / ${item.quantity} · due ${escapeHtml(item.due_date)}</p>
        </div>
        <div class="flex items-center gap-2">
          <input type="number" min="1" max="${item.outstanding}" value="${item.outstanding}" id="returnQty-${item.checkout_id}"
            class="w-16 rounded-md border border-border bg-card2 px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-emerald-500/60" />
          <button data-action="open-direct-extend" data-checkout-id="${item.checkout_id}" data-asset-name="${escapeHtml(item.asset_name)}" data-due-date="${escapeHtml(item.due_date)}"
            class="rounded-md border border-border px-2.5 py-1.5 text-[11px] font-semibold text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">Extend</button>
          <button data-action="process-return" data-checkout-id="${item.checkout_id}" class="rounded-md bg-emerald-600/90 px-2.5 py-1.5 text-[11px] font-semibold text-white transition hover:bg-emerald-500">Process Return</button>
        </div>
      </div>
    </div>`).join('') || `<p class="text-[12px] text-slate-500">No items currently checked out.</p>`;

    updateCustodySelection();
    openModal('custodyModal');
  } catch (err) {
    alert(err.message);
  }
}

// ---- Process a quantified return (requirement #5 from an earlier pass) ----
// Reads the "Return Quantity" number input next to the button that
// triggered this (falls back to whatever the input inside the Properties
// Hub's deployment ledger holds, since both modals share the same pattern).
export async function processReturn(checkoutId) {
  const qtyInput = document.getElementById(`returnQty-${checkoutId}`);
  const quantity = qtyInput ? parseInt(qtyInput.value, 10) : 1;
  if (!quantity || quantity < 1) {
    alert('Enter a valid return quantity (1 or more).');
    return;
  }
  try {
    await apiRequest(`/checkouts/${checkoutId}/return`, {
      method: 'POST', body: JSON.stringify({ quantity }),
    });
    refreshDashboard();
    if (!document.getElementById('custodyModal').classList.contains('hidden') && currentCustodyUserId) {
      openCustodyModal(currentCustodyUserId, currentCustodyEntityType);
    }
    if (!document.getElementById('propsModal').classList.contains('hidden') && getCurrentPropsAssetId()) {
      openPropsModal(getCurrentPropsAssetId());
    }
  } catch (err) {
    alert(err.message);
  }
}

export function updateCustodySelection() {
  const checkboxes = document.querySelectorAll('.custody-item-checkbox');
  const checked = document.querySelectorAll('.custody-item-checkbox:checked');
  document.getElementById('custodySelectedCount').textContent = checked.length;
  document.getElementById('bulkReturnBtn').disabled = checked.length === 0;
  document.getElementById('custodySelectAll').checked = checkboxes.length > 0 && checked.length === checkboxes.length;
}

export function toggleSelectAllCustody(masterCheckbox) {
  document.querySelectorAll('.custody-item-checkbox').forEach(cb => { cb.checked = masterCheckbox.checked; });
  updateCustodySelection();
}

// Bulk return returns the FULL outstanding amount for every selected line
// (a bulk action is inherently "clear these out entirely" -- for a partial
// return on one specific item, use that item's own Return Quantity input).
export async function bulkProcessReturns() {
  const checked = document.querySelectorAll('.custody-item-checkbox:checked');
  for (const cb of checked) {
    const quantity = parseInt(cb.dataset.outstanding, 10);
    await apiRequest(`/checkouts/${cb.dataset.checkoutId}/return`, {
      method: 'POST', body: JSON.stringify({ quantity }),
    });
  }
  if (currentCustodyUserId) openCustodyModal(currentCustodyUserId, currentCustodyEntityType);
  refreshDashboard();
}

export function processAllReturns() {
  document.querySelectorAll('.custody-item-checkbox').forEach(cb => { cb.checked = true; });
  updateCustodySelection();
  bulkProcessReturns();
}
