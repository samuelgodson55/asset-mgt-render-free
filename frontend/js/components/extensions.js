// =============================================================================
// js/components/extensions.js
// -----------------------------------------------------------------------------
// Three parts of the same feature:
//   1. Self-service "Request Extension" modal (staff.html / customer.html,
//      triggered from a button next to each row in the My Items table --
//      see js/components/myitems.js) -> POST /checkouts/{id}/extension-requests.
//   2. The "Extension Requests" review panel (admin.html / manager.html,
//      same idea as js/components/overdue.js's alert banner) -> lists
//      pending requests from GET /checkouts/extension-requests and lets a
//      Manager/Admin/Super Admin approve or deny each one inline ->
//      POST /checkouts/extension-requests/{id}/decision.
//   3. The "Extend" button inside the Custody Ledger drawer (admin.html /
//      manager.html -- User Directory AND Ad-Hoc Directory both use the
//      same drawer, see js/components/custody.js) -> lets a Manager/Admin/
//      Super Admin set a new due date on the spot, with no request/decision
//      round trip -> POST /checkouts/{id}/extend.
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, openModal, closeModal } from '../ui.js';
import { refreshDashboard } from '../dashboard.js';
import { loadMyItems } from './myitems.js';
import { getCurrentCustodyEntity, openCustodyModal } from './custody.js';

let pendingExtensionCheckoutId = null;
let pendingDirectExtendCheckoutId = null;

// ---- 1. Self-service request modal (staff.html / customer.html) -----------

export function openExtensionRequestModal(checkoutId, assetName, currentDueDate) {
  pendingExtensionCheckoutId = checkoutId;
  document.getElementById('extensionRequestAssetName').textContent = assetName;
  document.getElementById('extensionRequestCurrentDue').textContent = currentDueDate;
  document.getElementById('extensionNewDueDate').value = '';
  document.getElementById('extensionReason').value = '';
  openModal('extensionRequestModal');
}

export async function submitExtensionRequestForm(event) {
  event.preventDefault();
  if (!pendingExtensionCheckoutId) return;

  const newDueDate = document.getElementById('extensionNewDueDate').value;
  const reason = document.getElementById('extensionReason').value.trim();
  if (!newDueDate) {
    alert('Choose the new due date you would like to request.');
    return;
  }

  try {
    await apiRequest(`/checkouts/${pendingExtensionCheckoutId}/extension-requests`, {
      method: 'POST',
      body: JSON.stringify({ new_due_date: newDueDate, reason: reason || null }),
    });
    closeModal('extensionRequestModal');
    alert('Extension request submitted -- your manager/admin will review it shortly.');
    loadMyItems();
  } catch (err) {
    alert(err.message);
  }
}

// ---- 2. Manager/Admin review panel (admin.html / manager.html) ------------

export async function loadExtensionRequests() {
  const panel = document.getElementById('extensionRequestsPanel');
  if (!panel) return; // this page doesn't have the panel (e.g. staff/customer dashboards)

  try {
    const result = await apiRequest('/checkouts/extension-requests?status=pending&limit=10');

    if (!result.total) {
      panel.classList.add('hidden');
      return;
    }

    document.getElementById('extensionRequestsCount').textContent = result.total;

    const list = document.getElementById('extensionRequestsList');
    list.innerHTML = result.items.map(item => `
      <div class="flex flex-col gap-2 rounded-lg border border-border bg-card2/50 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between">
        <div class="min-w-0">
          <p class="truncate text-[13px] font-medium text-slate-200">${escapeHtml(item.asset_name)}</p>
          <p class="tag-mono text-[11px] text-slate-500">
            ${escapeHtml(item.requested_by_label)} · ${item.previous_due_date ? `was ${escapeHtml(item.previous_due_date)} → ` : ''}requesting ${escapeHtml(item.requested_new_due_date)}
          </p>
          ${item.reason ? `<p class="mt-0.5 text-[11px] italic text-slate-500">"${escapeHtml(item.reason)}"</p>` : ''}
        </div>
        <div class="flex shrink-0 items-center gap-2">
          <button data-action="approve-extension" data-request-id="${item.id}" class="rounded-md bg-emerald-600/90 px-2.5 py-1.5 text-[11px] font-semibold text-white transition hover:bg-emerald-500">Approve</button>
          <button data-action="deny-extension" data-request-id="${item.id}" class="rounded-md border border-border px-2.5 py-1.5 text-[11px] font-semibold text-slate-300 transition hover:border-rose-500/60 hover:text-rose-400">Deny</button>
        </div>
      </div>`).join('');

    panel.classList.remove('hidden');
  } catch (err) {
    // Same "fail quietly" rule as the overdue alert banner -- a broken
    // extension-requests panel should never block the rest of the
    // dashboard from loading.
    panel.classList.add('hidden');
    console.error('Failed to load extension requests:', err.message);
  }
}

export async function decideExtensionRequest(requestId, approve) {
  let note = null;
  if (!approve) {
    note = window.prompt('Optional note to include when denying this request:', '') || null;
  }
  try {
    await apiRequest(`/checkouts/extension-requests/${requestId}/decision`, {
      method: 'POST',
      body: JSON.stringify({ approve, note }),
    });
    loadExtensionRequests();
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}

// ---- 3. Manager/Admin "Extend" action (Custody Ledger drawer) -------------
// Lets a Manager/Admin/Super Admin grant more time on the spot from the
// User Directory or Ad-Hoc Directory's Custody Ledger drawer, without
// first having to log a request on the holder's behalf and then approve
// it -- see js/components/custody.js's item row template for the button,
// and backend/services/extension_service.py's extend_checkout_directly().

export function openDirectExtendModal(checkoutId, assetName, currentDueDate) {
  pendingDirectExtendCheckoutId = checkoutId;
  document.getElementById('directExtendAssetName').textContent = assetName;
  document.getElementById('directExtendCurrentDue').textContent = currentDueDate;
  document.getElementById('directExtendNewDueDate').value = '';
  document.getElementById('directExtendReason').value = '';
  openModal('directExtendModal');
}

export async function submitDirectExtendForm(event) {
  event.preventDefault();
  if (!pendingDirectExtendCheckoutId) return;

  const newDueDate = document.getElementById('directExtendNewDueDate').value;
  const reason = document.getElementById('directExtendReason').value.trim();
  if (!newDueDate) {
    alert('Choose the new due date.');
    return;
  }

  try {
    await apiRequest(`/checkouts/${pendingDirectExtendCheckoutId}/extend`, {
      method: 'POST',
      body: JSON.stringify({ new_due_date: newDueDate, reason: reason || null }),
    });
    closeModal('directExtendModal');
    // Re-render whichever Custody Ledger drawer is open right now, same
    // "reopen wherever we already are" pattern as custody.js's
    // processReturn(), so the row's due date updates immediately.
    const { id, type } = getCurrentCustodyEntity();
    if (!document.getElementById('custodyModal').classList.contains('hidden') && id) {
      openCustodyModal(id, type);
    }
    refreshDashboard();
  } catch (err) {
    alert(err.message);
  }
}
