// =============================================================================
// js/components/due-soon.js
// -----------------------------------------------------------------------------
// "A reminder BEFORE something goes overdue" -- renders the "Due Soon"
// section inside the Notification Center bell dropdown (see
// js/components/notifications.js) from GET /checkouts/due-soon. The
// proactive counterpart to js/components/overdue.js's "Overdue Checkouts"
// section: same shape, same data flow, just surfaced earlier -- while
// there's still time to return the item or grant an extension, instead of
// only finding out after the due date has already passed.
//
// Kept as its own tiny module for the exact same reason overdue.js is:
// admin.html and manager.html share the same markup/ids for it, so this
// same code works unmodified on both pages (the backend already scopes
// what a Manager vs a Super Admin sees -- see
// services/checkout_service.py's list_due_soon_checkouts()).
//
// See overdue.js's module docstring for why there's no dismiss/recall
// logic here anymore -- living inside a closed-by-default bell dropdown
// instead of an always-visible dashboard banner removes the need for it.
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, groupByPerson } from '../ui.js';

export async function loadDueSoonAlerts() {
  const section = document.getElementById('dueSoonAlertBanner');
  if (!section) return false; // this page doesn't have the section (e.g. staff/customer dashboards)

  try {
    const result = await apiRequest('/checkouts/due-soon?limit=5');

    if (!result.total) {
      section.classList.add('hidden');
      return false;
    }

    document.getElementById('dueSoonAlertCount').textContent = result.total;

    // Grouped by PERSON -- someone with several items due soon gets one
    // line ("T. Okafor has 3 checkouts due soon"), not one per item. Full
    // itemized detail (which asset, exact due date) is a click away in
    // their Custody Ledger rather than cluttering this section -- that's
    // the whole point of it being a summary.
    const groups = groupByPerson(result.items);
    const list = document.getElementById('dueSoonAlertList');
    list.innerHTML = groups.map(g => {
      const count = g.items.length;
      const canOpen = g.entityType && g.entityId != null;
      return `
      <li class="flex items-center justify-between gap-3 py-1.5 ${canOpen ? 'cursor-pointer transition hover:text-amber-200' : ''}"
        ${canOpen ? `data-action="open-custody" data-entity-id="${g.entityId}" data-entity-type="${g.entityType}" data-action-notification="1"` : ''}>
        <span class="truncate text-slate-200">
          <span class="font-medium">${escapeHtml(g.assigneeName)}</span>
          <span class="text-slate-500"> has ${count} checkout${count === 1 ? '' : 's'} due soon</span>
        </span>
        <span class="shrink-0 tag-mono text-[11px] font-semibold text-amber-400">View →</span>
      </li>`;
    }).join('');

    section.classList.remove('hidden');
    return true;
  } catch (err) {
    // Fail quietly -- same reasoning as loadOverdueAlerts(): a broken
    // notification section should never block the rest of the dropdown
    // (or dashboard) from loading/working.
    section.classList.add('hidden');
    console.error('Failed to load due-soon checkouts alert:', err.message);
    return false;
  }
}
