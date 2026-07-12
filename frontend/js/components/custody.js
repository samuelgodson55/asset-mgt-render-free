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
// right next to Process Return -- and, when a request is already pending on
// that item, Approve/Deny buttons acting on THAT specific request instead
// (see the `extendOrDecideButtons` ternary below -- this is now the ONLY
// place Approve/Deny happens; the notification bell's Extension Requests
// panel just links here, see components/extensions.js's loadExtensionRequests()).
//
// RESPONSIVE ROW LAYOUT: each row stacks the item info above its controls
// on narrow screens (`flex-col` by default, `sm:flex-row` from there up),
// and the action buttons themselves wrap (`flex-wrap`) instead of forcing
// the checkbox/qty input/Extend-or-Approve-Deny/Process Return all onto one
// unbroken line -- that's what was overflowing the drawer horizontally on
// mobile before.
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, openModal, closeModal, showFieldError, clearFieldError } from '../ui.js';
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
    list.innerHTML = data.assigned_items.map(item => {
      // At most one of these two color-codes the due-date line -- an item
      // that's already overdue is always shown as overdue, never also
      // "due soon" (is_overdue()/is_due_soon() on the backend are already
      // mutually exclusive, this just mirrors that here).
      const dueDateClass = item.overdue ? 'text-rose-400' : item.due_soon ? 'text-amber-400' : 'text-slate-500';
      const dueDateSuffix = item.overdue ? ' · Overdue' : item.due_soon ? ' · Due Soon' : '';
      // When there's a pending extension request on this item, the
      // "Extend" button is replaced by Approve/Deny acting on THAT
      // specific request (see backend/services/user_service.py's
      // _pending_extension_fields()) -- a Manager/Admin who navigated
      // here straight from the "Extension Requests" bell notification
      // should be able to decide it right on the item, instead of the
      // button offering to fire off a brand new, unrelated direct
      // extension while one is already awaiting a decision.
      const extensionBadge = item.pending_extension
        ? `<span class="ml-2 inline-flex items-center gap-1 rounded-full border border-violet-500/30 bg-violet-500/10 px-2 py-0.5 text-[10px] font-semibold text-violet-300">
             Requesting → ${escapeHtml(item.pending_extension_new_due_date || '')}
           </span>`
        : '';
      const extensionReasonLine = (item.pending_extension && item.pending_extension_reason)
        ? `<p class="mt-0.5 text-[11px] italic text-slate-500">"${escapeHtml(item.pending_extension_reason)}"</p>`
        : '';
      const extendOrDecideButtons = item.pending_extension && item.pending_extension_request_id
        ? `
          <button data-action="approve-extension" data-request-id="${item.pending_extension_request_id}" class="rounded-md bg-emerald-600/90 px-2.5 py-1.5 text-[11px] font-semibold text-white transition hover:bg-emerald-500">Approve</button>
          <button data-action="deny-extension" data-request-id="${item.pending_extension_request_id}" class="rounded-md border border-border px-2.5 py-1.5 text-[11px] font-semibold text-slate-300 transition hover:border-rose-500/60 hover:text-rose-400">Deny</button>`
        : `<button data-action="open-direct-extend" data-checkout-id="${item.checkout_id}" data-asset-name="${escapeHtml(item.asset_name)}" data-due-date="${escapeHtml(item.due_date)}"
            class="rounded-md border border-border px-2.5 py-1.5 text-[11px] font-semibold text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">Extend</button>`;
      return `
    <div class="flex flex-col gap-3 rounded-lg border border-border bg-card2/50 p-3 sm:flex-row sm:items-center sm:gap-3">
      <div class="flex items-start gap-3 sm:flex-1 sm:items-center">
        <input type="checkbox" data-checkout-id="${item.checkout_id}" data-outstanding="${item.outstanding}" data-action="update-custody-selection"
          class="custody-item-checkbox mt-0.5 h-4 w-4 shrink-0 rounded border-border bg-card2 text-blue-600 focus:ring-0 focus:ring-offset-0 sm:mt-0" />
        <div class="min-w-0">
          <p class="break-words text-[13px] font-medium text-slate-200">${escapeHtml(item.asset_name)}${extensionBadge}</p>
          <p class="tag-mono text-[11px] ${dueDateClass}">
            Outstanding ${item.outstanding} / ${item.quantity} · due ${escapeHtml(item.due_date)}${dueDateSuffix}
          </p>
          ${extensionReasonLine}
        </div>
      </div>
      <div class="flex flex-wrap items-center gap-2 pl-7 sm:shrink-0 sm:justify-end sm:pl-0">
        <!-- 'relative' here (not on the row itself) so showFieldError()'s
             inserted <p data-error> can be positioned absolutely under
             JUST the input -- otherwise, as a sibling in this
             'flex items-center' row, it would render as its own flex
             item wedged between the input and the Extend button instead
             of dropping down below the field like a real validation
             message. -->
        <div class="relative">
          <input type="number" min="1" max="${item.outstanding}" value="${item.outstanding}" id="returnQty-${item.checkout_id}"
            class="w-16 rounded-md border border-border bg-card2 px-2 py-1.5 text-[12px] text-slate-200 outline-none focus:border-emerald-500/60" />
        </div>
        ${extendOrDecideButtons}
        <button data-action="process-return" data-checkout-id="${item.checkout_id}" class="rounded-md bg-emerald-600/90 px-2.5 py-1.5 text-[11px] font-semibold text-white transition hover:bg-emerald-500">Process Return</button>
      </div>
    </div>`;
    }).join('') || `<p class="text-[12px] text-slate-500">No items currently checked out.</p>`;

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
  const qtyInputId = `returnQty-${checkoutId}`;
  const qtyInput = document.getElementById(qtyInputId);
  const quantity = qtyInput ? parseInt(qtyInput.value, 10) : 1;
  if (!quantity || quantity < 1) {
    showFieldError(qtyInputId, 'Enter 1 or more.', true);
    return;
  }
  clearFieldError(qtyInputId);
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
  const bulkExtendBtn = document.getElementById('bulkExtendBtn');
  if (bulkExtendBtn) bulkExtendBtn.disabled = checked.length === 0;
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

// ---- Bulk Extend (same checkbox selection as Bulk Process Returns) --------
// Gives every SELECTED checkout on this account the SAME new due date in
// one go -- POST /checkouts/bulk-extend, see backend/services/
// extension_service.py's extend_checkouts_bulk(). Opens a small modal to
// pick that one date (+ optional reason) instead of prompting per item.

export function openBulkExtendModal() {
  const checked = document.querySelectorAll('.custody-item-checkbox:checked');
  if (!checked.length) return;
  document.getElementById('bulkExtendCount').textContent = checked.length;
  document.getElementById('bulkExtendNewDueDate').value = '';
  document.getElementById('bulkExtendReason').value = '';
  clearFieldError('bulkExtendNewDueDate');
  openModal('bulkExtendModal');
}

export async function submitBulkExtendForm(event) {
  event.preventDefault();
  const checked = document.querySelectorAll('.custody-item-checkbox:checked');
  const checkoutIds = Array.from(checked).map(cb => parseInt(cb.dataset.checkoutId, 10));
  if (!checkoutIds.length) return;

  const newDueDate = document.getElementById('bulkExtendNewDueDate').value;
  const reason = document.getElementById('bulkExtendReason').value.trim();
  if (!newDueDate) {
    showFieldError('bulkExtendNewDueDate', 'Choose a date.');
    return;
  }
  clearFieldError('bulkExtendNewDueDate');

  try {
    const result = await apiRequest('/checkouts/bulk-extend', {
      method: 'POST',
      body: JSON.stringify({ checkout_ids: checkoutIds, new_due_date: newDueDate, reason: reason || null }),
    });
    closeModal('bulkExtendModal');
    if (result.failed > 0) {
      // Some items didn't already qualify (e.g. this date isn't later than
      // one item's own current due date) -- surface that instead of
      // silently pretending everything went through.
      alert(`${result.message}\n\n${result.results.filter(r => !r.success).map(r => `Checkout #${r.checkout_id}: ${r.error}`).join('\n')}`);
    }
    if (currentCustodyUserId) openCustodyModal(currentCustodyUserId, currentCustodyEntityType);
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}
