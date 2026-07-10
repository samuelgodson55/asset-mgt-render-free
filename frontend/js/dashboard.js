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
  loadExtensionRequests();
}
