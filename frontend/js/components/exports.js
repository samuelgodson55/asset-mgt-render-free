// =============================================================================
// js/components/exports.js
// -----------------------------------------------------------------------------
// Shared "download a file from an authenticated GET endpoint" helper, plus
// the specific click handlers for every properties-assigned export button
// in the app (self-service "My Items", the Custody Ledger modal, and the
// "Export All" buttons on the User Directory / Ad-Hoc Directory / Asset
// Inventory). This mirrors the raw-fetch pattern components/audit.js's
// exportAuditLogs() already uses for the Audit Trail export -- apiRequest()
// is JSON-only, so a real file download needs its own fetch + Authorization
// header + blob, rather than going through the normal apiRequest() wrapper.
//
// BUTTON PROGRESS FEEDBACK: every export button below now disables itself
// and swaps its label to "Exporting..." for the duration of the download,
// then restores it -- the same pattern components/backups.js's
// createBackupNow() already uses for the "Create Backup Now" button, so a
// person clicking any export button gets the same "yes, this is working"
// feedback instead of a button that looks unresponsive until the browser's
// save-file prompt suddenly appears.
// =============================================================================

import { API_URL } from '../api.js';
import { getSession } from '../auth.js';
import { getCurrentCustodyEntity } from './custody.js';
import { getCurrentQuoteId, getCurrentMyQuoteDetailId } from './quotation.js';
import { showToast } from '../ui.js';

// Performs the authenticated fetch, reads the real filename the backend
// chose off the Content-Disposition header (falling back to `fallbackName`
// if that header is ever missing), and triggers a normal browser download.
// `button` is optional (some callers -- e.g. a future keyboard shortcut --
// might not have one) -- when present, it's disabled and relabeled for the
// duration of the request so the person gets the same progress feedback
// the backup button already gives.
export async function downloadExport(path, fallbackName, button) {
  const original = button ? button.textContent : null;
  if (button) {
    button.disabled = true;
    button.textContent = 'Exporting…';
  }
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
    showToast('Download complete.');
  } catch (err) {
    alert(err.message);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

// ---- Self-service: "My Checked-Out Items" (staff.html / customer.html) ----
export function exportMyItems(format, button) {
  downloadExport(`/users/me/items/export?format=${format}`, `my_properties.${format}`, button);
}

// ---- Custody Ledger modal: export whichever user/outsider is currently open ----
export function exportCustodyItems(format, button) {
  const entity = getCurrentCustodyEntity();
  if (!entity.id) return; // modal isn't open against anyone -- nothing to export
  const path = entity.type === 'outsider'
    ? `/outsiders/${entity.id}/items/export?format=${format}`
    : `/users/${entity.id}/items/export?format=${format}`;
  downloadExport(path, `properties.${format}`, button);
}

// ---- Bulk: "Export All" on the User Directory / Team Allocation Matrix ----
export function exportAllUsers(format, button) {
  downloadExport(`/users/export?format=${format}`, `all_users_properties.${format}`, button);
}

// ---- Bulk: "Export All" on the Ad-Hoc (Unlinked) Directory ----
export function exportAllOutsiders(format, button) {
  downloadExport(`/outsiders/export?format=${format}`, `all_outsiders_properties.${format}`, button);
}

// ---- Self-service: "My Order" equipment quotation (staff.html / customer.html) ----
export function exportQuotation(button) {
  downloadExport('/quotations/export', 'equipment_quotation.pdf', button);
}

// ---- Admin/Manager: Quotes tab detail modal export (any Quotation by ID) ----
export function exportQuoteDetail(button) {
  const quoteId = getCurrentQuoteId();
  if (!quoteId) return;
  downloadExport(`/quotations/${quoteId}/export`, `quotation_${quoteId}.pdf`, button);
}

// ---- Self-service: "My Quote Detail" modal export (one of the caller's OWN
// submitted quotes, or one assigned to them, by ID) -- staff.html / customer.html ----
export function exportMyQuoteDetail(button) {
  const quoteId = getCurrentMyQuoteDetailId();
  if (!quoteId) return;
  downloadExport(`/quotations/me/${quoteId}/export`, `quotation_${quoteId}.pdf`, button);
}


// ---- Asset Inventory Export button (Asset Inventory tab) ----
// Reads the category chosen in the small #assetExportModal (populated by
// components/assets.js's openAssetExportModal()) -- "all" (the default,
// "Download All") or one specific category -- and downloads the
// inventory list itself (one row per pool), not a properties-assigned
// custody export.
export function exportAssetsInventory(format, button) {
  const select = document.getElementById('assetExportCategory');
  const category = select && select.value ? select.value : 'all';
  const params = new URLSearchParams({ format, category });
  const fallbackName = category === 'all'
    ? `asset_inventory_all.${format}`
    : `asset_inventory_${category.replace(/\s+/g, '_')}.${format}`;
  downloadExport(`/assets/export?${params.toString()}`, fallbackName, button);
}
