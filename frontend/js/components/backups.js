// =============================================================================
// js/components/backups.js
// -----------------------------------------------------------------------------
// "System Backups" panel (admin.html only -- Super Admin ONLY on the
// backend, every /api/backup/* route gated on deps.require_true_super_admin.
// A plain `admin` session never even reaches this module: main.js's
// DOMContentLoaded handler removes #systemBackupsSection from the DOM
// entirely for any non-super_admin session before any of these functions
// would run).
//
// Covers: showing the daily-schedule/Google-Drive status, listing local
// backup files with Download/Restore/Delete actions, the "Backup Now"
// button, and the two restore paths -- restoring a backup already on local
// disk, and restoring from a freshly uploaded .sql.gz (the recovery path
// once local disk has been wiped by a Render redeploy/spin-down -- see
// backend/services/backup_service.py's module docstring).
//
// Restore is maximally destructive (replaces the ENTIRE database), so both
// restore paths require typing the exact word RESTORE into a confirmation
// modal before the request fires -- a plain confirm() dialog felt too easy
// to click through by habit for an action this size.
//
// isTrueSuperAdmin() below is belt-and-suspenders on top of main.js
// removing the section: it mirrors deps.require_true_super_admin so that
// even if this module somehow runs without that removal happening first,
// the row-level Restore control still won't render as usable.
// =============================================================================

import { apiRequest, API_URL } from '../api.js';
import { getSession } from '../auth.js';
import { escapeHtml, openModal, closeModal, showToast, showFieldError, clearFieldError } from '../ui.js';

let pendingRestore = null; // { mode: 'local' | 'upload', filename?: string, file?: File }

// Mirrors deps.require_true_super_admin on the backend: restore (and, as
// of this panel's latest gating, the whole System Backups panel) is
// restricted to the root Super Admin account, not the broader
// Super-Admin-equivalent `admin` role this app otherwise treats as
// identical.
function isTrueSuperAdmin() {
  const session = getSession();
  return !!session && session.role === 'super_admin';
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}

function formatWhen(isoString) {
  if (!isoString) return '—';
  const date = new Date(isoString);
  return date.toLocaleString();
}

function gdriveBadge(entry) {
  if (entry.gdrive_error) {
    return `<span class="inline-flex items-center gap-1 rounded-full border border-rose-500/40 bg-rose-500/10 px-2 py-0.5 text-[11px] font-medium text-rose-400" title="${escapeHtml(entry.gdrive_error)}">Drive upload failed</span>`;
  }
  if (entry.gdrive_uploaded) {
    return `<span class="inline-flex items-center gap-1 rounded-full border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-400">Synced to Drive</span>`;
  }
  return `<span class="inline-flex items-center gap-1 rounded-full border border-border bg-card2 px-2 py-0.5 text-[11px] font-medium text-slate-500">Local only</span>`;
}

const TRIGGER_LABELS = {
  manual: 'Manual',
  scheduled: 'Scheduled',
  pre_restore_safety: 'Pre-restore safety',
};

export async function loadBackupStatus() {
  const el = document.getElementById('backupStatusCard');
  if (!el) return;
  try {
    const status = await apiRequest('/backup/status');
    const latest = status.latest_backup;
    el.innerHTML = `
      <div class="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <div>
          <p class="text-[11px] uppercase tracking-wide text-slate-500">Daily schedule</p>
          <p class="text-[13px] font-semibold text-slate-200">${status.auto_backup_enabled ? status.backup_hours_display.join(', ') + ' ' + status.display_timezone_label : 'Disabled'}</p>
        </div>
        <div>
          <p class="text-[11px] uppercase tracking-wide text-slate-500">Google Drive sync</p>
          <p class="text-[13px] font-semibold ${status.gdrive_enabled ? 'text-emerald-400' : 'text-slate-500'}">${status.gdrive_enabled ? 'Enabled' : 'Not configured'}</p>
        </div>
        <div>
          <p class="text-[11px] uppercase tracking-wide text-slate-500">Local backups kept</p>
          <p class="text-[13px] font-semibold text-slate-200">${status.backup_count} / ${status.retention_count}</p>
        </div>
        <div>
          <p class="text-[11px] uppercase tracking-wide text-slate-500">Last backup</p>
          <p class="text-[13px] font-semibold text-slate-200">${latest ? formatWhen(latest.created_at) : 'None yet'}</p>
        </div>
      </div>
      ${status.gdrive_enabled ? '' : `
      <p class="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-400">
        Google Drive sync is off -- local backups do not survive a redeploy or spin-down on Render's Free plan.
        Set BACKUP_GDRIVE_ENABLED (and its credentials/folder ID) to make backups durable. See README.md's "Backups" section.
      </p>`}
    `;
  } catch (err) {
    el.innerHTML = `<p class="text-[13px] text-rose-400">Failed to load backup status: ${escapeHtml(err.message)}</p>`;
  }
}

export async function loadBackupList() {
  const tbody = document.getElementById('backupTableBody');
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="5" class="px-5 py-6 text-center text-slate-500">Loading backups…</td></tr>`;
  try {
    const entries = await apiRequest('/backup/list');
    if (!entries.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="px-5 py-6 text-center text-slate-500">No backups yet -- click "Backup Now" to create one.</td></tr>`;
      return;
    }
    // MOBILE OVERFLOW FIX: this used to render File, Cloud Sync, and
    // Actions as three separately always-visible columns -- a long
    // filename plus a Cloud Sync badge plus three action buttons don't fit
    // side-by-side on a phone, forcing the whole table into horizontal
    // scroll and clipping the badge/buttons off-screen. Now only ONE
    // column (File) stays a real always-visible column; Created/Size/
    // Trigger/Cloud Sync/Actions are all hidden as separate columns below
    // `sm` and instead rendered a second time, stacked, INSIDE that same
    // File cell (shown only below `sm` via the `sm:hidden` wrapper) --
    // same "everything essential in one cell, full grid on desktop"
    // approach as every other responsive table in this app, just without
    // a details modal since these actions (Download/Restore/Delete) need
    // to stay directly tappable rather than hidden behind a tap-to-open
    // popup.
    tbody.innerHTML = entries.map((entry) => {
      const metaLine = `${formatWhen(entry.created_at)} · ${formatBytes(entry.size_bytes)} · ${TRIGGER_LABELS[entry.triggered_by] || entry.triggered_by}`;
      const restoreBtn = isTrueSuperAdmin()
        ? `<button data-action="restore-local-backup" data-filename="${escapeHtml(entry.filename)}" title="Restore the database from this backup" class="rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-[12px] font-medium text-amber-400 transition hover:bg-amber-500/20">Restore</button>`
        : `<button disabled title="Restore is restricted to the Super Admin account" class="cursor-not-allowed rounded-md border border-border bg-card2 px-2.5 py-1 text-[12px] font-medium text-slate-600">Restore</button>`;
      const actionButtons = `
        <button data-action="download-backup" data-filename="${escapeHtml(entry.filename)}" title="Download" class="rounded-md border border-border bg-card2 px-2.5 py-1 text-[12px] font-medium text-slate-300 transition hover:border-blue-500/50 hover:text-blue-400">Download</button>
        ${restoreBtn}
        <button data-action="delete-backup" data-filename="${escapeHtml(entry.filename)}" title="Delete this local backup file" class="rounded-md border border-border bg-card2 px-2.5 py-1 text-[12px] font-medium text-slate-400 transition hover:border-rose-500/50 hover:text-rose-400">Delete</button>`;

      return `
      <tr>
        <td class="px-5 py-3">
          <p class="break-all tag-mono text-[12px] text-slate-300">${escapeHtml(entry.filename)}</p>
          <!-- Mobile-only stacked meta/badge/actions (hidden on sm+, where
               the dedicated columns to the right take over instead). -->
          <p class="mt-1 text-[11px] text-slate-500 sm:hidden">${metaLine}</p>
          <div class="mt-2 sm:hidden">${gdriveBadge(entry)}</div>
          <div class="mt-3 flex flex-wrap gap-2 sm:hidden">${actionButtons}</div>
        </td>
        <td class="hidden px-5 py-3 text-[12px] text-slate-400 sm:table-cell">${formatWhen(entry.created_at)}</td>
        <td class="hidden px-5 py-3 text-[12px] text-slate-400 sm:table-cell">${formatBytes(entry.size_bytes)} · ${TRIGGER_LABELS[entry.triggered_by] || entry.triggered_by}</td>
        <td class="hidden px-5 py-3 sm:table-cell">${gdriveBadge(entry)}</td>
        <td class="hidden px-5 py-3 text-right sm:table-cell">
          <div class="flex items-center justify-end gap-2">${actionButtons}</div>
        </td>
      </tr>
    `;
    }).join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="px-5 py-6 text-center text-rose-400">Failed to load backups: ${escapeHtml(err.message)}</td></tr>`;
  }
}

export function refreshBackupsPanel() {
  loadBackupStatus();
  loadBackupList();
}

// ---- Backup Now ----
export async function createBackupNow(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Backing up…';
  try {
    await apiRequest('/backup/create', { method: 'POST' });
    refreshBackupsPanel();
  } catch (err) {
    alert(`Backup failed: ${err.message}`);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

// ---- Download ----
export async function downloadBackup(el) {
  const filename = el.dataset.filename;
  try {
    const response = await fetch(`${API_URL}/backup/download/${encodeURIComponent(filename)}`, {
      credentials: 'include',
    });
    if (!response.ok) throw new Error('Download failed.');
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
  }
}

// ---- Delete ----
export async function deleteBackup(el) {
  const filename = el.dataset.filename;
  if (!confirm(`Delete local backup "${filename}"? This does not affect any copy already uploaded to Google Drive.`)) return;
  try {
    await apiRequest(`/backup/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    refreshBackupsPanel();
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
}

// ---- Restore (local file already on disk) ----
export function openRestoreLocalModal(el) {
  if (!isTrueSuperAdmin()) {
    alert('Restore is restricted to the Super Admin account.');
    return;
  }
  pendingRestore = { mode: 'local', filename: el.dataset.filename };
  const label = document.getElementById('restoreModalTarget');
  if (label) label.textContent = el.dataset.filename;
  const uploadRow = document.getElementById('restoreUploadRow');
  if (uploadRow) uploadRow.classList.add('hidden');
  const confirmInput = document.getElementById('restoreConfirmInput');
  if (confirmInput) confirmInput.value = '';
  openModal('restoreBackupModal');
}

// ---- Restore (upload a file -- e.g. downloaded from Google Drive) ----
export function openRestoreUploadModal() {
  if (!isTrueSuperAdmin()) {
    alert('Restore is restricted to the Super Admin account.');
    return;
  }
  pendingRestore = { mode: 'upload' };
  const label = document.getElementById('restoreModalTarget');
  if (label) label.textContent = 'the file you upload below';
  const uploadRow = document.getElementById('restoreUploadRow');
  if (uploadRow) uploadRow.classList.remove('hidden');
  const confirmInput = document.getElementById('restoreConfirmInput');
  if (confirmInput) confirmInput.value = '';
  const fileInput = document.getElementById('restoreUploadInput');
  if (fileInput) fileInput.value = '';
  openModal('restoreBackupModal');
}

export async function confirmRestore() {
  if (!pendingRestore) return;
  const confirmInput = document.getElementById('restoreConfirmInput');
  if (!confirmInput || confirmInput.value.trim() !== 'RESTORE') {
    alert('Type RESTORE (all caps) in the confirmation box to proceed.');
    return;
  }

  const confirmBtn = document.getElementById('restoreConfirmBtn');
  const originalText = confirmBtn ? confirmBtn.textContent : '';
  if (confirmBtn) {
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Restoring…';
  }

  try {
    if (pendingRestore.mode === 'local') {
      await apiRequest(`/backup/restore/${encodeURIComponent(pendingRestore.filename)}`, { method: 'POST' });
    } else {
      const fileInput = document.getElementById('restoreUploadInput');
      const file = fileInput && fileInput.files && fileInput.files[0];
      if (!file) {
        alert('Choose a backup file to upload first.');
        if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = originalText; }
        return;
      }
      const formData = new FormData();
      formData.append('file', file);
      const response = await fetch(`${API_URL}/backup/restore-upload`, {
        method: 'POST',
        body: formData,
        credentials: 'include',
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Restore failed.');
    }
    closeModal('restoreBackupModal');
    alert('Restore complete. The database has been replaced with the chosen backup. A safety backup of the prior state was taken automatically before the restore ran.');
    pendingRestore = null;
    refreshBackupsPanel();
  } catch (err) {
    alert(`Restore failed: ${err.message}`);
  } finally {
    if (confirmBtn) {
      confirmBtn.disabled = false;
      confirmBtn.textContent = originalText;
    }
  }
}

// =============================================================================
// ADMIN: DAILY DIGEST RECIPIENTS (admin.html's "Daily Digest Recipients" card,
// Audit & Backups tab)
// -----------------------------------------------------------------------------
// GET/PUT /settings/digest-recipients (backend/api/notifications.py,
// Super Admin/Admin only). This is the SOLE audience for the once-a-day
// overdue/due-soon summary email (see backend/tasks/notification_tasks.py) --
// being an Admin/Manager account no longer implies receiving it. Addresses
// here don't need to correspond to an app user at all.
//
// Edited as a full list, not one-at-a-time on the server: each Remove click
// re-PUTs the whole array with that address dropped, and Add re-PUTs it with
// the new one appended (after a client-side de-dupe check) -- mirrors how
// DigestRecipientsUpdateRequest on the backend is documented as a full
// replace rather than an add/remove-one endpoint.
// =============================================================================
let digestRecipientsCache = [];

export async function loadDigestRecipients() {
  const list = document.getElementById('digestRecipientsList');
  if (!list) return;
  try {
    const data = await apiRequest('/settings/digest-recipients');
    digestRecipientsCache = data.emails || [];
    renderDigestRecipients();
  } catch (err) {
    list.innerHTML = `<span class="text-[12px] text-rose-400">${escapeHtml(err.message)}</span>`;
  }
}

function renderDigestRecipients() {
  const list = document.getElementById('digestRecipientsList');
  if (!list) return;
  if (digestRecipientsCache.length === 0) {
    list.innerHTML = '<span class="text-[12px] text-slate-500">No recipients configured -- the daily digest currently has nowhere to send. Add an address below.</span>';
    return;
  }
  list.innerHTML = digestRecipientsCache.map((email) => `
    <span class="flex items-center gap-2 rounded-full border border-border bg-card2 py-1 pl-3 pr-1.5 text-[12px] text-slate-300">
      ${escapeHtml(email)}
      <button type="button" data-action="remove-digest-recipient" data-email="${escapeHtml(email)}"
        title="Remove" class="rounded-full p-0.5 text-slate-500 transition hover:bg-rose-500/10 hover:text-rose-400">
        <svg class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>
      </button>
    </span>
  `).join('');
}

async function saveDigestRecipients(nextEmails, successMessage) {
  const messageEl = document.getElementById('digestRecipientsMessage');
  try {
    const data = await apiRequest('/settings/digest-recipients', { method: 'PUT', body: JSON.stringify({ emails: nextEmails }) });
    digestRecipientsCache = data.emails || [];
    renderDigestRecipients();
    if (messageEl) {
      messageEl.textContent = successMessage;
      messageEl.className = 'text-[12px] font-medium text-emerald-400';
      messageEl.classList.remove('hidden');
    }
    showToast(successMessage);
  } catch (err) {
    if (messageEl) {
      messageEl.textContent = err.message;
      messageEl.className = 'text-[12px] font-medium text-rose-400';
      messageEl.classList.remove('hidden');
    }
  }
}

export async function submitDigestRecipientAddForm(event) {
  event.preventDefault();
  const input = document.getElementById('digestRecipientInput');
  if (!input) return;
  const email = input.value.trim().toLowerCase();
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  if (!EMAIL_RE.test(email)) {
    showFieldError('digestRecipientInput', 'Enter a valid email address.');
    return;
  }
  clearFieldError('digestRecipientInput');
  if (digestRecipientsCache.includes(email)) {
    showFieldError('digestRecipientInput', 'That address is already on the list.');
    return;
  }
  await saveDigestRecipients([...digestRecipientsCache, email], `${email} will now receive the daily digest.`);
  input.value = '';
}

export async function removeDigestRecipient(email) {
  await saveDigestRecipients(
    digestRecipientsCache.filter((existing) => existing !== email),
    `${email} removed from the daily digest.`,
  );
}
