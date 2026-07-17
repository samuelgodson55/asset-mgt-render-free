// =============================================================================
// js/components/extensions.js
// -----------------------------------------------------------------------------
// Three parts of the same feature:
//   1. Self-service "Request Extension" modal (staff.html / customer.html,
//      triggered from a button next to each row in the My Items table --
//      see js/components/myitems.js) -> POST /checkouts/{id}/extension-requests.
//   2. The "Extension Requests" review panel (admin.html / manager.html,
//      same idea as js/components/overdue.js's alert banner) -> lists
//      pending requests from GET /checkouts/extension-requests, grouped by
//      requester, as a pure summary -- clicking one opens that requester's
//      Custody Ledger drawer, which is where the Manager/Admin/Super Admin
//      actually approves or denies (see #3 below and
//      POST /checkouts/extension-requests/{id}/decision).
//   3. The "Extend" button inside the Custody Ledger drawer (admin.html /
//      manager.html -- User Directory AND Ad-Hoc Directory both use the
//      same drawer, see js/components/custody.js) -> lets a Manager/Admin/
//      Super Admin set a new due date on the spot, with no request/decision
//      round trip -> POST /checkouts/{id}/extend. When a request is already
//      pending on that item, this button is replaced by Approve/Deny acting
//      on that specific request instead -- this is the ONLY place Approve/
//      Deny happens now (see components/custody.js's item row template).
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, openModal, closeModal, groupByPerson, isItemDismissed, dismissItems, showFieldError, clearFieldError } from '../ui.js';
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
  clearFieldError('extensionNewDueDate');
  openModal('extensionRequestModal');
}

export async function submitExtensionRequestForm(event) {
  event.preventDefault();
  if (!pendingExtensionCheckoutId) return;

  const newDueDate = document.getElementById('extensionNewDueDate').value;
  const reason = document.getElementById('extensionReason').value.trim();
  if (!newDueDate) {
    showFieldError('extensionNewDueDate', 'Choose a date.');
    return;
  }
  clearFieldError('extensionNewDueDate');

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

// ---- 2. Manager/Admin review panel (bell dropdown, admin.html/manager.html) ---
// Renders into the Notification Center's "Extension Requests" section. See
// components/overdue.js's module docstring for why there's no dismiss/
// recall logic here anymore -- living inside a closed-by-default bell
// dropdown removes the need for it. This panel is now a pure summary --
// clicking a row opens the requester's Custody Ledger, where the actual
// Approve/Deny decision happens (see components/custody.js).

export async function loadExtensionRequests() {
  const panel = document.getElementById('extensionRequestsPanel');
  if (!panel) return false; // this page doesn't have the panel (e.g. staff/customer dashboards)

  try {
    const result = await apiRequest('/checkouts/extension-requests?status=pending&limit=10');

    if (!result.total) {
      panel.classList.add('hidden');
      return false;
    }

    document.getElementById('extensionRequestsCount').textContent = result.total;

    // Grouped by PERSON so someone with several pending requests gets one
    // heading, not several indistinguishable rows -- same "don't flag
    // once per item" idea as the Overdue/Due Soon banners (see
    // components/due-soon.js's loadDueSoonAlerts()).
    //
    // Approve/Deny used to live right here as buttons on each individual
    // request -- now this panel is purely a summary, same shape as the
    // Overdue/Due Soon sections above it: one line per requester, a single
    // "Custody Ledger ->" link, and nothing to accidentally misclick. The
    // actual decision is made on the Custody Ledger drawer itself (see
    // components/custody.js's item row template, which already swaps that
    // item's "Extend" button for Approve/Deny the moment a request is
    // pending on it) -- so approving/denying always happens in the one
    // place that also shows the full context (due date, outstanding
    // quantity, every other item this person holds), not in a cramped
    // dropdown.
    const groups = groupByPerson(result.items);
    const list = document.getElementById('extensionRequestsList');
    list.innerHTML = groups.map(g => {
      const canOpen = g.entityType && g.entityId != null;
      const count = g.items.length;
      return `
      <li class="flex items-center justify-between gap-3 py-1.5 ${canOpen ? 'cursor-pointer transition hover:text-violet-200' : ''}"
        ${canOpen ? `data-action="open-custody" data-entity-id="${g.entityId}" data-entity-type="${g.entityType}" data-action-notification="1"` : ''}>
        <span class="truncate text-slate-200">
          <span class="font-medium">${escapeHtml(g.assigneeName)}</span>
          <span class="text-slate-500"> has ${count} pending extension request${count === 1 ? '' : 's'}</span>
        </span>
        <span class="shrink-0 tag-mono text-[11px] font-semibold text-violet-400">Custody Ledger →</span>
      </li>`;
    }).join('');

    panel.classList.remove('hidden');
    return true;
  } catch (err) {
    // Same "fail quietly" rule as the overdue alert section -- a broken
    // extension-requests panel should never block the rest of the
    // dashboard/dropdown from loading.
    panel.classList.add('hidden');
    console.error('Failed to load extension requests:', err.message);
    return false;
  }
}

// Denying a request without saying why leaves the requester (and anyone
// looking back at the Audit Trail later) guessing, but a browser
// window.prompt() is cramped, has no room for a real explanation, and
// looks completely different from every other form in this app. Deny now
// opens the same kind of modal as everything else, with a proper textarea
// -- see openDenyReasonModal()/submitDenyReasonForm() below. Approving
// doesn't need this detour: there's nothing to explain when granting
// exactly what was asked for, so it still decides immediately.
let pendingDenyRequestId = null;

export function openDenyReasonModal(requestId) {
  pendingDenyRequestId = requestId;
  document.getElementById('denyReasonText').value = '';
  clearFieldError('denyReasonText');
  openModal('denyReasonModal');
}

export async function submitDenyReasonForm(event) {
  event.preventDefault();
  if (!pendingDenyRequestId) return;
  const note = document.getElementById('denyReasonText').value.trim();
  closeModal('denyReasonModal');
  await finalizeExtensionDecision(pendingDenyRequestId, false, note || null);
  pendingDenyRequestId = null;
}

export async function decideExtensionRequest(requestId, approve) {
  if (!approve) {
    openDenyReasonModal(requestId);
    return;
  }
  await finalizeExtensionDecision(requestId, true, null);
}

async function finalizeExtensionDecision(requestId, approve, note) {
  try {
    await apiRequest(`/checkouts/extension-requests/${requestId}/decision`, {
      method: 'POST',
      body: JSON.stringify({ approve, note }),
    });
    loadExtensionRequests();
    refreshDashboard();
    // If a Custody Ledger drawer is open right now (e.g. the Manager/Admin
    // decided this straight from the item's own Approve/Deny buttons --
    // see components/custody.js's row template), re-render it so the
    // item's due date/badge reflects the decision immediately instead of
    // going stale until the drawer is closed and reopened.
    const { id, type } = getCurrentCustodyEntity();
    if (!document.getElementById('custodyModal').classList.contains('hidden') && id) {
      openCustodyModal(id, type);
    }
  } catch (err) {
    alert(err.message);
  }
}

// ---- 2b. Self-service "my extension decisions" banner (all dashboards) ----
// The requester-facing counterpart to the review panel above: once a
// Manager/Admin decides one of MY OWN extension requests, I should find
// out even if I never check email -- see backend/services/
// extension_service.py's list_my_recent_extension_decisions().
//
// DISMISSAL MODEL: per-item, not signature-based (unlike the Overdue/Due
// Soon/Extension Requests banners) -- see js/ui.js's isItemDismissed()/
// dismissItems() for the full rationale. In short: this is a one-time
// notification feed, not a live status banner, so once a person closes a
// decision it must stay gone forever, even after a newer, different
// decision arrives alongside it later.
const MY_DECISIONS_STORAGE_KEY = 'myExtensionDecisions';
let currentDecisionIds = []; // ids currently rendered in the banner, so dismiss() knows exactly what to mark seen

export function dismissMyExtensionDecisionsAlert() {
  const banner = document.getElementById('myExtensionDecisionsBanner');
  if (banner) banner.classList.add('hidden');
  dismissItems(MY_DECISIONS_STORAGE_KEY, currentDecisionIds);
  currentDecisionIds = [];
}

export async function loadMyExtensionDecisionsAlert() {
  const banner = document.getElementById('myExtensionDecisionsBanner');
  if (!banner) return false; // this page doesn't have the banner

  try {
    const result = await apiRequest('/checkouts/my-extension-decisions?limit=10');

    // Filter out anything already individually dismissed on a previous
    // visit -- this is the step that makes "seen it, closed it" permanent
    // per-decision instead of resetting the moment a newer decision shows
    // up in the same server response (see the model comment above).
    const unseen = result.items.filter(item => !isItemDismissed(MY_DECISIONS_STORAGE_KEY, item.id));

    if (!unseen.length) {
      banner.classList.add('hidden');
      currentDecisionIds = [];
      return false;
    }

    document.getElementById('myExtensionDecisionsCount').textContent = unseen.length;

    const list = document.getElementById('myExtensionDecisionsList');
    // flex-wrap + break-words (not `truncate`) here on purpose: this
    // banner sits on staff/customer dashboards which are viewed on
    // phones far more than admin/manager ones are, and a mid-length
    // asset name + decision note truncating to an ellipsis on a narrow
    // screen hides exactly the information ("what got approved, what's
    // the new due date") this banner exists to surface. Wrapping to a
    // second line costs a little vertical space; silently cutting off
    // the answer doesn't.
    list.innerHTML = unseen.map(item => {
      const approved = item.status === 'approved';
      return `
      <li class="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 py-1.5">
        <span class="font-semibold ${approved ? 'text-emerald-400' : 'text-rose-400'}">${approved ? 'Approved' : 'Denied'}:</span>
        <span class="font-medium text-slate-200 break-words">${escapeHtml(item.asset_name)}</span>
        <span class="text-slate-500">
          ${approved ? `— new due date ${escapeHtml(item.due_date || item.requested_new_due_date)}` : '— current due date unchanged'}
        </span>
        ${item.decision_note ? `<span class="italic text-slate-500 break-words">· "${escapeHtml(item.decision_note)}"</span>` : ''}
      </li>`;
    }).join('');

    currentDecisionIds = unseen.map(item => item.id);
    banner.classList.remove('hidden');
    return true;
  } catch (err) {
    // Same "fail quietly" rule as every other alert banner -- this should
    // never block the rest of the page (My Items table, etc.) from loading.
    banner.classList.add('hidden');
    console.error('Failed to load my extension decisions:', err.message);
    return false;
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
  clearFieldError('directExtendNewDueDate');
  openModal('directExtendModal');
}

export async function submitDirectExtendForm(event) {
  event.preventDefault();
  if (!pendingDirectExtendCheckoutId) return;

  const newDueDate = document.getElementById('directExtendNewDueDate').value;
  const reason = document.getElementById('directExtendReason').value.trim();
  if (!newDueDate) {
    showFieldError('directExtendNewDueDate', 'Choose a date.');
    return;
  }
  clearFieldError('directExtendNewDueDate');

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
