// =============================================================================
// js/components/overdue.js
// -----------------------------------------------------------------------------
// Operations & Observability requirement #5: renders the "Overdue Checkouts"
// section inside the Notification Center bell dropdown (see
// js/components/notifications.js) from GET /checkouts/overdue.
//
// Kept as its own tiny module (rather than folded into assets.js or
// dashboard.js) because it owns exactly one job -- load the overdue list and
// paint its section -- and both admin.html and manager.html share the exact
// same markup/ids for it, so this same code works unmodified on both pages
// (the backend already scopes what a Manager vs a Super Admin sees -- see
// services/checkout_service.py's list_overdue_checkouts()).
//
// NO DISMISS/RECALL LOGIC HERE ON PURPOSE: this used to be an always-visible
// banner sitting on the dashboard, which needed a "click X to dismiss, but
// don't immediately pop back open" affordance. Now that it only renders
// inside the bell's dropdown (closed by default, opened on demand), that
// whole problem disappears -- the section simply always reflects whatever
// is currently overdue whenever the dropdown is opened/refreshed.
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, groupByPerson } from '../ui.js';

export async function loadOverdueAlerts() {
  const section = document.getElementById('overdueAlertBanner');
  if (!section) return false; // this page doesn't have the section (e.g. staff/customer dashboards)

  try {
    const result = await apiRequest('/checkouts/overdue?limit=5');

    if (!result.total) {
      section.classList.add('hidden');
      return false;
    }

    document.getElementById('overdueAlertCount').textContent = result.total;

    // Grouped by PERSON, not by item -- see components/due-soon.js's
    // loadDueSoonAlerts() for the identical idea/rationale.
    const groups = groupByPerson(result.items);
    const list = document.getElementById('overdueAlertList');
    list.innerHTML = groups.map(g => {
      const count = g.items.length;
      const canOpen = g.entityType && g.entityId != null;
      return `
      <li class="flex items-center justify-between gap-3 py-1.5 ${canOpen ? 'cursor-pointer transition hover:text-rose-300' : ''}"
        ${canOpen ? `data-action="open-custody" data-entity-id="${g.entityId}" data-entity-type="${g.entityType}" data-action-notification="1"` : ''}>
        <span class="truncate text-slate-200">
          <span class="font-medium">${escapeHtml(g.assigneeName)}</span>
          <span class="text-slate-500"> has ${count} overdue checkout${count === 1 ? '' : 's'}</span>
        </span>
        <span class="shrink-0 tag-mono text-[11px] font-semibold text-rose-400">View →</span>
      </li>`;
    }).join('');

    section.classList.remove('hidden');
    return true;
  } catch (err) {
    // Fail quietly -- a broken notification section should never block the
    // rest of the bell dropdown (or the dashboard) from loading/working.
    section.classList.add('hidden');
    console.error('Failed to load overdue checkouts alert:', err.message);
    return false;
  }
}
