// =============================================================================
// js/components/due-soon.js
// -----------------------------------------------------------------------------
// "A reminder BEFORE something goes overdue" -- renders the "Due Soon"
// alert banner on admin.html / manager.html from GET /checkouts/due-soon.
// The proactive counterpart to js/components/overdue.js's "Overdue
// Checkouts" banner: same shape, same data flow, just surfaced earlier --
// while there's still time to return the item or grant an extension,
// instead of only finding out after the due date has already passed.
//
// Kept as its own tiny module for the exact same reason overdue.js is:
// admin.html and manager.html share the same markup/ids for it, so this
// same code works unmodified on both pages (the backend already scopes
// what a Manager vs a Super Admin sees -- see
// services/checkout_service.py's list_due_soon_checkouts()).
// =============================================================================

import { apiRequest } from '../api.js';
import { escapeHtml, isAlertDismissed, setAlertDismissed, clearAlertDismissed, groupByPerson } from '../ui.js';

// Same dismiss/recall pattern as components/overdue.js's loadOverdueAlerts()
// -- see the comment there, and js/ui.js's isAlertDismissed()/
// setAlertDismissed(), for the full rationale. Persisted to localStorage
// (keyed to a signature of exactly what's currently on screen) rather than
// a plain in-memory flag, so dismissing it actually sticks across a page
// reload/tab switch instead of silently resetting -- and still comes back
// on its own the moment the underlying due-soon list actually changes.
const STORAGE_KEY = 'dueSoon';
let currentSignature = '';

export function dismissDueSoonAlert() {
  const banner = document.getElementById('dueSoonAlertBanner');
  if (banner) banner.classList.add('hidden');
  setAlertDismissed(STORAGE_KEY, currentSignature);
}

export async function loadDueSoonAlerts(force = false) {
  const banner = document.getElementById('dueSoonAlertBanner');
  if (!banner) return false; // this page doesn't have the banner (e.g. staff/customer dashboards)
  if (force) clearAlertDismissed(STORAGE_KEY);

  try {
    const result = await apiRequest('/checkouts/due-soon?limit=5');

    if (!result.total) {
      banner.classList.add('hidden');
      currentSignature = '';
      return false;
    }

    document.getElementById('dueSoonAlertCount').textContent = result.total;

    // Grouped by PERSON, not by item -- someone with several items due
    // soon gets one line ("T. Okafor has 3 checkouts due soon"), not one
    // per item. Full itemized detail (which asset, exact due date) is a
    // click away in their Custody Ledger rather than cluttering this
    // banner -- that's the whole point of it being a summary.
    const groups = groupByPerson(result.items);
    const list = document.getElementById('dueSoonAlertList');
    list.innerHTML = groups.map(g => {
      const count = g.items.length;
      const canOpen = g.entityType && g.entityId != null;
      return `
      <li class="flex items-center justify-between gap-3 py-1 ${canOpen ? 'cursor-pointer transition hover:text-amber-200' : ''}"
        ${canOpen ? `data-action="open-custody" data-entity-id="${g.entityId}" data-entity-type="${g.entityType}"` : ''}>
        <span class="truncate text-slate-200">
          <span class="font-medium">${escapeHtml(g.assigneeName)}</span>
          <span class="text-slate-500"> has ${count} checkout${count === 1 ? '' : 's'} due soon</span>
        </span>
        <span class="shrink-0 tag-mono text-[11px] font-semibold text-amber-400">Check Custody Ledger →</span>
      </li>`;
    }).join('');

    // A signature of exactly what's being shown right now -- changes the
    // instant a checkout is added/removed from the list or a
    // days-until-due count ticks over, which is what lets a stale
    // dismissal auto-expire instead of hiding genuinely new information.
    currentSignature = `total:${result.total}|` + result.items.map(i => `${i.checkout_id}:${i.days_until_due}`).join(',');

    if (!isAlertDismissed(STORAGE_KEY, currentSignature)) banner.classList.remove('hidden');
    return true;
  } catch (err) {
    // Fail quietly -- same reasoning as loadOverdueAlerts(): a broken
    // alert banner should never block the rest of the dashboard from
    // loading/working.
    banner.classList.add('hidden');
    console.error('Failed to load due-soon checkouts alert:', err.message);
    return false;
  }
}
