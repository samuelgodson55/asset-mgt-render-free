// =============================================================================
// js/components/audit.js
// -----------------------------------------------------------------------------
// Audit Trail table + the "Export Audit Ledger" (CSV/PDF) action.
//
// EXPORTS ARE ASYNC (run on a background Celery worker, not inline in the
// request -- see backend/tasks/export_tasks.py and backend/api/audit.py):
// `exportAuditLogs()` below enqueues a job, polls its status, then
// downloads the finished file once ready. The final download step is the
// one place this file needs a raw (non-JSON) authenticated fetch, so it
// uses `API_URL` + the session token directly rather than `apiRequest`
// (which assumes a JSON or passthrough-Response result, not a file blob).
//
// PAGINATION NOTE (Data Quality & Usability requirement #4): the audit
// ledger is genuinely unbounded (it's an append-only log that grows for
// the entire lifetime of the system), so this file does TRUE server-side
// pagination: every page turn or "rows per page" change re-fetches just
// that slice from `GET /audit-logs?limit=&offset=`. `auditState` below is
// this file's own tiny bit of state (page number + rows-per-page +
// last-known total) -- it's intentionally NOT wired into js/ui.js's
// client-side `tableState`/`filterAndPaginate` machinery, since that
// machinery assumes the full dataset is already sitting in the browser,
// which is exactly what we're avoiding here.
//
// This is now the same pattern used by the Asset/User/Outsider
// directories (see components/assets.js, components/users.js,
// components/outsiders.js) -- `renderServerPaginationBar()` in js/ui.js is
// the bit all four of these files share.
// =============================================================================

import { apiRequest, API_URL } from '../api.js';
import { getSession } from '../auth.js';
import { escapeHtml, formatTimestamp, renderServerPaginationBar, rowDetailsTrigger } from '../ui.js';

const auditState = { page: 1, perPage: 5, total: 0 };

export async function loadAuditLogs() {
  const tbody = document.getElementById('auditTableBody');
  if (!tbody) return;
  try {
    const offset = (auditState.page - 1) * auditState.perPage;
    const result = await apiRequest(`/audit-logs?limit=${auditState.perPage}&offset=${offset}`);
    auditState.total = result.total;

    // Whole row is tappable on mobile -- see components/assets.js's
    // renderAssetsTable() for the full explanation of this pattern.
    //
    // MOBILE OVERFLOW FIX: this used to render Timestamp (whitespace-nowrap)
    // and the Action badge as two always-visible columns side-by-side. On a
    // narrow phone a long action string (e.g. "CHECKOUT_DISPATCHED") plus a
    // full timestamp don't fit in one row, forcing the whole table into
    // horizontal scroll and clipping the badge off-screen. Now only the
    // Timestamp column is a real always-visible column; the Action badge is
    // nested underneath it (small, `truncate`d so a long action name wraps
    // to an ellipsis instead of pushing the row wider) and only shown below
    // `sm` -- its own standalone column (still present for desktop) is
    // hidden on mobile instead, matching the "one primary column, rest
    // behind a tap" pattern every other table in this app already uses.
    tbody.innerHTML = result.items.map(l => `
    <tr ${rowDetailsTrigger('Log Entry', [
      ['Timestamp', escapeHtml(l.timestamp)],
      ['Operator', escapeHtml(l.operator)],
      ['Action', escapeHtml(l.action)],
      ['Detail', escapeHtml(l.details)],
    ])} class="cursor-pointer transition hover:bg-card2/40 active:bg-card2/60 sm:cursor-default">
      <td class="px-5 py-2.5">
        <div class="flex items-center gap-2">
          <div class="min-w-0">
            <p class="whitespace-nowrap" title="${escapeHtml(l.timestamp)}">${escapeHtml(formatTimestamp(l.timestamp))}</p>
            <span class="mt-1 inline-block max-w-full truncate rounded bg-blue-500/10 px-1.5 py-0.5 align-bottom text-blue-400 ring-1 ring-blue-500/30 sm:hidden">${escapeHtml(l.action)}</span>
          </div>
          <!-- Mobile-only affordance showing the row itself is tappable
               (replaces the old separate "Details" button). -->
          <svg class="ml-auto h-4 w-4 shrink-0 text-slate-600 sm:hidden" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
        </div>
      </td>
      <td class="hidden px-5 py-2.5 sm:table-cell">${escapeHtml(l.operator)}</td>
      <td class="hidden px-5 py-2.5 sm:table-cell">
        <span class="rounded bg-blue-500/10 px-1.5 py-0.5 text-blue-400 ring-1 ring-blue-500/30">${escapeHtml(l.action)}</span>
      </td>
      <td class="hidden px-5 py-2.5 text-slate-500 sm:table-cell">${escapeHtml(l.details)}</td>
    </tr>`).join('') || `<tr><td colspan="4" class="px-5 py-6 text-center text-slate-500">No log entries yet.</td></tr>`;

    renderServerPaginationBar('audit', auditState);
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="4" class="px-5 py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
  }
}

// Called by main.js's delegated click handler when Prev/Next is clicked.
export function changeAuditPage(delta) {
  const nextPage = auditState.page + delta;
  if (nextPage < 1) return;
  auditState.page = nextPage;
  loadAuditLogs();
}

// Called by main.js's delegated change handler when "Rows per page" changes.
export function setAuditPerPage(value) {
  auditState.perPage = parseInt(value, 10) || 5;
  auditState.page = 1; // changing page size always resets back to page 1
  loadAuditLogs();
}

// ---- Export Audit Ledger (CSV or PDF) ----
// Prompts the user for an optional start/end date range, then runs the
// whole export as a background job instead of generating the file inline
// in one request:
//   1. POST /audit-logs/export?format=&start_date=&end_date= enqueues the
//      job on the Celery worker (see backend/tasks/export_tasks.py) and
//      returns a task_id immediately -- it does NOT wait for the file.
//   2. We poll GET /audit-logs/export/{task_id}/status every 1.2s,
//      updating the small status text next to the buttons so the UI never
//      just sits there looking frozen while the worker builds the file
//      (this matters most for a wide date-range PDF of an unbounded
//      ledger, which is exactly the case that used to risk tying up the
//      request for a long time).
//   3. Once status is SUCCESS, we fetch the finished file from
//      .../download (a raw, non-JSON response -- same reason this file
//      already uses `API_URL` + the session token directly rather than
//      `apiRequest`) and trigger the browser download.
const EXPORT_POLL_INTERVAL_MS = 1200;
const EXPORT_POLL_TIMEOUT_MS = 5 * 60 * 1000; // give up politely after 5 minutes

function setExportStatus(text, isError = false) {
  const el = document.getElementById('auditExportStatus');
  if (!el) return;
  el.textContent = text;
  el.className = `text-[12px] ${isError ? 'text-rose-400' : 'text-slate-500'}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function exportAuditLogs(format = 'csv') {
  try {
    const startDate = prompt('Export from which date? (YYYY-MM-DD, leave blank for "no start limit")', '');
    if (startDate === null) return; // user clicked Cancel -- abort the export entirely
    const endDate = prompt('Export up to which date? (YYYY-MM-DD, leave blank for "no end limit")', '');
    if (endDate === null) return;

    const params = new URLSearchParams();
    if (startDate.trim()) params.set('start_date', startDate.trim());
    if (endDate.trim()) params.set('end_date', endDate.trim());
    params.set('format', format);

    setExportStatus('Queuing export…');
    const { task_id } = await apiRequest(`/audit-logs/export?${params.toString()}`, { method: 'POST' });

    const deadline = Date.now() + EXPORT_POLL_TIMEOUT_MS;
    while (true) {
      const status = await apiRequest(`/audit-logs/export/${task_id}/status`);

      if (status.state === 'SUCCESS') {
        setExportStatus('Downloading…');
        await downloadFinishedExport(task_id, format);
        setExportStatus('Export ready.');
        setTimeout(() => setExportStatus(''), 4000);
        return;
      }
      if (status.state === 'FAILURE') {
        throw new Error(status.error || 'Export failed on the server.');
      }
      if (Date.now() > deadline) {
        throw new Error('Export is taking longer than expected. Please try again shortly.');
      }

      setExportStatus(status.state === 'STARTED' ? 'Generating file…' : 'Waiting for a worker…');
      await sleep(EXPORT_POLL_INTERVAL_MS);
    }
  } catch (err) {
    setExportStatus(err.message, true);
    alert(err.message);
  }
}

// Fetches the finished file for a completed export job and triggers a
// browser download. Kept separate from `apiRequest` (same reason the CSV
// export always was) because the response body here is a raw file blob,
// not JSON.
async function downloadFinishedExport(taskId, format) {
  const session = getSession();
  const response = await fetch(`${API_URL}/audit-logs/export/${taskId}/download`, {
    headers: { 'Authorization': `Bearer ${session.token}` },
  });
  if (!response.ok) throw new Error('The finished export could not be downloaded. Please try exporting again.');

  const blob = await response.blob();
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `audit_export_${new Date().toISOString().slice(0, 10)}.${format}`;
  a.click();
  window.URL.revokeObjectURL(url);
}
