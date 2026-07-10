// =============================================================================
// js/components/overdue.js
// -----------------------------------------------------------------------------
// Operations & Observability requirement #5: renders the "Overdue Checkouts"
// alert banner on admin.html / manager.html from GET /checkouts/overdue.
//
// Kept as its own tiny module (rather than folded into assets.js or
// dashboard.js) because it owns exactly one job -- load the overdue list and
// paint the banner -- and both admin.html and manager.html share the exact
// same markup/ids for it, so this same code works unmodified on both pages
// (the backend already scopes what a Manager vs a Super Admin sees -- see
// services/checkout_service.py's list_overdue_checkouts()).
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, isAlertDismissed, setAlertDismissed, clearAlertDismissed, groupByPerson } from '../ui.js';

// Tracks whether the person has closed the banner via its X since the
// last explicit "Check Alerts" (see checkAlertsNow() in dashboard.js).
// Lets the banner be dismissed without it immediately popping back open
// on the next routine refreshDashboard() poll, while still being fully
// recallable on demand -- checkAlertsNow() always passes force=true,
// which resets this and re-shows the banner if there's still something
// to report.
//
// Persisted to localStorage (keyed to a signature of exactly what's
// currently on screen -- see js/ui.js's isAlertDismissed()/
// setAlertDismissed()) rather than a plain in-memory flag, so dismissing
// it actually sticks across a page reload/tab switch instead of silently
// resetting -- and still comes back on its own the moment the underlying
// overdue list actually changes.
const STORAGE_KEY = 'overdue';
let currentSignature = '';

export function dismissOverdueAlert() {
  const banner = document.getElementById('overdueAlertBanner');
  if (banner) banner.classList.add('hidden');
  setAlertDismissed(STORAGE_KEY, currentSignature);
}

export async function loadOverdueAlerts(force = false) {
  const banner = document.getElementById('overdueAlertBanner');
  if (!banner) return false; // this page doesn't have the banner (e.g. staff/customer dashboards)
  if (force) clearAlertDismissed(STORAGE_KEY);

  try {
    const result = await apiRequest('/checkouts/overdue?limit=5');

    if (!result.total) {
      banner.classList.add('hidden');
      currentSignature = '';
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
      <li class="flex items-center justify-between gap-3 py-1 ${canOpen ? 'cursor-pointer transition hover:text-rose-300' : ''}"
        ${canOpen ? `data-action="open-custody" data-entity-id="${g.entityId}" data-entity-type="${g.entityType}"` : ''}>
        <span class="truncate text-slate-200">
          <span class="font-medium">${escapeHtml(g.assigneeName)}</span>
          <span class="text-slate-500"> has ${count} overdue checkout${count === 1 ? '' : 's'}</span>
        </span>
        <span class="shrink-0 tag-mono text-[11px] font-semibold text-rose-400">Check Custody Ledger →</span>
      </li>`;
    }).join('');

    // A signature of exactly what's being shown right now -- see
    // due-soon.js's loadDueSoonAlerts() for the identical idea.
    currentSignature = `total:${result.total}|` + result.items.map(i => `${i.checkout_id}:${i.days_overdue}`).join(',');

    // Content is kept fresh even while dismissed, so whenever the banner
    // does come back (a forced re-check, or the signature changing) it's
    // never showing stale data.
    if (!isAlertDismissed(STORAGE_KEY, currentSignature)) banner.classList.remove('hidden');
    return true;
  } catch (err) {
    // Fail quietly -- a broken alert banner should never block the rest of
    // the dashboard from loading/working. The other widgets (assets,
    // users, audit log) already reported their own errors independently.
    banner.classList.add('hidden');
    console.error('Failed to load overdue checkouts alert:', err.message);
    return false;
  }
}
