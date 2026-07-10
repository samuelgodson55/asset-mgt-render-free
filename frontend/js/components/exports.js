// =============================================================================
// js/components/exports.js
// -----------------------------------------------------------------------------
// Shared "download a file from an authenticated GET endpoint" helper, plus
// the specific click handlers for every properties-assigned export button
// in the app (self-service "My Items", the Custody Ledger modal, and the
// "Export All" buttons on the User Directory / Ad-Hoc Directory). This
// mirrors the raw-fetch pattern components/audit.js's exportAuditLogs()
// already uses for the Audit Trail export -- apiRequest() is JSON-only, so
// a real file download needs its own fetch + Authorization header + blob,
// rather than going through the normal apiRequest() wrapper.
// =============================================================================

import { API_URL } from '../api.js';
import { getSession } from '../auth.js';
import { getCurrentCustodyEntity } from './custody.js';

// Performs the authenticated fetch, reads the real filename the backend
// chose off the Content-Disposition header (falling back to `fallbackName`
// if that header is ever missing), and triggers a normal browser download.
async function downloadExport(path, fallbackName) {
  try {
    const session = getSession();
    const response = await fetch(`${API_URL}${path}`, {
      headers: { 'Authorization': `Bearer ${session.token}` },
    });
    if (!response.ok) throw new Error('Export failed. Please try again.');

    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^";]+)"?/);
    const filename = match ? match[1] : fallbackName;

    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    window.URL.revokeObjectURL(url);
  } catch (err) {
    alert(err.message);
  }
}

// ---- Self-service: "My Checked-Out Items" (staff.html / customer.html) ----
export function exportMyItems(format) {
  downloadExport(`/users/me/items/export?format=${format}`, `my_properties.${format}`);
}

// ---- Custody Ledger modal: export whichever user/outsider is currently open ----
export function exportCustodyItems(format) {
  const entity = getCurrentCustodyEntity();
  if (!entity.id) return; // modal isn't open against anyone -- nothing to export
  const path = entity.type === 'outsider'
    ? `/outsiders/${entity.id}/items/export?format=${format}`
    : `/users/${entity.id}/items/export?format=${format}`;
  downloadExport(path, `properties.${format}`);
}

// ---- Bulk: "Export All" on the User Directory / Team Allocation Matrix ----
export function exportAllUsers(format) {
  downloadExport(`/users/export?format=${format}`, `all_users_properties.${format}`);
}

// ---- Bulk: "Export All" on the Ad-Hoc (Unlinked) Directory ----
export function exportAllOutsiders(format) {
  downloadExport(`/outsiders/export?format=${format}`, `all_outsiders_properties.${format}`);
}
