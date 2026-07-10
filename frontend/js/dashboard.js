// =============================================================================
// js/dashboard.js
// -----------------------------------------------------------------------------
// A single, tiny orchestration point: `refreshDashboard()` re-loads every
// table on the current admin/manager dashboard. It lives in its own module
// (rather than inside components/assets.js, say) specifically so that
// components/*.js can `import { refreshDashboard } from './dashboard.js'`
// after mutating actions (dispatch, delete, return, etc.) without creating a
// real circular dependency -- this file is the only one that imports the
// `load*` functions FROM the components, and every component only calls
// `refreshDashboard()` from inside an event handler (not at module-load
// time), which ES modules handle safely.
// =============================================================================

import { loadAssets } from './components/assets.js';
import { loadUsers, loadDeletedUsers } from './components/users.js';
import { loadOutsiders } from './components/outsiders.js';
import { loadAuditLogs } from './components/audit.js';
import { loadOverdueAlerts } from './components/overdue.js';
import { loadDueSoonAlerts } from './components/due-soon.js';
import { loadExtensionRequests } from './components/extensions.js';

export function refreshDashboard() {
  loadAssets();
  loadUsers();
  // No-ops on any page without a #deletedUserTableBody (e.g. manager.html,
  // which has no Restore Deleted Users panel) -- see loadDeletedUsers()'s
  // own guard in components/users.js.
  loadDeletedUsers();
  loadAuditLogs();
  loadOutsiders();
  loadOverdueAlerts();
  loadDueSoonAlerts();
  loadExtensionRequests();

  // Whatever just changed elsewhere on the dashboard could also change
  // what the three alert widgets above have to show, so a stale "All
  // caught up" toast left over from an earlier manual check (see
  // checkAlertsNow() below) should never linger past this point.
  const allClearBanner = document.getElementById('alertsAllClearBanner');
  if (allClearBanner) allClearBanner.classList.add('hidden');
}

// Manual "Check Now" entry point for the button next to the three alert
// widgets (Overdue / Due Soon / Extension Requests). Those three already
// auto-load on every refreshDashboard() above, but each one only ever
// renders when it has something to show (Operations & Observability
// requirement #5's `result.total > 0` gate -- see components/overdue.js,
// components/due-soon.js, components/extensions.js), so a routine
// background refresh gives no visible confirmation either way. A person
// who explicitly clicks "Check Now" deserves SOME feedback that the
// check actually ran even when every widget comes back clean -- hence
// the "All caught up" banner, shown only when all three report nothing.
export async function checkAlertsNow() {
  const allClearBanner = document.getElementById('alertsAllClearBanner');
  const btn = document.getElementById('checkAlertsBtn');
  if (allClearBanner) allClearBanner.classList.add('hidden');
  if (btn) btn.disabled = true;

  try {
    const [hasOverdue, hasDueSoon, hasExtensionRequests] = await Promise.all([
      loadOverdueAlerts(true),
      loadDueSoonAlerts(true),
      loadExtensionRequests(true),
    ]);

    if (allClearBanner && !hasOverdue && !hasDueSoon && !hasExtensionRequests) {
      allClearBanner.classList.remove('hidden');
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}
